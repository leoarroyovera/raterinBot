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

# Modo por defecto para lotes de mas de un archivo: "separado" (un mensaje
# por archivo) o "zip" (un ZIP con todo + album con las imagenes). Se puede
# cambiar desde el bot con /modo; ese cambio se guarda en SETTINGS_PATH y
# tiene prioridad sobre este valor.
DEFAULT_GROUP_MODE = os.environ.get("MODO_GRUPO", "separado").strip().lower()
SETTINGS_PATH = "settings.json"

# Limite de subida en MiB. Vacio = automatico segun la cuenta de usuario
# (4000 si es Premium, 2000 si no). Los archivos que lo superan se rechazan
# al agregarlos a la cola, antes de descargarlos.
_max_upload = os.environ.get("MAX_UPLOAD_MB", "").strip()
MAX_UPLOAD_MB = int(_max_upload) if _max_upload else None

# Tamano maximo de cada ZIP en MiB; si el lote lo supera se parte en varios
# ZIP. Vacio = el limite de subida.
_zip_max = os.environ.get("ZIP_MAX_MB", "").strip()
ZIP_MAX_MB = int(_zip_max) if _zip_max else None

# Espacio libre que se deja siempre en disco al descargar (MiB).
DISK_MARGIN_MB = int(os.environ.get("MARGEN_DISCO_MB", "200"))

# Enlaces con rango (t.me/canal/100-150): cantidad maxima de mensajes.
MAX_LINK_RANGE = 500

# Grupos destino: cuantos recientes se muestran primero y cuanto tiempo se
# reutiliza la lista de grupos antes de volver a recorrer los dialogos.
RECENT_DESTS = 5
DESTS_CACHE_S = 600

# Cola y envios pendientes, para retomarlos si el bot se reinicia.
QUEUE_PATH = "queue.json"
# Cuantos botones de "Reintentar fallidos" se mantienen activos.
MAX_RETRIES_KEPT = 20
