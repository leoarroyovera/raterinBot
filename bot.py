import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault, Channel, Chat

import config
from links import fetch_link_messages, parse_links
from topics import GENERAL_TOPIC_ID, is_forum, list_topics
from transfer import Reporter, has_file, transfer, transfer_zipped

LOG_PATH = os.path.join(config.LOG_DIR, "bot.log")

# Se muestran en el boton "Menu" de Telegram junto al campo de texto.
BOT_COMMANDS = [
    BotCommand("start", "Menú principal y ayuda"),
    BotCommand("cola", "Ver la cola y cancelar envíos en espera"),
    BotCommand("cancelar", "Vaciar la cola en armado"),
    BotCommand("modo", "Alternar modo ZIP / separados (o /modo zip|separado)"),
]


def _log(line: str) -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")
    print(line)


@dataclass
class Item:
    source: str  # "bot" (reenviado/enviado al bot) o "user" (via enlace)
    msg: object
    label: str

    @property
    def key(self):
        if self.source == "bot":
            return ("bot", self.msg.id)
        return ("user", self.msg.chat_id, self.msg.id)

    def to_dict(self) -> dict:
        # Lo minimo para volver a leer el mensaje tras un reinicio.
        return {"source": self.source, "chat_id": self.msg.chat_id,
                "msg_id": self.msg.id, "label": self.label}


@dataclass
class Batch:
    items: list[Item] = field(default_factory=list)
    keys: set = field(default_factory=set)
    prompt_msg: object = None
    prompt_task: asyncio.Task | None = None
    dests: list[tuple[int, str]] = field(default_factory=list)
    # Supergrupo con temas elegido, a la espera de que se elija el tema.
    dest_id: int | None = None
    topics: list[tuple[int, str]] = field(default_factory=list)
    # True: un ZIP con todo + album de imagenes; False: un mensaje por archivo.
    # Solo aplica con mas de un archivo.
    zip_mode: bool = False


@dataclass
class Job:
    """Lote con destino ya elegido: esperando turno o subiendose."""
    id: int
    items: list[Item]
    zip_mode: bool
    dest_id: int
    topic_id: int | None
    title: str
    status_msg: object = None
    running: bool = False
    cancelled: bool = False
    current: int = 0  # archivo que se esta subiendo (modo separado)
    # Progreso del modo separado, persistido para retomar tras un reinicio.
    done: int = 0
    ok: int = 0
    failed: list = field(default_factory=list)  # [(indice, error)]

    def to_dict(self) -> dict:
        return {"id": self.id, "items": [it.to_dict() for it in self.items],
                "zip_mode": self.zip_mode, "dest_id": self.dest_id, "topic_id": self.topic_id,
                "title": self.title, "done": self.done, "ok": self.ok, "failed": self.failed}


@dataclass
class Retry:
    """Archivos que fallaron en un envio, listos para reencolar con un boton."""
    id: int
    items: list[Item]
    zip_mode: bool
    dest_id: int
    topic_id: int | None
    title: str

    def to_dict(self) -> dict:
        return {"id": self.id, "items": [it.to_dict() for it in self.items],
                "zip_mode": self.zip_mode, "dest_id": self.dest_id,
                "topic_id": self.topic_id, "title": self.title}


