"""Descarga de un mensaje con media y re-subida a un chat destino."""
import asyncio
import os
import shutil
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from telethon import errors
from telethon.tl.types import MessageMediaWebPage

import config
from fast_upload import upload_file_fast

MAX_CAPTION = 1024
ALBUM_SIZE = 10  # maximo de fotos por album en Telegram
MAX_PHOTO_BYTES = 10 * 1024 * 1024  # mayores no se aceptan como foto
# Margen por archivo para cabeceras ZIP/ZIP64 al calcular el tamano de cada parte.
ZIP_ENTRY_OVERHEAD = 1024


def has_file(msg) -> bool:
    return msg is not None and msg.file is not None and not isinstance(msg.media, MessageMediaWebPage)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024


async def safe_run(coro_fn, *args, max_retries=5, **kwargs):
    for attempt in range(1, max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except errors.FloodWaitError as e:
            print(f"  [FloodWait] esperando {e.seconds + 1}s (intento {attempt}/{max_retries})")
            await asyncio.sleep(e.seconds + 1)
    raise RuntimeError(f"Excedido max_retries ({max_retries}) tras FloodWait repetido")


class Reporter:
    """Edita un mensaje de estado del bot con el progreso, como mucho una
    vez cada `interval` segundos para no chocar con FloodWait."""

    def __init__(self, status_msg, header, interval=4.0):
        self.status_msg = status_msg
        self.header = header
        self.interval = interval
        self.last = 0.0
        self.last_text = None

    async def set(self, text, force=False):
        now = time.monotonic()
        if not force and now - self.last < self.interval:
            return
        full = f"{self.header}\n{text}"
        if full == self.last_text:
            return
        self.last, self.last_text = now, full
        try:
            await self.status_msg.edit(full)
        except (errors.MessageNotModifiedError, errors.FloodWaitError):
            pass

    def callback(self, verb):
        start = time.monotonic()

        async def cb(done, total):
            span = max(time.monotonic() - start, 1e-6)
            pct = f" ({done * 100 / total:.0f}%)" if total else ""
            await self.set(f"{verb}: {human(done)} / {human(total or 0)}{pct} a {human(done / span)}/s")

        return cb


async def _refetch(client, msg):
    chat = await msg.get_input_chat()
    return await client.get_messages(chat, ids=msg.id)


async def _download(client, msg, dest_dir, progress_cb):
    try:
        return msg, await safe_run(client.download_media, msg, file=dest_dir + os.sep,
                                   progress_callback=progress_cb)
    except errors.FileReferenceExpiredError:
        # La referencia del archivo vence (~1h): se relee el mensaje.
        msg = await _refetch(client, msg)
        if not has_file(msg):
            raise RuntimeError("el mensaje ya no esta disponible")
        return msg, await safe_run(client.download_media, msg, file=dest_dir + os.sep,
                                   progress_callback=progress_cb)


def _is_media_type(msg) -> bool:
    """True si en origen se mostraba como media (foto/video/audio/gif) y no
    como archivo adjunto generico."""
    return bool(msg.photo or msg.video or msg.gif or msg.audio or msg.voice or msg.video_note)


async def transfer(dl_client, up_client, msg, dest, reporter: Reporter, topic_id=None):
    """Descarga `msg` con `dl_client` y lo sube a `dest` con `up_client`.
    `topic_id` es el tema destino en supergrupos con modo Foro (None = General)."""
    work_dir = os.path.join(config.TMP_DIR, uuid.uuid4().hex)
    os.makedirs(work_dir, exist_ok=True)
    try:
        msg, path = await _download(dl_client, msg, work_dir, reporter.callback("⬇️ descargando"))
        if not path:
            raise RuntimeError("la descarga no devolvio archivo")
        path = Path(path)

        thumb = None
        if msg.document and msg.document.thumbs:
            try:
                thumb = await dl_client.download_media(msg, file=os.path.join(work_dir, "thumb.jpg"), thumb=-1)
            except Exception:
                thumb = None

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
            kwargs["thumb"] = thumb
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
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


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


def build_zips(files: list[Path], out_dir: str, base_name: str, max_bytes: int) -> list[Path]:
    """Empaqueta `files` (en orden) en uno o mas ZIP sin compresion (los
    archivos multimedia ya vienen comprimidos), partiendo cuando una parte
    superaria `max_bytes`. Es bloqueante: llamar con asyncio.to_thread."""
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
        zips.append(zip_path)
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


async def transfer_zipped(entries, up_client, dest, status_msg, title, topic_id=None):
    """Modo ZIP: descarga todos los mensajes de `entries` [(dl_client, msg,
    label)], sube un ZIP con todos los archivos (partido si excede
    config.ZIP_MAX_MB) y ademas publica las imagenes como album.
    Devuelve (resumen, lista de fallos)."""
    work_dir = os.path.join(config.TMP_DIR, uuid.uuid4().hex)
    os.makedirs(work_dir, exist_ok=True)
    total = len(entries)
    downloaded, failed = [], []
    try:
        for i, (dl_client, msg, label) in enumerate(entries, 1):
            reporter = Reporter(status_msg, f"🗜️ {title} — {i}/{total}: {label}")
            await reporter.set("iniciando…", force=True)
            # Una carpeta por archivo: evita que dos con el mismo nombre se pisen.
            item_dir = os.path.join(work_dir, str(i))
            os.makedirs(item_dir, exist_ok=True)
            try:
                msg, path = await _download(dl_client, msg, item_dir, reporter.callback("⬇️ descargando"))
                if not path:
                    raise RuntimeError("la descarga no devolvio archivo")
                downloaded.append((msg, Path(path)))
            except Exception as e:
                failed.append(f"{label}: {e}")

        if not downloaded:
            return f"❌ No se pudo descargar ningún archivo para {title}", failed

        reporter = Reporter(status_msg, f"🗜️ {title}")
        await reporter.set(f"comprimiendo {len(downloaded)} archivo(s)…", force=True)
        base_name = "archivos_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        zips = await asyncio.to_thread(build_zips, [p for _, p in downloaded], work_dir,
                                       base_name, config.ZIP_MAX_MB * 1024 * 1024)

        zips_ok = 0
        for j, zip_path in enumerate(zips, 1):
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            reporter = Reporter(status_msg, f"🗜️ {title} — {zip_path.name} ({j}/{len(zips)})")
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
        return summary, failed
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
