import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime

from telethon import Button, TelegramClient, events
from telethon.tl.types import Channel, Chat

import config
from links import fetch_link_messages, parse_links
from transfer import Reporter, has_file, transfer

LOG_PATH = os.path.join(config.LOG_DIR, "bot.log")


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


class RateroBot:
    def __init__(self, bot: TelegramClient, user: TelegramClient):
        self.bot = bot
        self.user = user
        self.batch = Batch()
        self.process_lock = asyncio.Lock()
        self.dest_entities = {}

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

    def _prompt_text(self, batch: Batch) -> str:
        lines = [f"📦 {len(batch.items)} archivo(s) en cola:"]
        for i, it in enumerate(batch.items[:15], 1):
            lines.append(f"  {i}. {it.label}")
        if len(batch.items) > 15:
            lines.append(f"  … y {len(batch.items) - 15} más")
        lines.append("\n¿A qué grupo los envío?")
        return "\n".join(lines)

    def _dest_buttons(self, batch: Batch, page: int):
        per = config.DESTS_PER_PAGE
        chunk = batch.dests[page * per:(page + 1) * per]
        rows = [[Button.inline(title[:60], f"d:{dest_id}".encode())] for dest_id, title in chunk]
        nav = []
        if page > 0:
            nav.append(Button.inline("◀️", f"p:{page - 1}".encode()))
        if (page + 1) * per < len(batch.dests):
            nav.append(Button.inline("▶️", f"p:{page + 1}".encode()))
        if nav:
            rows.append(nav)
        rows.append([Button.inline("❌ Cancelar", b"x")])
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
        batch = self.batch
        if event.message_id != getattr(batch.prompt_msg, "id", None):
            await event.answer("Este menú ya no está activo.")
            return

        if data == "x":
            n = len(batch.items)
            self.batch = Batch()
            await event.edit(f"Cancelado. Descarté {n} archivo(s).")
            return
        if data.startswith("p:"):
            await event.edit(buttons=self._dest_buttons(batch, int(data[2:])))
            return
        if data.startswith("d:"):
            dest_id = int(data[2:])
            title = dict(batch.dests).get(dest_id, str(dest_id))
            # A partir de aca lo que llegue va a una cola nueva.
            self.batch = Batch()
            await event.answer(f"Enviando a {title}")
            await event.edit(f"📤 {len(batch.items)} archivo(s) → {title}", buttons=None)
            asyncio.create_task(self._process(batch, dest_id, title))

    # ---------- procesamiento ----------

    async def _process(self, batch: Batch, dest_id: int, title: str):
        status = await self.bot.send_message(config.OWNER_ID, "⏳ En espera…")
        async with self.process_lock:
            dest = self.dest_entities.get(dest_id) or await self.user.get_entity(dest_id)
            total = len(batch.items)
            ok, failed = 0, []
            _log(f"INICIO {total} archivo(s) -> {title} ({dest_id})")
            for i, item in enumerate(batch.items, 1):
                reporter = Reporter(status, f"📤 {title} — {i}/{total}: {item.label}")
                await reporter.set("iniciando…", force=True)
                dl_client = self.bot if item.source == "bot" else self.user
                try:
                    name = await transfer(dl_client, self.user, item.msg, dest, reporter)
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

    # ---------- comandos ----------

    async def on_start(self, event):
        await event.respond(
            "Reenviame archivos (fotos, videos, documentos…) o pegame enlaces "
            "de mensajes t.me/... — uno o varios. Cuando dejes de mandar, te "
            "pregunto a qué grupo subirlos y van todos al mismo.\n\n"
            "/cola — ver lo que hay en cola\n"
            "/cancelar — vaciar la cola"
        )

    async def on_queue(self, event):
        if not self.batch.items:
            await event.respond("La cola está vacía.")
            return
        self._schedule_prompt()

    async def on_cancel(self, event):
        n = len(self.batch.items)
        if self.batch.prompt_task:
            self.batch.prompt_task.cancel()
        if self.batch.prompt_msg:
            try:
                await self.batch.prompt_msg.delete()
            except Exception:
                pass
        self.batch = Batch()
        await event.respond(f"Cola vaciada ({n} archivo(s) descartados).")

    def register(self):
        owner = events.NewMessage(from_users=config.OWNER_ID, incoming=True, func=lambda e: e.is_private)
        self.bot.add_event_handler(self.on_start, events.NewMessage(
            from_users=config.OWNER_ID, pattern=r"^/(start|help)\b"))
        self.bot.add_event_handler(self.on_queue, events.NewMessage(
            from_users=config.OWNER_ID, pattern=r"^/cola\b"))
        self.bot.add_event_handler(self.on_cancel, events.NewMessage(
            from_users=config.OWNER_ID, pattern=r"^/cancelar\b"))
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
        RateroBot(bot, user).register()
        _log("Bot iniciado")
        await bot.run_until_disconnected()
    finally:
        await user.disconnect()
        await bot.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
