"""Parseo de enlaces t.me a mensajes y resolucion con la cuenta de usuario."""
import re
from dataclasses import dataclass

from telethon.tl.types import PeerChannel

# t.me/usuario/123, t.me/c/1234567/123, con topic opcional en el medio
# (t.me/c/1234567/<topic>/123) y query opcional (?single, ?thread=...).
LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?"
    r"(?:c/(?P<cid>\d+)|(?P<user>[A-Za-z][A-Za-z0-9_]{3,}))"
    r"/(?P<a>\d+)(?:/(?P<b>\d+))?(?P<query>\?[^\s]*)?"
)

ALBUM_WINDOW = 10


@dataclass
class ParsedLink:
    url: str
    chat: str | int  # username o id interno de canal
    msg_id: int
    single: bool


def parse_links(text: str) -> list[ParsedLink]:
    out = []
    for m in LINK_RE.finditer(text or ""):
        chat = int(m["cid"]) if m["cid"] else m["user"]
        msg_id = int(m["b"] or m["a"])
        single = "single" in (m["query"] or "")
        out.append(ParsedLink(m.group(0), chat, msg_id, single))
    return out


async def _resolve_chat(client, chat):
    if isinstance(chat, str):
        return await client.get_entity(chat)
    peer = PeerChannel(chat)
    try:
        return await client.get_entity(peer)
    except ValueError:
        # El access_hash no esta en cache: recorrer dialogos lo carga.
        await client.get_dialogs()
        return await client.get_entity(peer)


async def fetch_link_messages(client, link: ParsedLink) -> list:
    """Devuelve los mensajes apuntados por el enlace. Si es parte de un
    album (y el enlace no tiene ?single) devuelve el album completo."""
    entity = await _resolve_chat(client, link.chat)
    msg = await client.get_messages(entity, ids=link.msg_id)
    if msg is None:
        return []
    if not msg.grouped_id or link.single:
        return [msg]

    ids = list(range(link.msg_id - ALBUM_WINDOW, link.msg_id + ALBUM_WINDOW + 1))
    around = await client.get_messages(entity, ids=ids)
    album = [m for m in around if m is not None and m.grouped_id == msg.grouped_id]
    return sorted(album, key=lambda m: m.id)
