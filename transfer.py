"""Descarga de un mensaje con media y re-subida a un chat destino."""
import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path

from telethon import errors
from telethon.tl.types import MessageMediaWebPage

import config
from fast_upload import upload_file_fast

MAX_CAPTION = 1024


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
