"""
Clasificación semántica vía LLM (spec sección 3, paso 3 y sección 4), servido
por Ollama en vez de llama-cpp-python embebido en la app.

- El "modelo cargado una vez al día": Ollama mantiene el modelo en memoria
  durante todo el chequeo (OLLAMA_KEEP_ALIVE) y Clasificador.cerrar() fuerza
  su descarga justo al terminar, para que no siga cargado el resto del día.
- Salida forzada a JSON válido mediante `format` (JSON Schema) en la API de
  Ollama — equivalente funcional al grammar-constrained decoding (GBNF) de
  llama.cpp. El esquema se construye aquí mismo a partir de categorias.py,
  que es la única fuente de verdad para la taxonomía (sin gramática aparte
  que mantener sincronizada a mano).
- Temperatura ~0 para determinismo.
"""
import json
import logging

from . import config, ollama_client
from .categorias import CATEGORIAS_CANONICAS, GRUPOS_VALIDOS, TURNOS_VALIDOS
from .chunker import CHARS_POR_TOKEN, calcular_max_tokens_salida

log = logging.getLogger(__name__)

# El chunker ya deja los bloques en un tamaño razonable para la latencia en CPU.
# Esto es solo una red de seguridad basada en el CONTEXTO: Ollama reserva
# num_predict tokens del contexto, y si prompt + num_predict > num_ctx recorta
# el prompt por el principio, perdiendo el prompt de sistema. Es lo que pasó
# con el prompt de 4355 tokens (limit=2050): lo más probable es que fuese
# 4096 - num_predict, porque otros prompts de ~2100 tokens pasaron sin recorte.
_TOKENS_SISTEMA = 700        # medido en logs: ~689 tokens
_TOKENS_WRAPPER = 40         # "Texto de la convocatoria:" + "Devuelve el JSON." + plantilla
_MIN_TOKENS_SALIDA = 700     # las convocatorias en prosa llegan con n_filas=0 y un tope minúsculo
_MAX_CHARS_TEXTO = 4000      # ~1800 tokens en el peor caso medido; por encima se trocea aquí


def _trocear(texto: str, max_chars: int) -> list[str]:
    """Parte el texto por líneas en trozos de <= max_chars.
    Una línea individual más larga que max_chars se corta a lo bruto."""
    trozos, actual = [], ""
    for linea in texto.splitlines():
        while len(linea) > max_chars:
            if actual:
                trozos.append(actual)
                actual = ""
            trozos.append(linea[:max_chars])
            linea = linea[max_chars:]
        if actual and len(actual) + len(linea) + 1 > max_chars:
            trozos.append(actual)
            actual = ""
        actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        trozos.append(actual)
    return trozos or [texto]

_SYSTEM_PROMPT = f"""Eres un asistente experto en oposiciones públicas españolas (BOE).
Tu tarea: leer el texto de una convocatoria (o fragmento) y devolver SOLO un JSON
con la estructura pedida, sin explicaciones adicionales.

Contexto legal breve:
- "turno": libre, promocion_interna, discapacidad, mixto, o no_especificado si no se indica.
- "grupo": grupo/subgrupo profesional (A1, A2, B, C1, C2, AP). Puede haber varios.
- "categorias": elige SIEMPRE de esta lista cerrada, mapeando sinónimos del texto
  a la categoría canónica más adecuada (ej. "informática", "sistemas", "TIC" -> informatica_tic):
  {", ".join(CATEGORIAS_CANONICAS)}

Un mismo documento puede tener varias plazas/puestos distintos (formato tabla).
Si el texto introductorio comparte organismo/turno con filas que no lo repiten
explícitamente, propaga ese valor a la fila y marca "hereda_contexto_general": true.
Grupos válidos: {", ".join(GRUPOS_VALIDOS)}.
Turnos válidos: {", ".join(TURNOS_VALIDOS)}.

Ejemplo (formato texto):
Texto: "Resolución del Ayuntamiento de Ejemplo por la que se convoca una plaza de
Técnico Informático, funcionario de carrera, grupo A2, turno libre."
Salida: {{"formato_fuente": "texto", "convocatorias": [{{"puesto": "Técnico Informático",
"organismo": "Ayuntamiento de Ejemplo", "plazas": 1, "turno": "libre", "grupo": ["A2"],
"categorias": ["informatica_tic"], "hereda_contexto_general": false}}]}}

Ejemplo (formato tabla, turno general en la intro no repetido en la fila):
Texto: "Se convocan las siguientes plazas, turno libre:
1 Auxiliar Administrativo C2
2 Ingeniero Técnico A2"
Salida: {{"formato_fuente": "tabla", "convocatorias": [
{{"puesto": "Auxiliar Administrativo", "organismo": "no_especificado", "plazas": 1,
"turno": "libre", "grupo": ["C2"], "categorias": ["administracion_general"],
"hereda_contexto_general": true}},
{{"puesto": "Ingeniero Técnico", "organismo": "no_especificado", "plazas": 1,
"turno": "libre", "grupo": ["A2"], "categorias": ["ingenieria"],
"hereda_contexto_general": true}}]}}
"""

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "formato_fuente": {"type": "string", "enum": ["texto", "tabla"]},
        "convocatorias": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "puesto": {"type": "string"},
                    "organismo": {"type": "string"},
                    "plazas": {"type": "integer"},
                    "turno": {"type": "string", "enum": TURNOS_VALIDOS},
                    "grupo": {"type": "array", "items": {"type": "string", "enum": GRUPOS_VALIDOS}},
                    "categorias": {"type": "array", "items": {"type": "string", "enum": CATEGORIAS_CANONICAS}},
                    "hereda_contexto_general": {"type": "boolean"},
                },
                "required": [
                    "puesto", "organismo", "plazas", "turno",
                    "grupo", "categorias", "hereda_contexto_general",
                ],
            },
        },
    },
    "required": ["formato_fuente", "convocatorias"],
}


