import asyncio
import itertools
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
    batch: Batch
    title: str
    status_msg: object = None
    running: bool = False
    cancelled: bool = False
    current: int = 0  # archivo que se esta subiendo (modo separado)


class RateroBot:
    def __init__(self, bot: TelegramClient, user: TelegramClient):
        self.bot = bot
        self.user = user
        self.zip_default = self._load_zip_default()
        self.batch = self._new_batch()
        self.process_lock = asyncio.Lock()
        self.dest_entities = {}
        self.jobs: list[Job] = []
        self.job_ids = itertools.count(1)
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

    # ---------- recepcion ----------

    def _add(self, item: Item, key) -> bool:
        if key in self.batch.keys:
            return False
        self.batch.keys.add(key)
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
            added += self._add(Item("bot", msg, label), ("bot", msg.id))
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
                        added += self._add(Item("user", m, label), ("user", m.chat_id, m.id))
            finally:
                self.resolving -= 1

        if errors:
            await event.reply("No pude agregar:\n" + "\n".join(errors))
        if added:
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

        batch = self.batch
        if event.message_id != getattr(batch.prompt_msg, "id", None):
            await event.answer("Este menú ya no está activo.")
            return

        if data == "x":
            n = len(batch.items)
            self.batch = self._new_batch()
            await event.edit(f"Cancelado. Descarté {n} archivo(s).")
            return
        if data.startswith("p:"):
            await event.edit(buttons=self._dest_buttons(batch, int(data[2:])))
            return
        if data.startswith("m:"):
            _, nav, page = data.split(":")
            batch.zip_mode = not batch.zip_mode
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
        job = Job(next(self.job_ids), batch, title)
        job.status_msg = await self.bot.send_message(
            config.OWNER_ID, f"⏳ En espera: {len(batch.items)} archivo(s) → {title}",
            buttons=[[Button.inline("❌ Cancelar este envío", f"cj:{job.id}".encode())]])
        # Se registra ya (no dentro de la tarea) para que /cola lo vea de inmediato.
        self.jobs.append(job)
        asyncio.create_task(self._process(job, dest_id, reply_to))

    # ---------- procesamiento ----------

    async def _process(self, job: Job, dest_id: int, topic_id=None):
        try:
            await self._process_job(job, dest_id, topic_id)
        finally:
            if job in self.jobs:
                self.jobs.remove(job)

    async def _process_job(self, job: Job, dest_id: int, topic_id=None):
        batch, title, status = job.batch, job.title, job.status_msg
        async with self.process_lock:
            if job.cancelled:
                return
            job.running = True
            # Ya no se puede cancelar: se quita el boton.
            await status.edit(f"📤 {title}\niniciando…", buttons=None)
            dest = self.dest_entities.get(dest_id) or await self.user.get_entity(dest_id)
            total = len(batch.items)
            if batch.zip_mode and total > 1:
                await self._process_zip(batch, dest, dest_id, title, topic_id, status)
                return
            ok, failed = 0, []
            _log(f"INICIO {total} archivo(s) -> {title} ({dest_id})")
            for i, item in enumerate(batch.items, 1):
                job.current = i
                reporter = Reporter(status, f"📤 {title} — {i}/{total}: {item.label}")
                await reporter.set("iniciando…", force=True)
                dl_client = self.bot if item.source == "bot" else self.user
                try:
                    name = await transfer(dl_client, self.user, item.msg, dest, reporter, topic_id)
                    ok += 1
                    _log(f"  OK {name}")
                except Exception as e:
                    failed.append(f"{item.label}: {e}")
                    _log(f"  ERROR {item.label}: {e!r}")

            summary = f"✅ {ok}/{total} enviado(s) a {title}"
            if failed:
                summary += "\n\n❌ Fallaron:\n" + "\n".join(failed)
            _log(f"FIN {ok}/{total} -> {title}")
            await status.edit(summary)

    async def _process_zip(self, batch: Batch, dest, dest_id: int, title: str, topic_id, status):
        _log(f"INICIO ZIP {len(batch.items)} archivo(s) -> {title} ({dest_id})")
        entries = [(self.bot if it.source == "bot" else self.user, it.msg, it.label) for it in batch.items]
        try:
            summary, failed = await transfer_zipped(entries, self.user, dest, status, title, topic_id)
        except Exception as e:
            summary, failed = f"❌ Error enviando ZIP a {title}", [str(e)]
        for f in failed:
            _log(f"  ERROR {f}")
        _log(f"FIN ZIP -> {title}: {summary}")
        if failed:
            summary += "\n\n❌ Fallaron:\n" + "\n".join(failed)
        await status.edit(summary)

    # ---------- cancelar envios en espera ----------

    def _queue_view(self):
        """Texto y botones de /cola para los lotes con destino ya elegido, o
        (None, None) si no hay ninguno."""
        lines, buttons = [], []
        for job in self.jobs:
            n = len(job.batch.items)
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
            _log(f"CANCELADO {len(job.batch.items)} archivo(s) -> {job.title}")
            await event.answer("Envío cancelado")
            try:
                await job.status_msg.edit(
                    f"🚫 Cancelado: {len(job.batch.items)} archivo(s) → {job.title}", buttons=None)
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
        RateroBot(bot, user).register()
        _log("Bot iniciado")
        await bot.run_until_disconnected()
    finally:
        await user.disconnect()
        await bot.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
