"""Descarga de un mensaje con media y re-subida a un chat destino."""
import asyncio
import os
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from telethon import errors
from telethon.tl.types import MessageMediaWebPage

import config
from fast_download import download_document_fast
from fast_upload import upload_file_fast

MAX_CAPTION = 1024
# Debajo de esto la descarga estandar alcanza (abrir 8 conexiones no compensa).
FAST_DOWNLOAD_MIN_BYTES = 10 * 1024 * 1024
ALBUM_SIZE = 10  # maximo de fotos por album en Telegram
MAX_PHOTO_BYTES = 10 * 1024 * 1024  # mayores no se aceptan como foto
# Margen por archivo para cabeceras ZIP/ZIP64 al calcular el tamano de cada parte.
ZIP_ENTRY_OVERHEAD = 1024
MIB = 1024 * 1024


def has_file(msg) -> bool:
    return msg is not None and msg.file is not None and not isinstance(msg.media, MessageMediaWebPage)


def file_size(msg) -> int:
    return (msg.file.size or 0) if has_file(msg) else 0


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024


def ensure_disk_space(needed: int) -> None:
    """Falla antes de descargar si en TMP_DIR no entran `needed` bytes mas
    el margen de config.DISK_MARGIN_MB."""
    os.makedirs(config.TMP_DIR, exist_ok=True)
    free = shutil.disk_usage(config.TMP_DIR).free
    if free < needed + config.DISK_MARGIN_MB * MIB:
        raise RuntimeError(f"sin espacio en disco: necesito {human(needed)} y hay {human(free)} libres")