class Clasificador:
    """Agrupa las llamadas a Ollama de un chequeo diario completo."""

    def __init__(self):
        ollama_client.esperar_ollama()
        ollama_client.asegurar_modelo()
        ollama_client.calentar_modelo()

    def cerrar(self):
        # El modelo NO debe seguir cargado en Ollama el resto del día.
        ollama_client.descargar_modelo_de_memoria()

    def _clasificar_trozo(self, texto: str, n_filas: int, profundidad: int = 0) -> dict:
        """Una llamada a Ollama. Siempre devuelve un dict; si algo falla lleva
        "error": True (con las convocatorias parciales que se hayan podido sacar).

        Si el JSON llega cortado (salida truncada por num_predict: demasiadas
        filas para el tope de tokens), reintentar igual daría lo mismo con
        temperatura ~0. En su lugar se parte el trozo por la mitad y se
        clasifica cada mitad, con menos filas y por tanto menos salida."""
        max_tokens = max(calcular_max_tokens_salida(n_filas), _MIN_TOKENS_SALIDA)
        tokens_prompt = _TOKENS_SISTEMA + _TOKENS_WRAPPER + int(len(texto) / CHARS_POR_TOKEN)
        max_tokens = min(max_tokens, max(200, config.LLM_CONTEXT_TOKENS - tokens_prompt - 100))
        mensajes = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Texto de la convocatoria:\n\n{texto}\n\nDevuelve el JSON."},
        ]
        try:
            return ollama_client.chat_json(mensajes, _JSON_SCHEMA, max_tokens)
        except json.JSONDecodeError:
            partes = _trocear(texto, len(texto) // 2 + 1) if len(texto) >= 600 else []
            if profundidad >= 2 or len(partes) < 2:
                log.exception("JSON cortado y no se puede partir más (num_predict=%d, %d caracteres)",
                              max_tokens, len(texto))
                return {"formato_fuente": "texto", "convocatorias": [], "error": True}
            log.warning("JSON cortado (num_predict=%d, %d caracteres): se parte en %d y se reintenta.",
                        max_tokens, len(texto), len(partes))
            convocatorias, formato_fuente, hubo_error = [], "texto", False
            for parte in partes:
                r = self._clasificar_trozo(parte, max(1, n_filas // len(partes)), profundidad + 1)
                formato_fuente = r.get("formato_fuente", formato_fuente)
                convocatorias.extend(r.get("convocatorias", []))
                hubo_error = hubo_error or r.get("error", False)
            salida = {"formato_fuente": formato_fuente, "convocatorias": convocatorias}
            if hubo_error:
                salida["error"] = True
            return salida
        except Exception:
            log.exception("Error clasificando trozo con Ollama")
            return {"formato_fuente": "texto", "convocatorias": [], "error": True}

    def clasificar_bloque(self, texto_bloque: str, n_filas_estimadas: int) -> dict:
        """Si el bloque excede el presupuesto de tokens, se parte en trozos
        (en vez de dejar que Ollama lo trunque destruyendo el prompt).

        Limitación: los trozos posteriores al primero no ven la introducción,
        así que pueden perder el contexto heredado (organismo/turno general).
        Lo ideal es que el chunker ya genere bloques dentro del presupuesto.

        Si algún trozo falla, el resultado lleva "error": True para que el
        llamador NO marque el documento como procesado."""
        trozos = _trocear(texto_bloque, _MAX_CHARS_TEXTO)
        if len(trozos) > 1:
            log.warning("Bloque de %d caracteres partido en %d trozos (máx %d).",
                        len(texto_bloque), len(trozos), _MAX_CHARS_TEXTO)

        convocatorias, formato_fuente, hubo_error = [], "texto", False
        for trozo in trozos:
            n_filas = max(1, round(n_filas_estimadas * len(trozo) / max(len(texto_bloque), 1)))
            resultado = self._clasificar_trozo(trozo, n_filas)
            formato_fuente = resultado.get("formato_fuente", formato_fuente)
            convocatorias.extend(resultado.get("convocatorias", []))
            hubo_error = hubo_error or resultado.get("error", False)

        salida = {"formato_fuente": formato_fuente, "convocatorias": convocatorias}
        if hubo_error:
            salida["error"] = True
        return salida

    def clasificar_documento(self, bloques) -> dict:
        """Clasifica todos los bloques de un documento y fusiona resultados (spec 3.4)."""
        convocatorias = []
        formato_fuente = "texto"
        hubo_error = False
        for bloque in bloques:
            resultado = self.clasificar_bloque(bloque.texto, bloque.n_filas)
            formato_fuente = resultado.get("formato_fuente", formato_fuente)
            convocatorias.extend(resultado.get("convocatorias", []))
            hubo_error = hubo_error or resultado.get("error", False)
        salida = {"formato_fuente": formato_fuente, "convocatorias": convocatorias}
        if hubo_error:
            salida["error"] = True
        return salida