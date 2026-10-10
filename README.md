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
3. Tocás el grupo (y el tema, si es un supergrupo con Temas) y el bot sube todo ahí, en orden, mostrando el progreso. Al final te dice cuántos subió y cuáles fallaron.

Comandos: `/cola` (volver a mostrar el menú), `/cancelar` (vaciar la cola), `/modo` (ver abajo), `/start`.

### Varios archivos: separados o en ZIP

Cuando la cola tiene más de un archivo hay dos modos de envío:

- **Separados** (default): un mensaje por archivo, como siempre.
- **ZIP**: se descargan todos, se sube **un ZIP sin compresión** con todos los archivos y, si en la cola había imágenes, **además** se publican como álbum (hasta 10 por post, cada una con su caption). Si el ZIP superaría `ZIP_MAX_MB` (default 2000 MiB; poné 4000 con Premium) se parte en `..._parte1.zip`, `..._parte2.zip`, etc.

Cómo elegir:

- **Global**: `/modo zip`, `/modo separado`, o `/modo` solo para alternar. Se guarda en `settings.json` y sobrevive reinicios (el valor inicial sale de `MODO_GRUPO` en `.env`).
- **Solo para este lote**: el menú de grupos muestra el modo actual y un botón *Cambiar a: …* para cambiarlo antes de elegir el destino.

En modo ZIP el espacio temporal en `tmp/` es aproximadamente el doble del tamaño del lote (originales + ZIP).

## Detalles

- **Reenvíos**: los descarga el propio bot. **Enlaces**: los resuelve tu cuenta de usuario, así que funcionan con grupos/canales privados de los que seas miembro, incluso con "contenido protegido" (que no permite reenviar).
- Un enlace a un mensaje que forma parte de un álbum trae el álbum completo; agregale `?single` para traer solo ese mensaje.
- La subida la hace tu cuenta con varias conexiones en paralelo (`fast_upload.py`, el mismo de cloneToTelegram). Se conservan caption, formato, nombre de archivo, miniatura y metadata de video/audio, y si en origen era foto/video se sube como foto/video (no como documento).
- Grupos ofrecidos: por defecto solo aquellos donde sos creador o admin. Con `SOLO_GRUPOS_PROPIOS=0` aparecen todos los grupos en los que podés escribir. Si el grupo tiene Temas, después de elegirlo aparece un segundo menú para elegir el tema (con "↩️ Otro grupo" para volver).
- Mientras se sube un lote, podés seguir armando el siguiente: queda en cola y arranca al terminar el actual.
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
