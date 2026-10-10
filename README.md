# rateroBot

Bot de Telegram al que le reenviás archivos (fotos, videos, documentos, audio…) o le pegás enlaces de mensajes `t.me/...`. Los descarga y los vuelve a subir como archivo nuevo (no como reenvío) al grupo que elijas. Podés mandar uno o varios; todos van al mismo grupo.

## Setup

1. Instalar dependencias:
   ```
   pip install -r requirements.txt
   ```
2. Copiar `.env.example` a `.env` y completar:
   - `TG_API_ID` / `TG_API_HASH`: de https://my.telegram.org (pueden ser los mismos de cloneToTelegram).
   - `TG_BOT_TOKEN`: crear un bot **nuevo** con @BotFather (no reutilizar el token del bot de cloneToTelegram si ese sigue corriendo).
   - `TG_OWNER_ID`: tu user_id numérico (@userinfobot). El bot ignora a cualquier otra persona.
3. Login de tu cuenta, una sola vez y en una terminal interactiva:
   ```
   python login.py
   ```
   Queda guardado en `ratero_user.session`.
4. Levantar el bot:
   ```
   python bot.py
   ```

## Uso

1. Mandale al bot, en privado, una o varias de estas cosas:
   - archivos reenviados desde cualquier chat, o enviados directamente;
   - enlaces de mensajes: `t.me/canal/123`, `t.me/c/1234567/123`, con topic (`t.me/c/1234567/5/123`). Se pueden pegar varios en un mismo mensaje.
2. Cuando pasan ~2,5 s sin que mandes nada nuevo, el bot te muestra la cola con botones para elegir el grupo destino (paginados). Si seguís mandando cosas, el menú se actualiza.
3. Tocás el grupo y el bot sube todo ahí, en orden, mostrando el progreso. Al final te dice cuántos subió y cuáles fallaron.

Comandos: `/cola` (volver a mostrar el menú), `/cancelar` (vaciar la cola), `/start`.

## Detalles

- **Reenvíos**: los descarga el propio bot. **Enlaces**: los resuelve tu cuenta de usuario, así que funcionan con grupos/canales privados de los que seas miembro, incluso con "contenido protegido" (que no permite reenviar).
- Un enlace a un mensaje que forma parte de un álbum trae el álbum completo; agregale `?single` para traer solo ese mensaje.
- La subida la hace tu cuenta con varias conexiones en paralelo (`fast_upload.py`, el mismo de cloneToTelegram). Se conservan caption, formato, nombre de archivo, miniatura y metadata de video/audio, y si en origen era foto/video se sube como foto/video (no como documento).
- Grupos ofrecidos: por defecto solo aquellos donde sos creador o admin. Con `SOLO_GRUPOS_PROPIOS=0` aparecen todos los grupos en los que podés escribir. Si el grupo tiene Temas, se publica en "General".
- Mientras se sube un lote, podés seguir armando el siguiente: queda en cola y arranca al terminar el actual.
- Logs en `logs/bot.log`.
