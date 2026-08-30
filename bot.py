"""
Bot de Discord que escucha un canal de dailies y carga cada mensaje en Google Sheets.

Formato esperado del mensaje (tolera variantes de tildes, mayúsculas y signos):

    ¿Qué hice ayer para avanzar hacia el objetivo del sprint?
    El backend de cambiar y recuperación de contraseña, ...
    ¿Qué haré hoy para avanzar hacia el objetivo del sprint?
    Deploy completo y hosteo.
    ¿Tengo algún impedimento que me bloquee a mí o al equipo?
    No.

Los mensajes que no contengan al menos las dos primeras preguntas se ignoran,
así el chat normal del canal no ensucia la planilla.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
CANAL_ID = int(os.environ["CANAL_ID"])
SHEET_ID = os.environ["SHEET_ID"]
HOJA = os.getenv("HOJA", "Dailies")
CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TZ = ZoneInfo(os.getenv("TZ", "America/Argentina/Cordoba"))

# Guarda el ID del mensaje en una 7ma columna (podés ocultarla en Sheets).
# Sirve para deduplicar y para actualizar la fila si alguien edita el mensaje.
GUARDAR_MSG_ID = os.getenv("GUARDAR_MSG_ID", "true").lower() == "true"

BASE_DIR = Path(__file__).parent
ESTADO_FILE = Path(os.getenv("ESTADO_FILE", BASE_DIR / "procesados.json"))
MIEMBROS_FILE = BASE_DIR / "miembros.json"

# Respuestas que se consideran "sin impedimento"
SIN_BLOQUEO = {
    "no", "no.", "ninguno", "ninguno.", "ninguna", "ninguna.", "-", "n/a", "na",
    "sin impedimentos", "sin impedimento", "no tengo", "nada", "nope", "no, ninguno",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
)
log = logging.getLogger("daily-bot")

# --------------------------------------------------------------------------- #
# Parseo del mensaje
# --------------------------------------------------------------------------- #

# Traducción 1:1 (no cambia la longitud del string, así los índices siguen
# sirviendo para cortar el texto ORIGINAL).
_TILDES = str.maketrans("áéíóúÁÉÍÓÚüÜñÑàèìòù", "aeiouAEIOUuUnNaeiou")


def normalizar(texto: str) -> str:
    return texto.translate(_TILDES).lower()


P_AYER = re.compile(r"qu[e]?\s*hice\s+ayer|hice\s+ayer")
P_HOY = re.compile(r"(har[e]|voy\s+a\s+hacer|hago|hare)\s+hoy")
P_IMPEDIMENTO = re.compile(r"impedimento|bloqueo|blocker")


def _localizar(texto_norm: str, patron: re.Pattern):
    """Devuelve (inicio_de_la_linea_de_la_pregunta, fin_de_la_pregunta)."""
    m = patron.search(texto_norm)
    if not m:
        return None
    inicio_linea = texto_norm.rfind("\n", 0, m.start()) + 1
    fin_linea = texto_norm.find("\n", m.end())
    if fin_linea == -1:
        fin_linea = len(texto_norm)
    signo = texto_norm.find("?", m.end(), fin_linea)
    fin_pregunta = signo + 1 if signo != -1 else fin_linea
    return inicio_linea, fin_pregunta


def _limpiar(fragmento: str) -> str:
    lineas = []
    for linea in fragmento.strip().splitlines():
        linea = linea.strip().lstrip("-*•>").strip()
        if linea:
            lineas.append(linea)
    return " ".join(lineas)


def parsear_daily(texto: str):
    """Devuelve dict con ayer/hoy/impedimento, o None si no es una daily."""
    norm = normalizar(texto)

    pos_ayer = _localizar(norm, P_AYER)
    pos_hoy = _localizar(norm, P_HOY)
    pos_imp = _localizar(norm, P_IMPEDIMENTO)

    if not pos_ayer or not pos_hoy:
        return None
    if pos_hoy[0] < pos_ayer[0]:
        return None

    fin_ayer = pos_ayer[1]
    corte_ayer = pos_hoy[0]

    fin_hoy = pos_hoy[1]
    corte_hoy = pos_imp[0] if pos_imp and pos_imp[0] > fin_hoy else len(texto)

    ayer = _limpiar(texto[fin_ayer:corte_ayer])
    hoy = _limpiar(texto[fin_hoy:corte_hoy])
    impedimento = _limpiar(texto[pos_imp[1]:]) if pos_imp else ""

    if not ayer and not hoy:
        return None

    return {"ayer": ayer, "hoy": hoy, "impedimento": impedimento or "No."}


def estado_bloqueo(impedimento: str) -> str:
    limpio = normalizar(impedimento).strip().rstrip(".").strip()
    return "Ninguno" if limpio in {s.rstrip(".") for s in SIN_BLOQUEO} else "Activo"


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def abrir_hoja():
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    cliente = gspread.authorize(creds)
    return cliente.open_by_key(SHEET_ID).worksheet(HOJA)


_hoja = None
sheets_lock = asyncio.Lock()


def hoja():
    """Conexión perezosa: se abre en el primer uso y se reutiliza."""
    global _hoja
    if _hoja is None:
        _hoja = abrir_hoja()
    return _hoja


def _fila_de_mensaje(msg_id: str):
    """Busca la fila que ya tiene ese message id (columna 7). None si no está."""
    if not GUARDAR_MSG_ID:
        return None
    celda = hoja().find(str(msg_id), in_column=7)
    return celda.row if celda else None


def _escribir(fila: list, fila_existente: int | None):
    if fila_existente:
        hoja().update([fila], f"A{fila_existente}", value_input_option="USER_ENTERED")
    else:
        hoja().append_row(fila, value_input_option="USER_ENTERED")


# --------------------------------------------------------------------------- #
# Estado local (dedup entre reinicios)
# --------------------------------------------------------------------------- #

def cargar_procesados() -> set:
    if ESTADO_FILE.exists():
        return set(json.loads(ESTADO_FILE.read_text()))
    return set()


def guardar_procesados(ids: set):
    ESTADO_FILE.write_text(json.dumps(sorted(ids)))


def cargar_miembros() -> dict:
    """Mapea discord_user_id -> nombre para la planilla."""
    if MIEMBROS_FILE.exists():
        return json.loads(MIEMBROS_FILE.read_text())
    return {}


procesados = cargar_procesados()
miembros = cargar_miembros()

# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #

intents = discord.Intents.default()
intents.message_content = True  # privileged intent: activalo en el portal
client = discord.Client(intents=intents)


def nombre_de(autor) -> str:
    return miembros.get(str(autor.id)) or getattr(autor, "display_name", autor.name)


async def registrar(mensaje: discord.Message, forzar: bool = False) -> bool:
    if mensaje.author.bot:
        return False
    if mensaje.channel.id != CANAL_ID:
        return False
    if str(mensaje.id) in procesados and not forzar:
        return False

    daily = parsear_daily(mensaje.content)
    if not daily:
        return False

    fecha = mensaje.created_at.astimezone(TZ).strftime("%d/%m/%Y")
    fila = [
        fecha,
        nombre_de(mensaje.author),
        daily["ayer"],
        daily["hoy"],
        daily["impedimento"],
        estado_bloqueo(daily["impedimento"]),
    ]
    if GUARDAR_MSG_ID:
        fila.append(str(mensaje.id))

    async with sheets_lock:
        existente = await asyncio.to_thread(_fila_de_mensaje, mensaje.id)
        await asyncio.to_thread(_escribir, fila, existente)

    procesados.add(str(mensaje.id))
    guardar_procesados(procesados)
    log.info("Cargada daily de %s (%s)", fila[1], fecha)
    return True


@client.event
async def on_ready():
    log.info("Conectado como %s — escuchando el canal %s", client.user, CANAL_ID)


@client.event
async def on_message(mensaje: discord.Message):
    # Comando manual para levantar mensajes viejos: !sync 200
    if (
        mensaje.channel.id == CANAL_ID
        and not mensaje.author.bot
        and mensaje.content.strip().lower().startswith("!sync")
    ):
        partes = mensaje.content.split()
        limite = int(partes[1]) if len(partes) > 1 and partes[1].isdigit() else 100
        cargadas = 0
        async for viejo in mensaje.channel.history(limit=limite, oldest_first=True):
            try:
                if await registrar(viejo):
                    cargadas += 1
            except Exception:
                log.exception("Error cargando el mensaje %s", viejo.id)
        await mensaje.reply(f"Listo: {cargadas} dailies nuevas cargadas.")
        return

    try:
        if await registrar(mensaje):
            await mensaje.add_reaction("✅")
    except Exception:
        log.exception("Error cargando el mensaje %s", mensaje.id)
        try:
            await mensaje.add_reaction("⚠️")
        except discord.HTTPException:
            pass


@client.event
async def on_message_edit(_antes: discord.Message, despues: discord.Message):
    """Si alguien corrige su daily, actualiza la fila en lugar de duplicarla."""
    try:
        await registrar(despues, forzar=True)
    except Exception:
        log.exception("Error actualizando el mensaje %s", despues.id)


if __name__ == "__main__":
    client.run(TOKEN, reconnect=True)
