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
   - rangos de mensajes: `t.me/canal/100-150` trae todos los mensajes con archivo entre el 100 y el 150 (máximo 500 por rango). En un enlace con topic el rango es por id de mensaje, así que puede incluir mensajes de otros temas.
2. Cuando pasan ~2,5 s sin que mandes nada nuevo, el bot te muestra la cola con botones para elegir el grupo destino (paginados, con los últimos usados primero, marcados con 🕘). Si seguís mandando cosas, el menú se actualiza.
3. Antes de elegir, con más de un archivo podés tocar **✂️ Quitar archivos** para sacar algunos de la cola, y en modo ZIP **✏️ Nombre del ZIP** para ponerle nombre (mandá «-» para volver al automático).
4. Tocás el grupo (y el tema, si es un supergrupo con Temas) y el bot sube todo ahí, en orden, mostrando el progreso. Al final te dice cuántos subió y cuáles fallaron.

Comandos (también disponibles en el botón **Menú** de Telegram y como botones en `/start`):

| Comando | Qué hace |
|---|---|
| `/start` | Menú principal con botones (ver cola, vaciar cola, cambiar modo). |
| `/cola` | Muestra el envío en curso (con botón para detenerlo), los envíos en espera (con botón para cancelar cada uno) y el lote en armado con su menú de destino. |
| `/cancelar` | Vacía el lote en armado (el que todavía no tiene destino). |
| `/modo` | Modo de envío para varios archivos (ver abajo). |

### Varios archivos: separados o en ZIP

Cuando la cola tiene más de un archivo hay dos modos de envío:

- **Separados** (default): un mensaje por archivo, como siempre.
- **ZIP**: se descargan todos, se sube **un ZIP sin compresión** con todos los archivos y, si en la cola había imágenes, **además** se publican como álbum (hasta 10 por post, cada una con su caption). Si el ZIP superaría el límite de subida (o `ZIP_MAX_MB`, si lo definís) se parte en `..._parte1.zip`, `..._parte2.zip`, etc.

Cómo elegir:

- **Global**: `/modo zip`, `/modo separado`, o `/modo` solo para alternar. Se guarda en `settings.json` y sobrevive reinicios (el valor inicial sale de `MODO_GRUPO` en `.env`).
- **Solo para este lote**: el menú de grupos muestra el modo actual y un botón *Cambiar a: …* para cambiarlo antes de elegir el destino.

En modo ZIP el espacio temporal en `tmp/` es aproximadamente el doble del tamaño del lote (originales + ZIP).

## Detalles

- **Reenvíos**: los descarga el propio bot. **Enlaces**: los resuelve tu cuenta de usuario, así que funcionan con grupos/canales privados de los que seas miembro, incluso con "contenido protegido" (que no permite reenviar).
- Un enlace a un mensaje que forma parte de un álbum trae el álbum completo; agregale `?single` para traer solo ese mensaje.
- La subida la hace tu cuenta con varias conexiones en paralelo (`fast_upload.py`, el mismo de cloneToTelegram). La descarga de archivos de 10 MB o más también va en paralelo (`fast_download.py`, 8 conexiones al datacenter donde está el archivo); si falla por cualquier motivo se reintenta con la descarga estándar. Se conservan caption, formato, nombre de archivo, miniatura y metadata de video/audio, y si en origen era foto/video se sube como foto/video (no como documento).
- **Descarga y subida a la vez**: en modo separado, mientras se sube un archivo ya se va descargando el siguiente. El mensaje de estado muestra las dos cosas.
- **Límites**: el límite de subida se detecta solo al arrancar (4000 MiB si tu cuenta es Premium, 2000 si no; se puede forzar con `MAX_UPLOAD_MB`). Los archivos que lo superan se rechazan al mandarlos, sin descargarlos. Antes de cada descarga se revisa que haya espacio libre en disco (dejando `MARGEN_DISCO_MB`, default 200); en modo ZIP se revisa al empezar que entre el doble del lote.
- Grupos ofrecidos: por defecto solo aquellos donde sos creador o admin. La lista se reutiliza por 10 minutos; tocá **🔄 Actualizar grupos** si acabás de crear o unirte a uno. Con `SOLO_GRUPOS_PROPIOS=0` aparecen todos los grupos en los que podés escribir. Si el grupo tiene Temas, después de elegirlo aparece un segundo menú para elegir el tema (con "↩️ Otro grupo" para volver).
- Mientras se sube un lote, podés seguir armando el siguiente: queda en cola y arranca al terminar el actual. Mientras espera, su mensaje "⏳ En espera" tiene un botón para cancelarlo (también desde `/cola`).
- **Detener el envío en curso**: el mensaje de progreso tiene un botón "⛔ Detener este envío" (también en `/cola`). Corta la transferencia en el acto; lo que ya se subió queda, y lo que faltaba (más lo que haya fallado) queda disponible con el botón "🔁 Retomar lo pendiente".
- **Reintentar fallidos**: si algún archivo falla, el resumen final trae un botón "🔁 Reintentar fallidos (N)" que los vuelve a encolar al mismo grupo/tema y con el mismo modo. En modo ZIP se reintentan los archivos que no se pudieron descargar o los de la parte del ZIP que no se pudo subir.
- **Reinicios**: la cola en armado, los envíos pendientes (con su progreso) y los botones de reintento se guardan en `queue.json`. Si el bot se reinicia (systemd, VPS, caída), al arrancar avisa y retoma: un envío a medias sigue desde el archivo siguiente al último terminado (el que estaba en curso se vuelve a subir; un ZIP a medias se rehace completo). Los archivos cuyo mensaje original ya no existe se descartan y se informa cuántos.
- Logs en `logs/bot.log`.

## Despliegue en una VPS (Oracle Cloud free tier)

Pensado para una VM Ubuntu con usuario `ubuntu`. El bot usa su propio venv, separado del de cloneToTelegram.

1. Clonar y crear el venv (requiere Python 3.10+):
   ```bash
   cd ~
   git clone https://github.com/leoarroyovera/raterinBot.git
   cd raterinBot
   sudo apt install -y python3-venv   # si `python3 -m venv` falla
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```
2. Copiar el `.env` desde tu PC (no está en el repo):
   ```powershell
   scp -i <tu_llave.key> .env ubuntu@<IP_VM>:~/raterinBot/
   ```
3. Login de tu cuenta en la VM: `venv/bin/python login.py`. Alternativa: copiar `ratero_user.session` por `scp`, pero en ese caso no correr el bot en la PC y en la VM a la vez con la misma sesión. En cualquier caso, detené el bot en la PC: dos procesos con el mismo token se pisan los mensajes.
4. Instalar el servicio systemd (ajustar `User` y rutas en `raterinbot.service` si tu VM es distinta):
   ```bash
   sudo cp raterinbot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now raterinbot
   sudo systemctl status raterinbot
   ```
   Logs: `journalctl -u raterinbot -f` o `tail -f logs/bot.log`.

Para actualizar:
```bash
cd ~/raterinBot && git pull && venv/bin/pip install -r requirements.txt && sudo systemctl restart raterinbot
```

Los archivos se descargan temporalmente en `tmp/` antes de subirse; para archivos de varios GB revisar el espacio libre con `df -h`.