async def safe_run(coro_fn, *args, max_retries=5, **kwargs):
    for attempt in range(1, max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except errors.FloodWaitError as e:
            print(f"  [FloodWait] esperando {e.seconds + 1}s (intento {attempt}/{max_retries})")
            await asyncio.sleep(e.seconds + 1)
    raise RuntimeError(f"Excedido max_retries ({max_retries}) tras FloodWait repetido")


def _progress_callback(setter, verb):
    start = time.monotonic()

    async def cb(done, total):
        span = max(time.monotonic() - start, 1e-6)
        pct = f" ({done * 100 / total:.0f}%)" if total else ""
        await setter(f"{verb}: {human(done)} / {human(total or 0)}{pct} a {human(done / span)}/s")

    return cb


class Board:
    """Mensaje de estado del bot con varias lineas que se actualizan por
    separado (p.ej. la descarga del siguiente archivo y la subida del actual
    a la vez). Edita como mucho una vez cada `interval` segundos para no
    chocar con FloodWait, y conserva los `buttons` en cada edicion."""

    def __init__(self, status_msg, header, buttons=None, interval=4.0):
        self.status_msg = status_msg
        self.header = header
        self.buttons = buttons
        self.interval = interval
        self.lines = {}
        self.last = 0.0
        self.last_text = None

    async def render(self, force=False):
        now = time.monotonic()
        if not force and now - self.last < self.interval:
            return
        full = "\n\n".join([self.header, *self.lines.values()])
        if full == self.last_text:
            return
        self.last, self.last_text = now, full
        try:
            await self.status_msg.edit(full, buttons=self.buttons)
        except (errors.MessageNotModifiedError, errors.FloodWaitError):
            pass

    def line(self, key, label) -> "BoardLine":
        return BoardLine(self, key, label)

    def remove(self, key):
        self.lines.pop(key, None)


class BoardLine:
    def __init__(self, board: Board, key, label):
        self.board, self.key, self.label = board, key, label

    async def set(self, text, force=False):
        self.board.lines[self.key] = f"{self.label}\n{text}"
        await self.board.render(force)

    def callback(self, verb):
        return _progress_callback(self.set, verb)


class Reporter(BoardLine):
    """Estado de una sola linea con encabezado (atajo sobre Board)."""

    def __init__(self, status_msg, header, buttons=None, interval=4.0):
        super().__init__(Board(status_msg, header, buttons, interval), "main", "")

    async def set(self, text, force=False):
        self.board.lines["main"] = text
        await self.board.render(force)


async def _refetch(client, msg):
    chat = await msg.get_input_chat()
    return await client.get_messages(chat, ids=msg.id)


def _file_name(msg) -> str:
    """Nombre con el que se guarda el documento (el mismo criterio que usa
    download_media cuando el archivo no trae nombre)."""
    if msg.file.name:
        return os.path.basename(msg.file.name)
    kind = "video" if msg.video else "audio" if msg.audio or msg.voice else "document"
    return f"{kind}_{msg.date:%Y-%m-%d_%H-%M-%S}{msg.file.ext or ''}"


async def _download_once(client, msg, dest_dir, progress_cb):
    doc = msg.document
    if doc and doc.size >= FAST_DOWNLOAD_MIN_BYTES:
        path = os.path.join(dest_dir, _file_name(msg))
        try:
            return await safe_run(download_document_fast, client, msg, path,
                                  progress_callback=progress_cb)
        except errors.FileReferenceExpiredError:
            raise
        except Exception as e:
            # Cualquier otro problema con las conexiones paralelas (DC,
            # autorizacion exportada, etc.): se cae a la descarga estandar.
            print(f"  descarga paralela fallo ({e!r}), uso la estandar")
            if os.path.exists(path):
                os.remove(path)
    return await safe_run(client.download_media, msg, file=dest_dir + os.sep,
                          progress_callback=progress_cb)


async def _download(client, msg, dest_dir, progress_cb):
    try:
        return msg, await _download_once(client, msg, dest_dir, progress_cb)
    except errors.FileReferenceExpiredError:
        # La referencia del archivo vence (~1h): se relee el mensaje.
        msg = await _refetch(client, msg)
        if not has_file(msg):
            raise RuntimeError("el mensaje ya no esta disponible")
        return msg, await _download_once(client, msg, dest_dir, progress_cb)


def _is_media_type(msg) -> bool:
    """True si en origen se mostraba como media (foto/video/audio/gif) y no
    como archivo adjunto generico."""
    return bool(msg.photo or msg.video or msg.gif or msg.audio or msg.voice or msg.video_note)


# ---------- modo separado: descarga y subida por separado ----------


@dataclass
class Prepared:
    """Archivo ya descargado, listo para subir."""
    msg: object
    path: Path
    thumb: str | None
    work_dir: str

    def cleanup(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)


async def prepare(dl_client, msg, reporter) -> Prepared:
    """Descarga `msg` (y su miniatura) con `dl_client` a una carpeta propia.
    Separado de publish() para poder bajar el siguiente archivo mientras se
    sube el actual."""
    ensure_disk_space(file_size(msg))
    work_dir = os.path.join(config.TMP_DIR, uuid.uuid4().hex)
    os.makedirs(work_dir, exist_ok=True)
    try:
        msg, path = await _download(dl_client, msg, work_dir, reporter.callback("⬇️ descargando"))
        if not path:
            raise RuntimeError("la descarga no devolvio archivo")
        thumb = None
        if msg.document and msg.document.thumbs:
            try:
                thumb = await dl_client.download_media(msg, file=os.path.join(work_dir, "thumb.jpg"), thumb=-1)
            except Exception:
                thumb = None
        return Prepared(msg, Path(path), thumb, work_dir)
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


async def publish(up_client, prep: Prepared, dest, reporter, topic_id=None) -> str:
    """Sube un archivo ya descargado a `dest` con `up_client`. `topic_id` es
    el tema destino en supergrupos con modo Foro (None = General)."""
    msg, path = prep.msg, prep.path
    handle = await safe_run(upload_file_fast, up_client, path,
                            progress_callback=reporter.callback("⬆️ subiendo"))

    caption = msg.message or ""
    entities = msg.entities
    if len(caption) > MAX_CAPTION:
        caption, entities = caption[: MAX_CAPTION - 1] + "…", None

    kwargs = dict(caption=caption, formatting_entities=entities, reply_to=topic_id,
                  file_size=path.stat().st_size,
                  force_document=not _is_media_type(msg))
    if msg.document:
        # Copiar atributos originales conserva nombre, duracion,
        # dimensiones de video, artista/titulo de audio, etc.
        kwargs["attributes"] = list(msg.document.attributes)
        kwargs["mime_type"] = msg.document.mime_type
        kwargs["thumb"] = prep.thumb
        if msg.video:
            kwargs["supports_streaming"] = True
    try:
        await safe_run(up_client.send_file, dest, handle, **kwargs)
    except (errors.PhotoSaveFileInvalidError, errors.PhotoInvalidDimensionsError):
        kwargs["force_document"] = True
        await safe_run(up_client.send_file, dest, handle, **kwargs)
    except errors.MediaCaptionTooLongError:
        kwargs["caption"], kwargs["formatting_entities"] = "", None
        await safe_run(up_client.send_file, dest, handle, **kwargs)
        if caption:
            await safe_run(up_client.send_message, dest, caption, reply_to=topic_id)
    return path.name


# ---------- modo ZIP ----------


def is_album_image(msg) -> bool:
    """Fotos, e imagenes JPG/PNG enviadas como archivo que Telegram aceptaria
    como foto (para publicarlas en el album)."""
    if msg.photo:
        return True
    doc = msg.document
    return bool(doc and doc.mime_type in ("image/jpeg", "image/png") and doc.size <= MAX_PHOTO_BYTES)


def _unique_arcname(name: str, used: set) -> str:
    stem, ext = os.path.splitext(name)
    candidate, n = name, 1
    while candidate.lower() in used:
        n += 1
        candidate = f"{stem} ({n}){ext}"
    used.add(candidate.lower())
    return candidate


def build_zips(files: list[Path], out_dir: str, base_name: str, max_bytes: int) -> list[tuple[Path, list[Path]]]:
    """Empaqueta `files` (en orden) en uno o mas ZIP sin compresion (los
    archivos multimedia ya vienen comprimidos), partiendo cuando una parte
    superaria `max_bytes`. Devuelve [(zip, archivos que contiene)].
    Es bloqueante: llamar con asyncio.to_thread."""
    groups, current, size = [], [], 0
    for f in files:
        fsize = f.stat().st_size + ZIP_ENTRY_OVERHEAD
        if current and size + fsize > max_bytes:
            groups.append(current)
            current, size = [], 0
        current.append(f)
        size += fsize
    if current:
        groups.append(current)

    zips = []
    for i, group in enumerate(groups, 1):
        suffix = f"_parte{i}" if len(groups) > 1 else ""
        zip_path = Path(out_dir) / f"{base_name}{suffix}.zip"
        used = set()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for f in group:
                zf.write(f, arcname=_unique_arcname(f.name, used))
        zips.append((zip_path, group))
    return zips


def _zip_caption(names: list[str], part: int, parts: int) -> str:
    head = f"🗜️ {len(names)} archivo(s)"
    if parts > 1:
        head += f" — parte {part}/{parts}"
    lines = [head, ""]
    for name in names:
        lines.append(f"• {name}")
    caption = "\n".join(lines)
    if len(caption) > MAX_CAPTION:
        caption = caption[: MAX_CAPTION - 1] + "…"
    return caption


async def _send_album(up_client, dest, images, topic_id):
    """Publica las imagenes de a ALBUM_SIZE por post, conservando el caption
    de cada una."""
    for start in range(0, len(images), ALBUM_SIZE):
        chunk = images[start:start + ALBUM_SIZE]
        captions, entities = [], []
        for msg, _ in chunk:
            caption, ents = msg.message or "", list(msg.entities or [])
            if len(caption) > MAX_CAPTION:
                caption, ents = caption[: MAX_CAPTION - 1] + "…", []
            captions.append(caption)
            entities.append(ents)
        files = [str(path) for _, path in chunk]
        try:
            await safe_run(up_client.send_file, dest, files, caption=captions,
                           formatting_entities=entities, reply_to=topic_id)
        except (errors.PhotoSaveFileInvalidError, errors.PhotoInvalidDimensionsError):
            # Alguna imagen no se acepto como foto: se mandan como documentos
            # (Telegram las muestra como mensajes sueltos, no como album).
            await safe_run(up_client.send_file, dest, files, caption=captions,
                           formatting_entities=entities, reply_to=topic_id,
                           force_document=True)


async def transfer_zipped(entries, up_client, dest, status_msg, title, topic_id=None,
                          zip_max_bytes=2000 * MIB, base_name=None, buttons=None):
    """Modo ZIP: descarga todos los mensajes de `entries` [(dl_client, msg,
    label)], sube un ZIP con todos los archivos (partido si excede
    `zip_max_bytes`) y ademas publica las imagenes como album. `base_name` es
    el nombre del ZIP sin extension (default archivos_<fecha>).
    Devuelve (resumen, lista de fallos, indices de `entries` a reintentar)."""
    # Originales + ZIP: hace falta mas o menos el doble del lote.
    ensure_disk_space(2 * sum(file_size(msg) for _, msg, _ in entries))
    work_dir = os.path.join(config.TMP_DIR, uuid.uuid4().hex)
    os.makedirs(work_dir, exist_ok=True)
    total = len(entries)
    downloaded, failed, failed_idx = [], [], []
    index_of = {}
    try:
        for i, (dl_client, msg, label) in enumerate(entries, 1):
            idx = i - 1
            reporter = Reporter(status_msg, f"🗜️ {title} — {i}/{total}: {label}", buttons)
            await reporter.set("iniciando…", force=True)
            # Una carpeta por archivo: evita que dos con el mismo nombre se pisen.
            item_dir = os.path.join(work_dir, str(i))
            os.makedirs(item_dir, exist_ok=True)
            try:
                msg, path = await _download(dl_client, msg, item_dir, reporter.callback("⬇️ descargando"))
                if not path:
                    raise RuntimeError("la descarga no devolvio archivo")
                downloaded.append((msg, Path(path)))
                index_of[Path(path)] = idx
            except Exception as e:
                failed.append(f"{label}: {e}")
                failed_idx.append(idx)

        if not downloaded:
            return f"❌ No se pudo descargar ningún archivo para {title}", failed, failed_idx

        reporter = Reporter(status_msg, f"🗜️ {title}", buttons)
        await reporter.set(f"comprimiendo {len(downloaded)} archivo(s)…", force=True)
        base_name = base_name or "archivos_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        zips = await asyncio.to_thread(build_zips, [p for _, p in downloaded], work_dir,
                                       base_name, zip_max_bytes)

        zips_ok = 0
        for j, (zip_path, group) in enumerate(zips, 1):
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            reporter = Reporter(status_msg, f"🗜️ {title} — {zip_path.name} ({j}/{len(zips)})", buttons)
            try:
                handle = await safe_run(upload_file_fast, up_client, zip_path,
                                        progress_callback=reporter.callback("⬆️ subiendo"))
                await safe_run(up_client.send_file, dest, handle,
                               caption=_zip_caption(names, j, len(zips)), parse_mode=None,
                               reply_to=topic_id, file_size=zip_path.stat().st_size,
                               force_document=True)
                zips_ok += 1
            except Exception as e:
                failed.append(f"{zip_path.name}: {e}")
                failed_idx.extend(index_of[p] for p in group)

        images = [(m, p) for m, p in downloaded if is_album_image(m)]
        album_note = ""
        if images:
            await reporter.set(f"publicando álbum de {len(images)} imagen(es)…", force=True)
            try:
                await _send_album(up_client, dest, images, topic_id)
                album_note = f" + álbum de {len(images)} imagen(es)"
            except Exception as e:
                failed.append(f"álbum de imágenes: {e}")

        zips_note = "ZIP" if len(zips) == 1 else f"{zips_ok}/{len(zips)} ZIP"
        summary = f"✅ {zips_note} con {len(downloaded)}/{total} archivo(s){album_note} → {title}"
        if zips_ok == 0:
            summary = f"❌ No se pudo subir el ZIP a {title}" + (" (sí el álbum)" if album_note else "")
        return summary, failed, sorted(failed_idx)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
