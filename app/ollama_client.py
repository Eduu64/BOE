"""
Cliente para la API REST de Ollama (sustituye a llama-cpp-python embebido).

Ventajas frente a compilar llama.cpp dentro del contenedor de la app:
- Sin compilación C/C++ en la imagen de la app (evita los problemas de
  libc/libgomp que dieron guerra al compilar llama-cpp-python a mano).
- Ollama descarga y cachea el GGUF él solo (`asegurar_modelo`), no hace
  falta bajarlo a mano de Hugging Face.
- Ollama detecta y usa GPU (NVIDIA/ROCm) automáticamente si está disponible
  en el host, sin tocar el Dockerfile de la app.
"""
import json
import logging
import time

import httpx

from . import config

log = logging.getLogger(__name__)


def esperar_ollama(timeout_s: int = 120):
    """Espera a que el servicio Ollama responda (por si tarda en arrancar)."""
    limite = time.time() + timeout_s
    ultimo_error = None
    while time.time() < limite:
        try:
            r = httpx.get(f"{config.OLLAMA_HOST}/api/tags", timeout=5)
            if r.status_code == 200:
                return
        except httpx.HTTPError as e:
            ultimo_error = e
        time.sleep(2)
    raise RuntimeError(f"Ollama no responde en {config.OLLAMA_HOST} tras {timeout_s}s: {ultimo_error}")


def _modelo_descargado(nombre: str) -> bool:
    r = httpx.get(f"{config.OLLAMA_HOST}/api/tags", timeout=10)
    r.raise_for_status()
    nombres = {m.get("name") for m in r.json().get("models", [])}
    return nombre in nombres


def asegurar_modelo(nombre: str | None = None):
    """Descarga el modelo en Ollama si no lo tiene ya cacheado (solo la primera vez)."""
    nombre = nombre or config.OLLAMA_MODEL
    if _modelo_descargado(nombre):
        log.info("Modelo %s ya está descargado en Ollama.", nombre)
        return

    log.info("Descargando modelo %s en Ollama (puede tardar varios minutos)...", nombre)
    with httpx.stream(
        "POST", f"{config.OLLAMA_HOST}/api/pull",
        json={"name": nombre, "stream": True},
        timeout=config.OLLAMA_PULL_TIMEOUT,
    ) as r:
        r.raise_for_status()
        for linea in r.iter_lines():
            if not linea:
                continue
            estado = json.loads(linea)
            if "error" in estado:
                raise RuntimeError(f"Error descargando modelo {nombre}: {estado['error']}")
            if estado.get("status"):
                log.info("[ollama pull %s] %s", nombre, estado["status"])
    log.info("Modelo %s descargado.", nombre)


def calentar_modelo(nombre: str | None = None):
    """Carga el modelo en memoria YA, antes de la primera petición real.

    Sin esto, la primera petición de /api/chat paga la carga del modelo
    (~45 s en CPU) dentro de su timeout. Se usa el mismo num_ctx que en
    chat_json: si difiriese, Ollama recargaría el modelo al llegar la
    primera petición real.
    """
    nombre = nombre or config.OLLAMA_MODEL
    log.info("Calentando modelo %s (carga en memoria)...", nombre)
    t0 = time.time()
    try:
        r = httpx.post(
            f"{config.OLLAMA_HOST}/api/generate",
            json={
                "model": nombre,
                "prompt": "",
                "stream": False,
                "keep_alive": config.OLLAMA_KEEP_ALIVE,
                "options": {"num_ctx": config.LLM_CONTEXT_TOKENS},
            },
            timeout=httpx.Timeout(600, connect=10),
        )
        r.raise_for_status()
        log.info("Modelo %s cargado en %.1f s.", nombre, time.time() - t0)
    except httpx.HTTPError as e:
        # No es fatal: la primera petición real lo cargará igualmente.
        log.warning("No se pudo calentar el modelo: %r", e)


def chat_json(mensajes: list[dict], json_schema: dict, max_tokens: int, reintentos: int = 1) -> dict:
    """Llama a /api/chat pidiendo salida forzada al JSON schema dado
    (equivalente funcional al grammar-constrained decoding de llama.cpp).

    Reintenta solo ante errores de red/HTTP/timeout. Un JSON inválido NO se
    reintenta: con temperatura ~0 daría lo mismo (suele ser una salida
    cortada por num_predict) y se propaga al llamador.
    """
    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": mensajes,
        "format": json_schema,
        "options": {
            "temperature": config.LLM_TEMPERATURE,
            "num_ctx": config.LLM_CONTEXT_TOKENS,
            "num_predict": max_tokens,
        },
        "stream": False,
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
    }
    timeout = httpx.Timeout(config.OLLAMA_REQUEST_TIMEOUT, connect=10)
    ultimo_error = None
    for intento in range(1, reintentos + 2):
        try:
            r = httpx.post(f"{config.OLLAMA_HOST}/api/chat", json=payload, timeout=timeout)
            r.raise_for_status()
            break
        except httpx.HTTPError as e:
            ultimo_error = e
            log.warning("/api/chat intento %d/%d falló: %r", intento, reintentos + 1, e)
            if intento <= reintentos:
                time.sleep(5 * intento)
    else:
        raise RuntimeError(f"Ollama no respondió tras {reintentos + 1} intentos: {ultimo_error!r}")

    contenido = r.json().get("message", {}).get("content", "")
    return json.loads(contenido)


def descargar_modelo_de_memoria(nombre: str | None = None):
    """Fuerza a Ollama a liberar el modelo de RAM/VRAM ya (spec: el modelo no
    debe seguir cargado el resto del día tras el chequeo)."""
    nombre = nombre or config.OLLAMA_MODEL
    try:
        httpx.post(f"{config.OLLAMA_HOST}/api/generate", json={"model": nombre, "keep_alive": 0}, timeout=30)
    except httpx.HTTPError as e:
        log.warning("No se pudo forzar la descarga del modelo de memoria: %s", e)