class RateroBot:
    def __init__(self, bot: TelegramClient, user: TelegramClient):
        self.bot = bot
        self.user = user
        self.zip_default = self._load_zip_default()
        self.batch = self._new_batch()
        self.process_lock = asyncio.Lock()
        self.dest_entities = {}
        self.jobs: list[Job] = []
        self.retries: dict[int, Retry] = {}
        # Un solo contador para jobs y reintentos; se persiste para que los
        # botones viejos ("cj:N", "rf:N") no apunten a otra cosa tras reiniciar.
        self.next_id = 1
        # Mensajes con enlaces que se estan resolviendo (aun no estan en la cola).
        self.resolving = 0

    # ---------- modo de envio (separado / zip) ----------

    @staticmethod
    def _load_zip_default() -> bool:
        mode = config.DEFAULT_GROUP_MODE
        try:
            with open(config.SETTINGS_PATH, "r", encoding="utf-8") as f:
                mode = json.load(f).get("group_mode", mode)
        except (FileNotFoundError, ValueError):
            pass
        return mode == "zip"

    def _save_zip_default(self) -> None:
        with open(config.SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump({"group_mode": "zip" if self.zip_default else "separado"}, f)

    def _new_batch(self) -> Batch:
        return Batch(zip_mode=self.zip_default)

    @staticmethod
    def _mode_label(zip_mode: bool) -> str:
        return "🗜️ ZIP + álbum de imágenes" if zip_mode else "📄 mensajes separados"

    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    # ---------- persistencia de la cola ----------

    def _save_queue(self) -> None:
        """Guarda la cola en armado, los envios pendientes y los reintentos
        disponibles. Escritura atomica para no dejar el archivo a medias."""
        data = {
            "next_id": self.next_id,
            "batch": {"items": [it.to_dict() for it in self.batch.items],
                      "zip_mode": self.batch.zip_mode},
            "jobs": [j.to_dict() for j in self.jobs],
            "retries": [r.to_dict() for r in self.retries.values()],
        }
        tmp = config.QUEUE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, config.QUEUE_PATH)

    async def _load_items(self, dicts: list[dict]) -> list[Item | None]:
        """Vuelve a leer de Telegram los mensajes guardados. Devuelve una
        lista alineada con `dicts`, con None en los que ya no estan."""
        by_chat = {}
        for d in dicts:
            by_chat.setdefault((d["source"], d["chat_id"]), []).append(d["msg_id"])
        found = {}
        for (source, chat_id), ids in by_chat.items():
            client = self.bot if source == "bot" else self.user
            try:
                try:
                    msgs = await client.get_messages(chat_id, ids=ids)
                except ValueError:
                    # Entidad fuera de cache: recorrer dialogos la carga.
                    await client.get_dialogs()
                    msgs = await client.get_messages(chat_id, ids=ids)
            except Exception as e:
                _log(f"  no pude releer mensajes de {chat_id}: {e!r}")
                continue
            for m in msgs:
                if has_file(m):
                    found[(source, chat_id, m.id)] = m
        items = []
        for d in dicts:
            m = found.get((d["source"], d["chat_id"], d["msg_id"]))
            items.append(Item(d["source"], m, d["label"]) if m is not None else None)
        return items

    async def restore(self) -> None:
        """Retoma lo que quedo pendiente antes de un reinicio."""
        try:
            with open(config.QUEUE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError):
            return
        self.next_id = data.get("next_id", 1)
        lost = 0

        b = data.get("batch") or {}
        items = await self._load_items(b.get("items", []))
        lost += items.count(None)
        self.batch.zip_mode = b.get("zip_mode", self.zip_default)
        for it in items:
            if it is not None:
                self._add(it)

        for r in data.get("retries", []):
            r_items = [it for it in await self._load_items(r["items"]) if it is not None]
            if r_items:
                self.retries[r["id"]] = Retry(r["id"], r_items, r["zip_mode"], r["dest_id"],
                                              r["topic_id"], r["title"])

        pending = []
        for j in data.get("jobs", []):
            aligned = await self._load_items(j["items"])
            lost += aligned.count(None)
            # Se quitan los perdidos y se reajustan los indices guardados a la lista nueva.
            new_index, j_items = {}, []
            for old, it in enumerate(aligned):
                if it is not None:
                    new_index[old] = len(j_items)
                    j_items.append(it)
            done = sum(1 for old in range(j["done"]) if old in new_index)
            ok = j["ok"]
            failed = [(new_index[old], err) for old, err in j["failed"] if old in new_index]
            if j_items and done < len(j_items):
                pending.append((j, j_items, done, ok, failed))

        # El aviso va antes que los mensajes "En espera" de los envios retomados.
        if pending or self.batch.items or lost:
            text = (f"♻️ Bot reiniciado: retomo {len(pending)} envío(s) pendiente(s) y "
                    f"{len(self.batch.items)} archivo(s) en cola.")
            if lost:
                text += f"\n⚠️ {lost} archivo(s) ya no estaban disponibles y se descartaron."
            _log(text)
            await self.bot.send_message(config.OWNER_ID, text)
        for j, j_items, done, ok, failed in pending:
            await self._enqueue(j_items, j["zip_mode"], j["dest_id"], j["topic_id"], j["title"],
                                done=done, ok=ok, failed=failed, job_id=j["id"])
        self._save_queue()
        if self.batch.items:
            self._schedule_prompt()

    # ---------- recepcion ----------

    def _add(self, item: Item) -> bool:
        if item.key in self.batch.keys:
            return False
        self.batch.keys.add(item.key)
        self.batch.items.append(item)
        return True

    async def on_message(self, event):
        msg = event.message
        if not has_file(msg) and msg.raw_text.startswith("/"):
            return

        added = 0
        errors = []
        if has_file(msg):
            label = msg.file.name or ("foto" if msg.photo else msg.file.mime_type or "archivo")
            added += self._add(Item("bot", msg, label))
        else:
            links = parse_links(msg.raw_text)
            if not links:
                await event.reply("No encontré archivos ni enlaces de mensajes (t.me/...) acá.")
                return
            self.resolving += 1
            try:
                for link in links:
                    try:
                        found = await fetch_link_messages(self.user, link)
                    except Exception as e:
                        errors.append(f"{link.url}: {e}")
                        continue
                    files = [m for m in found if has_file(m)]
                    if not files:
                        errors.append(f"{link.url}: no tiene archivo o no tengo acceso")
                        continue
                    for m in files:
                        label = m.file.name or ("foto" if m.photo else m.file.mime_type or "archivo")
                        added += self._add(Item("user", m, label))
            finally:
                self.resolving -= 1

        if errors:
            await event.reply("No pude agregar:\n" + "\n".join(errors))
        if added:
            self._save_queue()
            self._schedule_prompt()

    # ---------- seleccion de destino ----------

    def _schedule_prompt(self):
        if self.batch.prompt_task and not self.batch.prompt_task.done():
            self.batch.prompt_task.cancel()
        self.batch.prompt_task = asyncio.create_task(self._prompt_after_debounce(self.batch))

    async def _prompt_after_debounce(self, batch: Batch):
        await asyncio.sleep(config.BATCH_DEBOUNCE_S)
        if batch is not self.batch or not batch.items:
            return
        if not batch.dests:
            batch.dests = await self._list_dests()
        if batch.prompt_msg:
            try:
                await batch.prompt_msg.delete()
            except Exception:
                pass
        if not batch.dests:
            batch.prompt_msg = await self.bot.send_message(
                config.OWNER_ID, "No encontré grupos destino. Revisá SOLO_GRUPOS_PROPIOS en .env.")
            return
        batch.prompt_msg = await self.bot.send_message(
            config.OWNER_ID, self._prompt_text(batch), buttons=self._dest_buttons(batch, 0))

    def _prompt_text(self, batch: Batch, question="¿A qué grupo los envío?") -> str:
        lines = [f"📦 {len(batch.items)} archivo(s) en cola:"]
        for i, it in enumerate(batch.items[:15], 1):
            lines.append(f"  {i}. {it.label}")
        if len(batch.items) > 15:
            lines.append(f"  … y {len(batch.items) - 15} más")
        if len(batch.items) > 1:
            lines.append(f"\nModo: {self._mode_label(batch.zip_mode)}")
        lines.append(f"\n{question}")
        return "\n".join(lines)

    def _dest_buttons(self, batch: Batch, page: int):
        return self._paged_buttons(batch, batch.dests, page, select="d", nav="p")

    def _topic_buttons(self, batch: Batch, page: int):
        return self._paged_buttons(batch, batch.topics, page, select="t", nav="tp", back=True)

    def _paged_buttons(self, batch, entries, page, select, nav, back=False):
        per = config.DESTS_PER_PAGE
        chunk = entries[page * per:(page + 1) * per]
        rows = [[Button.inline(title[:60], f"{select}:{key}".encode())] for key, title in chunk]
        nav_row = []
        if page > 0:
            nav_row.append(Button.inline("◀️", f"{nav}:{page - 1}".encode()))
        if (page + 1) * per < len(entries):
            nav_row.append(Button.inline("▶️", f"{nav}:{page + 1}".encode()))
        if nav_row:
            rows.append(nav_row)
        if len(batch.items) > 1:
            other = "🗜️ Enviar como ZIP" if not batch.zip_mode else "📄 Enviar separados"
            # El menu y la pagina viajan en el callback para no perder la posicion.
            rows.append([Button.inline(f"Cambiar a: {other}", f"m:{nav}:{page}".encode())])
        last = [Button.inline("❌ Cancelar", b"x")]
        if back:
            last.insert(0, Button.inline("↩️ Otro grupo", b"b"))
        rows.append(last)
        return rows

    async def _list_dests(self) -> list[tuple[int, str]]:
        owned, others = [], []
        async for d in self.user.iter_dialogs():
            e = d.entity
            if isinstance(e, Chat):
                if e.deactivated or e.left:
                    continue
                is_owned = e.creator or bool(e.admin_rights)
            elif isinstance(e, Channel):
                if e.left:
                    continue
                is_owned = e.creator or bool(e.admin_rights)
                if e.broadcast and not (e.creator or (e.admin_rights and e.admin_rights.post_messages)):
                    continue
            else:
                continue
            if config.ONLY_OWNED_DESTS and not is_owned:
                continue
            self.dest_entities[d.id] = e
            (owned if is_owned else others).append((d.id, d.name))
        return owned + others

    async def on_callback(self, event):
        data = event.data.decode()
        # Botones que no dependen del menu de destino activo.
        if data.startswith("cj:"):
            await self._cancel_job(event, int(data[3:]))
            return
        if data.startswith("cmd:"):
            await self._run_menu_command(event, data[4:])
            return
        if data.startswith("rf:"):
            await self._retry_failed(event, int(data[3:]))
            return

        batch = self.batch
        if event.message_id != getattr(batch.prompt_msg, "id", None):
            await event.answer("Este menú ya no está activo.")
            return

        if data == "x":
            n = len(batch.items)
            self.batch = self._new_batch()
            self._save_queue()
            await event.edit(f"Cancelado. Descarté {n} archivo(s).")
            return
        if data.startswith("p:"):
            await event.edit(buttons=self._dest_buttons(batch, int(data[2:])))
            return
        if data.startswith("m:"):
            _, nav, page = data.split(":")
            batch.zip_mode = not batch.zip_mode
            self._save_queue()
            if nav == "tp":
                title = dict(batch.dests).get(batch.dest_id, str(batch.dest_id))
                await event.edit(self._prompt_text(batch, f"Grupo: {title}\n¿En qué tema?"),
                                 buttons=self._topic_buttons(batch, int(page)))
            else:
                await event.edit(self._prompt_text(batch), buttons=self._dest_buttons(batch, int(page)))
            return
        if data.startswith("tp:"):
            await event.edit(buttons=self._topic_buttons(batch, int(data[3:])))
            return
        if data == "b":
            batch.dest_id, batch.topics = None, []
            await event.edit(self._prompt_text(batch), buttons=self._dest_buttons(batch, 0))
            return
        if data.startswith("d:"):
            dest_id = int(data[2:])
            title = dict(batch.dests).get(dest_id, str(dest_id))
            dest = self.dest_entities.get(dest_id) or await self.user.get_entity(dest_id)
            if is_forum(dest):
                await event.answer()
                batch.dest_id = dest_id
                batch.topics = await list_topics(self.user, dest)
                await event.edit(self._prompt_text(batch, f"Grupo: {title}\n¿En qué tema?"),
                                 buttons=self._topic_buttons(batch, 0))
                return
            await self._start_send(event, batch, dest_id, title)
            return
        if data.startswith("t:") and batch.dest_id is not None:
            topic_id = int(data[2:])
            title = dict(batch.dests).get(batch.dest_id, str(batch.dest_id))
            title += " › " + dict(batch.topics).get(topic_id, str(topic_id))
            await self._start_send(event, batch, batch.dest_id, title, topic_id)

    async def _start_send(self, event, batch: Batch, dest_id: int, title: str, topic_id=None):
        # A partir de aca lo que llegue va a una cola nueva.
        self.batch = self._new_batch()
        await event.answer(f"Enviando a {title}")
        mode = f" ({self._mode_label(batch.zip_mode)})" if len(batch.items) > 1 else ""
        await event.edit(f"📤 {len(batch.items)} archivo(s) → {title}{mode}", buttons=None)
        # El tema General no admite reply_to: se publica sin tema.
        reply_to = None if topic_id in (None, GENERAL_TOPIC_ID) else topic_id
        await self._enqueue(batch.items, batch.zip_mode, dest_id, reply_to, title)

    async def _enqueue(self, items, zip_mode, dest_id, topic_id, title, done=0, ok=0,
                       failed=None, job_id=None) -> Job:
        job = Job(job_id or self._new_id(), items, zip_mode, dest_id, topic_id, title,
                  done=done, ok=ok, failed=failed or [])
        n = len(items) - done
        job.status_msg = await self.bot.send_message(
            config.OWNER_ID, f"⏳ En espera: {n} archivo(s) → {title}",
            buttons=[[Button.inline("❌ Cancelar este envío", f"cj:{job.id}".encode())]])
        # Se registra ya (no dentro de la tarea) para que /cola lo vea de inmediato.
        self.jobs.append(job)
        self._save_queue()
        asyncio.create_task(self._process(job))
        return job

    # ---------- procesamiento ----------

    async def _process(self, job: Job):
        try:
            await self._process_job(job)
        except Exception as e:
            # Error fuera de un archivo puntual (p.ej. destino inaccesible):
            # se reporta y el lote completo queda para reintentar.
            _log(f"ERROR envio -> {job.title}: {e!r}")
            await self._finish(job, f"❌ Error enviando a {job.title}: {e}", [],
                               list(range(len(job.items))))
        finally:
            if job in self.jobs:
                self.jobs.remove(job)
                self._save_queue()

    async def _process_job(self, job: Job):
        title, status = job.title, job.status_msg
        async with self.process_lock:
            if job.cancelled:
                return
            job.running = True
            # Ya no se puede cancelar: se quita el boton.
            await status.edit(f"📤 {title}\niniciando…", buttons=None)
            dest = self.dest_entities.get(job.dest_id) or await self.user.get_entity(job.dest_id)
            total = len(job.items)
            if job.zip_mode and total > 1:
                await self._process_zip(job, dest)
                return
            _log(f"INICIO {total} archivo(s) -> {title} ({job.dest_id})"
                 + (f" (retomando desde {job.done + 1})" if job.done else ""))
            for i, item in enumerate(job.items, 1):
                if i <= job.done:
                    continue
                job.current = i
                reporter = Reporter(status, f"📤 {title} — {i}/{total}: {item.label}")
                await reporter.set("iniciando…", force=True)
                dl_client = self.bot if item.source == "bot" else self.user
                try:
                    name = await transfer(dl_client, self.user, item.msg, dest, reporter, job.topic_id)
                    job.ok += 1
                    _log(f"  OK {name}")
                except Exception as e:
                    job.failed.append((i - 1, str(e)))
                    _log(f"  ERROR {item.label}: {e!r}")
                job.done = i
                self._save_queue()

            _log(f"FIN {job.ok}/{total} -> {title}")
            errors = [f"{job.items[idx].label}: {err}" for idx, err in job.failed]
            await self._finish(job, f"✅ {job.ok}/{total} enviado(s) a {title}", errors,
                               [idx for idx, _ in job.failed])

    async def _process_zip(self, job: Job, dest):
        _log(f"INICIO ZIP {len(job.items)} archivo(s) -> {job.title} ({job.dest_id})")
        entries = [(self.bot if it.source == "bot" else self.user, it.msg, it.label) for it in job.items]
        summary, errors, failed_idx = await transfer_zipped(
            entries, self.user, dest, job.status_msg, job.title, job.topic_id)
        for f in errors:
            _log(f"  ERROR {f}")
        _log(f"FIN ZIP -> {job.title}: {summary}")
        await self._finish(job, summary, errors, failed_idx)

    async def _finish(self, job: Job, summary: str, errors: list[str], failed_idx: list[int]):
        """Edita el mensaje de estado con el resumen y, si hubo archivos que
        fallaron, agrega un boton para reintentarlos."""
        if errors:
            summary += "\n\n❌ Fallaron:\n" + "\n".join(errors)
        if len(summary) > 4000:  # limite de texto de Telegram (4096)
            summary = summary[:3999] + "…"
        buttons = None
        if failed_idx:
            retry = Retry(self._new_id(), [job.items[i] for i in failed_idx], job.zip_mode,
                          job.dest_id, job.topic_id, job.title)
            self.retries[retry.id] = retry
            # Solo se conservan los ultimos reintentos para no crecer sin limite.
            for old in list(self.retries)[:-config.MAX_RETRIES_KEPT]:
                del self.retries[old]
            self._save_queue()
            buttons = [[Button.inline(f"🔁 Reintentar fallidos ({len(retry.items)})",
                                      f"rf:{retry.id}".encode())]]
        await job.status_msg.edit(summary, buttons=buttons)

    async def _retry_failed(self, event, retry_id: int):
        retry = self.retries.pop(retry_id, None)
        if retry is None:
            await event.answer("Ese reintento ya no está disponible.")
            return
        await event.answer(f"Reintentando {len(retry.items)} archivo(s)")
        msg = await event.get_message()
        await event.edit(msg.text + "\n\n🔁 Reintento encolado.", buttons=None)
        await self._enqueue(retry.items, retry.zip_mode, retry.dest_id, retry.topic_id, retry.title)

    # ---------- cancelar envios en espera ----------

    def _queue_view(self):
        """Texto y botones de /cola para los lotes con destino ya elegido, o
        (None, None) si no hay ninguno."""
        lines, buttons = [], []
        for job in self.jobs:
            n = len(job.items)
            if job.running:
                prog = f" {job.current}/{n}" if job.current else ""
                lines.append(f"📤 Subiendo{prog}: {n} archivo(s) → {job.title}")
            else:
                lines.append(f"⏳ En espera: {n} archivo(s) → {job.title}")
                buttons.append([Button.inline(f"❌ Cancelar → {job.title}"[:60], f"cj:{job.id}".encode())])
        if self.resolving:
            lines.append(f"🔎 Resolviendo {self.resolving} mensaje(s) con enlaces…")
        if not lines:
            return None, None
        return "\n".join(lines), buttons or None

    async def _cancel_job(self, event, job_id: int):
        job = next((j for j in self.jobs if j.id == job_id), None)
        if job is None:
            await event.answer("Ese envío ya terminó o fue cancelado.")
        elif job.running:
            await event.answer("Ese envío ya se está subiendo; no se puede cancelar.", alert=True)
        else:
            job.cancelled = True
            self.jobs.remove(job)
            self._save_queue()
            _log(f"CANCELADO {len(job.items)} archivo(s) -> {job.title}")
            await event.answer("Envío cancelado")
            try:
                await job.status_msg.edit(
                    f"🚫 Cancelado: {len(job.items)} archivo(s) → {job.title}", buttons=None)
            except MessageNotModifiedError:
                pass
        # Si se toco desde el listado de /cola, se refresca ese listado.
        if job is None or event.message_id != job.status_msg.id:
            text, buttons = self._queue_view()
            try:
                await event.edit(text or "No hay envíos pendientes.", buttons=buttons)
            except MessageNotModifiedError:
                pass

    # ---------- comandos ----------

    def _help_text(self) -> str:
        return (
            "Reenviame archivos (fotos, videos, documentos…) o pegame enlaces "
            "de mensajes t.me/... — uno o varios. Cuando dejes de mandar, te "
            "pregunto a qué grupo subirlos y van todos al mismo.\n\n"
            "/cola — ver la cola y cancelar envíos en espera\n"
            "/cancelar — vaciar la cola en armado\n"
            "/modo [zip|separado] — cómo enviar varios archivos juntos\n\n"
            f"Modo actual: {self._mode_label(self.zip_default)}"
        )

    def _menu_buttons(self):
        other = "📄 separados" if self.zip_default else "🗜️ ZIP"
        return [
            [Button.inline("📋 Ver cola", b"cmd:cola"), Button.inline("🗑️ Vaciar cola", b"cmd:cancelar")],
            [Button.inline(f"🔀 Cambiar modo a {other}", b"cmd:modo")],
        ]

    async def _run_menu_command(self, event, name: str):
        if name == "cola":
            await event.answer()
            await self.on_queue(event)
        elif name == "cancelar":
            await event.answer()
            await self.on_cancel(event)
        elif name == "modo":
            await event.answer(self._set_mode(""))
            await event.edit(self._help_text(), buttons=self._menu_buttons())
        else:
            await event.answer("Opción desconocida.")

    async def on_start(self, event):
        await event.respond(self._help_text(), buttons=self._menu_buttons())

    def _set_mode(self, arg: str) -> str | None:
        """Cambia el modo global ('' alterna). Devuelve el texto de
        confirmacion, o None si el argumento no es valido."""
        if arg in ("zip", "comprimido"):
            self.zip_default = True
        elif arg in ("separado", "separados"):
            self.zip_default = False
        elif not arg:
            self.zip_default = not self.zip_default
        else:
            return None
        self._save_zip_default()
        # La cola en armado adopta el nuevo modo y se refresca su menu.
        if self.batch.items:
            self.batch.zip_mode = self.zip_default
            self._save_queue()
            self._schedule_prompt()
        return f"Modo por defecto para varios archivos: {self._mode_label(self.zip_default)}"

    async def on_mode(self, event):
        text = self._set_mode((event.pattern_match.group(1) or "").strip().lower())
        await event.respond(text or "Uso: /modo zip | /modo separado (sin argumento alterna)")

    async def on_queue(self, event):
        # Lotes que ya tienen destino: el que se esta subiendo y los que esperan turno.
        text, buttons = self._queue_view()
        if text:
            await event.respond(text, buttons=buttons)
        if self.batch.items:
            # El lote en armado (sin destino aun) se muestra con su menu.
            self._schedule_prompt()
        elif not text:
            await event.respond("La cola está vacía.")

    async def on_cancel(self, event):
        n = len(self.batch.items)
        if self.batch.prompt_task:
            self.batch.prompt_task.cancel()
        if self.batch.prompt_msg:
            try:
                await self.batch.prompt_msg.delete()
            except Exception:
                pass
        self.batch = self._new_batch()
        self._save_queue()
        await event.respond(f"Cola vaciada ({n} archivo(s) descartados).")

    def register(self):
        owner = events.NewMessage(from_users=config.OWNER_ID, incoming=True, func=lambda e: e.is_private)
        self.bot.add_event_handler(self.on_start, events.NewMessage(
            from_users=config.OWNER_ID, pattern=r"^/(start|help)\b"))
        self.bot.add_event_handler(self.on_queue, events.NewMessage(
            from_users=config.OWNER_ID, pattern=r"^/cola\b"))
        self.bot.add_event_handler(self.on_cancel, events.NewMessage(
            from_users=config.OWNER_ID, pattern=r"^/cancelar\b"))
        self.bot.add_event_handler(self.on_mode, events.NewMessage(
            from_users=config.OWNER_ID, pattern=r"^/modo\b(.*)"))
        self.bot.add_event_handler(self.on_message, owner)
        self.bot.add_event_handler(
            self.on_callback, events.CallbackQuery(func=lambda e: e.sender_id == config.OWNER_ID))


