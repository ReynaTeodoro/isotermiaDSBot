# Daily Bot — Discord → Google Sheets

Escucha un canal de Discord, detecta los mensajes que tienen las tres preguntas
de la daily y agrega una fila a la planilla con este formato:

| Fecha | Integrante | ¿Qué hice ayer? | ¿Qué haré hoy? | ¿Tengo algún impedimento? | Estado del Bloqueo | (msg_id) |
|---|---|---|---|---|---|---|

Los mensajes que no son dailies se ignoran, así el chat normal del canal no
ensucia la planilla. Cuando carga una fila, el bot reacciona con ✅ al mensaje
(⚠️ si falló la escritura).

## 1. Crear el bot en Discord

1. https://discord.com/developers/applications → **New Application**.
2. Pestaña **Bot** → **Reset Token** y copiá el token (va en `DISCORD_TOKEN`).
3. En la misma pestaña, activá **MESSAGE CONTENT INTENT**. Sin esto el bot recibe
   los mensajes vacíos y no puede leer nada.
4. Pestaña **OAuth2 → URL Generator**: scope `bot`, permisos `View Channels`,
   `Read Message History`, `Send Messages`, `Add Reactions`. Abrí la URL generada
   e invitalo al server.
5. En Discord: Ajustes → Avanzado → **Modo desarrollador**. Click derecho sobre el
   canal de dailies → **Copiar ID** (va en `CANAL_ID`).

## 2. Dar acceso a la planilla

Si ya tenés la service account que usás en el workflow de GitHub Actions del
registro de artefactos, reusala: alcanza con compartir esta planilla con su email.

Si necesitás una nueva:

1. Google Cloud Console → proyecto nuevo → **APIs & Services** → habilitá
   **Google Sheets API**.
2. **Credentials → Create credentials → Service account** → dentro de la cuenta,
   **Keys → Add key → JSON**. Guardá el archivo como `credentials.json` en esta
   carpeta.
3. Abrí la planilla y compartila **como Editor** con el `client_email` que figura
   dentro del JSON.
4. Asegurate de que la pestaña se llame igual que `HOJA` y que la fila 1 tenga los
   encabezados (el bot solo hace `append`, no toca los headers).

## 3. Configurar

```bash
cp .env.example .env          # completá token, IDs, nombre de hoja
cp miembros.json.example miembros.json
```

`miembros.json` mapea el ID de usuario de Discord al nombre que querés que aparezca
en la columna *Integrante*. Si un usuario no está en el mapa, usa su nombre visible
del server. Podés dejarlo como `{}`.

## 4. Correr

Local:

```bash
pip install -r requirements.txt
python bot.py
```

Docker (recomendado para dejarlo 24/7, se reinicia solo):

```bash
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

Para hosteo sin máquina propia: Railway, Fly.io o Render, siempre como
**worker / background service**, no como web service (el bot no expone puerto).
Subís las variables del `.env` como env vars y el `credentials.json` como secret
file. En cualquier VPS también sirve un `systemd` service con `Restart=always`.

## Comandos

- `!sync 200` en el canal de dailies: recorre los últimos 200 mensajes y carga las
  dailies que falten (útil para levantar el historial la primera vez).
- Si alguien **edita** su mensaje, el bot actualiza la fila en lugar de duplicarla
  (requiere `GUARDAR_MSG_ID=true`).

## Formato que reconoce

Tolera tildes, mayúsculas, signos de pregunta faltantes y respuestas en viñetas o
en la misma línea que la pregunta. Le alcanza con encontrar:

- algo tipo *"…hice ayer…"*
- algo tipo *"…haré / voy a hacer / hago hoy…"*
- opcionalmente *"impedimento" / "bloqueo"* (si no aparece, guarda `No.`)

La columna **Estado del Bloqueo** se completa sola: `Ninguno` si la respuesta al
impedimento es un "no" / "ninguno" / "-", y `Activo` en cualquier otro caso. La
lista de respuestas que cuentan como "sin bloqueo" está en `SIN_BLOQUEO`, arriba
de todo en `bot.py`.
