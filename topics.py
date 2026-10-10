"""Listado de temas (topics) de un supergrupo con modo Foro."""
from telethon.tl.functions.messages import GetForumTopicsRequest

from transfer import safe_run

GENERAL_TOPIC_ID = 1


def is_forum(entity) -> bool:
    return bool(getattr(entity, "forum", False))


async def list_topics(client, entity) -> list[tuple[int, str]]:
    """Devuelve [(topic_id, titulo)] en el orden que muestra Telegram
    (fijados primero, luego por actividad reciente)."""
    topics = []
    offset_date, offset_id, offset_topic = None, 0, 0
    while True:
        result = await safe_run(client, GetForumTopicsRequest(
            peer=entity, offset_date=offset_date, offset_id=offset_id,
            offset_topic=offset_topic, limit=100))
        page = [t for t in result.topics if hasattr(t, "title")]  # sin ForumTopicDeleted
        for t in page:
            title = ("🔒 " if t.closed else "") + t.title
            topics.append((t.id, title))
        if len(result.topics) < 100 or not page:
            break
        last = result.topics[-1]
        offset_topic = last.id
        offset_id = last.top_message
        offset_date = next((m.date for m in result.messages if m.id == last.top_message), None)
    return topics