async def main() -> None:
    if not config.BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN debe estar definido en .env")
    if config.OWNER_ID is None:
        raise RuntimeError("TG_OWNER_ID debe estar definido en .env para restringir el bot a tu cuenta")
    if not os.path.exists(config.USER_SESSION_NAME + ".session"):
        raise RuntimeError("Falta la sesion de usuario: corre primero `python login.py`")

    os.makedirs(config.TMP_DIR, exist_ok=True)
    user = TelegramClient(config.USER_SESSION_NAME, config.API_ID, config.API_HASH)
    bot = TelegramClient(config.BOT_SESSION_NAME, config.API_ID, config.API_HASH)

    await user.connect()
    if not await user.is_user_authorized():
        raise RuntimeError("La sesion de usuario no esta autorizada: corre `python login.py`")
    await bot.start(bot_token=config.BOT_TOKEN)

    try:
        await bot(SetBotCommandsRequest(scope=BotCommandScopeDefault(), lang_code="", commands=BOT_COMMANDS))
    except Exception as e:
        _log(f"No pude registrar el menu de comandos: {e!r}")

    try:
        ratero = RateroBot(bot, user)
        ratero.register()
        _log("Bot iniciado")
        await ratero.restore()
        await bot.run_until_disconnected()
    finally:
        await user.disconnect()
        await bot.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
