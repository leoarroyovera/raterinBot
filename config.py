import os

from dotenv import load_dotenv

load_dotenv()

API_ID = os.environ.get("TG_API_ID")
API_HASH = os.environ.get("TG_API_HASH")

if not API_ID or not API_HASH:
    raise RuntimeError("TG_API_ID y TG_API_HASH deben estar definidos en .env")

API_ID = int(API_ID)

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")

_owner_id = os.environ.get("TG_OWNER_ID")
OWNER_ID = int(_owner_id) if _owner_id else None

# Si es 1 (default), solo se ofrecen como destino los grupos/canales donde
# sos creador o admin. Con 0 se listan todos los grupos donde podes escribir.
ONLY_OWNED_DESTS = os.environ.get("SOLO_GRUPOS_PROPIOS", "1") != "0"

USER_SESSION_NAME = "ratero_user"
BOT_SESSION_NAME = "ratero_bot"
TMP_DIR = "tmp"
LOG_DIR = "logs"

# Segundos sin recibir nada nuevo antes de preguntar a que grupo enviar.
BATCH_DEBOUNCE_S = 2.5
DESTS_PER_PAGE = 8
