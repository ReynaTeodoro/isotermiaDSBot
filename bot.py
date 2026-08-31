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

# Regla: un registro por integrante por día. Si ya existe una fila con la misma
# fecha y el mismo integrante, se actualiza en lugar de agregar otra.
FILA_ENCABEZADOS = int(os.getenv("FILA_ENCABEZADOS", "1"))
# Columna donde arranca la tabla (la de "Fecha"). Las 6 columnas se escriben a
# partir de ahí: Fecha, Integrante, Ayer, Hoy, Impedimento, Estado.
COLUMNA_INICIAL = os.getenv("COLUMNA_INICIAL", "A").strip().upper()
# Formato con el que se escribe la fecha. ISO (%Y-%m-%d) es el más seguro: Sheets
# lo interpreta como fecha real sin importar la configuración regional, y la
# columna la muestra igual con su propio formato.
FORMATO_FECHA = os.getenv("FORMATO_FECHA", "%Y-%m-%d")

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


def _letra_a_num(letra: str) -> int:
    n = 0
    for c in letra:
        n = n * 26 + (ord(c) - 64)
    return n


def _num_a_letra(n: int) -> str:
    letra = ""
    while n > 0:
        n, resto = divmod(n - 1, 26)
        letra = chr(65 + resto) + letra
    return letra


COL_INI = _letra_a_num(COLUMNA_INICIAL)
COL_FECHA = COLUMNA_INICIAL
COL_INTEGRANTE = _num_a_letra(COL_INI + 1)
COL_FIN = _num_a_letra(COL_INI + 5)


def _misma_fecha(a: str, b: str) -> bool:
    """Compara fechas tolerando distintos formatos de la planilla."""
    a, b = a.strip(), b.strip()
    if a and a == b:
        return True

    def parsear(texto):
        for formato in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(texto, formato).date()
            except ValueError:
                continue
        return None

    pa, pb = parsear(a), parsear(b)
    return pa is not None and pa == pb


def _upsert(fila: list) -> str:
    """Actualiza la fila del día si ya existe; si no, agrega al final.

    Devuelve "Actualizada" o "Cargada", solo para el log.
    """
    fecha, integrante = fila[0], fila[1].strip().lower()

    # Una sola lectura de Fecha e Integrante. get_values() no devuelve las filas
    # vacías del final, así que len() es la última fila realmente usada: eso evita
    # que la carga caiga al fondo de la hoja.
    valores = hoja().get_values(f"{COL_FECHA}:{COL_INTEGRANTE}")

    destino = None
    for nro, renglon in enumerate(valores, start=1):
        if nro <= FILA_ENCABEZADOS:
            continue
        f = renglon[0] if renglon else ""
        n = renglon[1] if len(renglon) > 1 else ""
        if _misma_fecha(f, fecha) and n.strip().lower() == integrante:
            destino = nro
            break

    accion = "Actualizada"
    if destino is None:
        destino = max(len(valores), FILA_ENCABEZADOS) + 1
        accion = "Cargada"

    hoja().update(
        [fila],
        f"{COL_FECHA}{destino}:{COL_FIN}{destino}",
        value_input_option="USER_ENTERED",
    )
    return accion


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

    fecha = mensaje.created_at.astimezone(TZ).strftime(FORMATO_FECHA)
    fila = [
        fecha,
        nombre_de(mensaje.author),
        daily["ayer"],
        daily["hoy"],
        daily["impedimento"],
        estado_bloqueo(daily["impedimento"]),
    ]
    async with sheets_lock:
        accion = await asyncio.to_thread(_upsert, fila)

    procesados.add(str(mensaje.id))
    guardar_procesados(procesados)
    log.info("%s daily de %s (%s)", accion, fila[1], fecha)
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
    """Si alguien corrige su daily, se reescribe la fila de ese día."""
    try:
        await registrar(despues, forzar=True)
    except Exception:
        log.exception("Error actualizando el mensaje %s", despues.id)


if __name__ == "__main__":
    client.run(TOKEN, reconnect=True)
