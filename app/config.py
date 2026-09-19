"""Configuración central. Todo se lee de variables de entorno (.env)."""
import os
from pathlib import Path

# --- Discord ---
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "")  # a quién avisar (DM). Opcional si se usa canal.
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")  # alternativa: canal fijo en vez de DM

# --- Horario ---
RUN_TIME = os.environ.get("RUN_TIME", "08:00")  # formato "HH:MM"
RUN_HOUR, RUN_MINUTE = (int(x) for x in RUN_TIME.split(":"))
TIMEZONE = os.environ.get("TZ", "Europe/Madrid")

# --- Rutas ---
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "boe_watcher.db"

# --- BOE API ---
BOE_SUMARIO_URL = "https://boe.es/datosabiertos/api/boe/sumario/{fecha}"
BOE_SECCIONES_OPOSICIONES = {"2B", "2A"}  # II.B principal, II.A también relevante según spec
REQUEST_TIMEOUT = 30

# --- Bandeja diaria ---
BANDEJA_EXPIRA_DIAS = int(os.environ.get("BANDEJA_EXPIRA_DIAS", "7"))

# --- Depuración del parser del BOE (activa logs detallados de la estructura
# XML recibida; útil mientras se verifica el parser contra datos reales) ---
DEBUG_BOE_PARSER = os.environ.get("DEBUG_BOE_PARSER", "true").lower() in ("1", "true", "yes")

# --- LLM (servido por Ollama, contenedor aparte) ---
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b-instruct-q4_K_M")
# Cuánto mantiene Ollama el modelo en RAM/VRAM tras la última petición dentro
# de un mismo chequeo diario (para no recargarlo entre cada PDF). Al acabar
# el chequeo, Clasificador.cerrar() fuerza la descarga inmediata (spec:
# el modelo no debe seguir cargado el resto del día).
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "5m")
OLLAMA_REQUEST_TIMEOUT = int(os.environ.get("OLLAMA_REQUEST_TIMEOUT", "180"))
OLLAMA_PULL_TIMEOUT = int(os.environ.get("OLLAMA_PULL_TIMEOUT", "1800"))  # primera descarga: puede ser ~2GB

LLM_CONTEXT_TOKENS = int(os.environ.get("LLM_CONTEXT_TOKENS", "4096"))
LLM_PROMPT_RESERVE_TOKENS = int(os.environ.get("LLM_PROMPT_RESERVE_TOKENS", "900"))  # system+ejemplos+margen
LLM_TOKENS_PER_ROW_ESTIMATE = int(os.environ.get("LLM_TOKENS_PER_ROW_ESTIMATE", "90"))
LLM_OUTPUT_TOKENS_MIN = int(os.environ.get("LLM_OUTPUT_TOKENS_MIN", "256"))
LLM_OUTPUT_TOKENS_MAX = int(os.environ.get("LLM_OUTPUT_TOKENS_MAX", "3000"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.0"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
