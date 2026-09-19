"""
DocumentChunker (spec sección 3, paso 4).

Solo trocea si el documento no cabe en el presupuesto de tokens de entrada.
Si hace falta trocear, corta por filas de tabla repitiendo SIEMPRE el
párrafo introductorio compartido en cada bloque, para no perder
turno/organismo al cortar.

Cambios respecto a la versión anterior:
- La estimación de tokens usa 2,2 caracteres/token en vez de 4. Medido en
  logs reales (tokenizer de Qwen2.5): texto legal español con tablas, cifras
  y mayúsculas da 2,3-3,4 car/token. Con 4 el chunker creía que documentos de
  ~3600 tokens reales "cabían" y los mandaba enteros.
- El presupuesto de texto por petición ya no es "contexto menos hueco" (~2600
  tokens, que a ~16 tok/s son minutos por llamada), sino un objetivo pensado
  para la latencia en CPU (LLM_MAX_TEXT_TOKENS, 700 por defecto).
- Un documento SIN filas de tabla (texto corrido) antes se enviaba entero sin
  importar su tamaño, porque no había filas por las que cortar. Ahora se
  acota a su cabecera (LLM_MAX_PROSE_CHARS): en una convocatoria en prosa,
  puesto/plazas/organismo/turno/grupo van al principio; el resto son bases,
  temario y tribunal, que solo cuestan tiempo y favorecen alucinaciones.
"""
import logging
from dataclasses import dataclass

from . import config
from .pdf_extractor import _PATRON_FILA_TABLA

log = logging.getLogger(__name__)

CHARS_POR_TOKEN = 2.2
MAX_TOKENS_TEXTO = getattr(config, "LLM_MAX_TEXT_TOKENS", 700)
MAX_CHARS_PROSA = getattr(config, "LLM_MAX_PROSE_CHARS", 3000)


def estimar_tokens(texto: str) -> int:
    # Estimación sin tokenizer exacto; ver CHARS_POR_TOKEN.
    return max(1, int(len(texto) / CHARS_POR_TOKEN))


def _cabecera(texto: str, max_chars: int) -> str:
    """Primeros max_chars caracteres, cortando en salto de línea si hay uno cercano."""
    if len(texto) <= max_chars:
        return texto
    corte = texto.rfind("\n", 0, max_chars)
    if corte < max_chars * 0.6:
        corte = max_chars
    return texto[:corte]


@dataclass
class Bloque:
    texto: str
    n_filas: int  # nº de filas de tabla estimadas en este bloque (para output dinámico)


class DocumentChunker:
    def __init__(self, contexto_max: int = None, hueco_prompt: int = None):
        self.contexto_max = contexto_max or config.LLM_CONTEXT_TOKENS
        self.hueco_prompt = hueco_prompt or config.LLM_PROMPT_RESERVE_TOKENS
        self.presupuesto_entrada = max(
            300, min(self.contexto_max - self.hueco_prompt, MAX_TOKENS_TEXTO)
        )

    def trocear(self, texto_limpio: str) -> list[Bloque]:
        intro, filas = self._separar_intro_y_filas(texto_limpio)

        if not filas:
            # Texto corrido (sin filas de tabla): un solo bloque con la cabecera.
            if len(texto_limpio) > MAX_CHARS_PROSA:
                log.warning("Documento sin tabla de %d caracteres: se clasifica solo la cabecera (%d).",
                            len(texto_limpio), MAX_CHARS_PROSA)
            return [Bloque(texto=_cabecera(texto_limpio, MAX_CHARS_PROSA), n_filas=0)]

        if estimar_tokens(texto_limpio) <= self.presupuesto_entrada:
            n_filas = len(_PATRON_FILA_TABLA.findall(texto_limpio))
            return [Bloque(texto=texto_limpio, n_filas=n_filas)]

        # La intro se repite en cada bloque: si es enorme, se acota para que
        # siempre quede sitio para filas.
        max_tokens_intro = self.presupuesto_entrada // 2
        if estimar_tokens(intro) > max_tokens_intro:
            intro = _cabecera(intro, int(max_tokens_intro * CHARS_POR_TOKEN))
        presupuesto_filas = max(200, self.presupuesto_entrada - estimar_tokens(intro))

        bloques: list[Bloque] = []
        bloque_actual: list[str] = []
        tokens_actuales = 0
        for fila in filas:
            t_fila = estimar_tokens(fila)
            if tokens_actuales + t_fila > presupuesto_filas and bloque_actual:
                bloques.append(self._construir_bloque(intro, bloque_actual))
                bloque_actual = []
                tokens_actuales = 0
            bloque_actual.append(fila)
            tokens_actuales += t_fila
        if bloque_actual:
            bloques.append(self._construir_bloque(intro, bloque_actual))

        return bloques or [Bloque(texto=_cabecera(texto_limpio, MAX_CHARS_PROSA), n_filas=0)]

    @staticmethod
    def _separar_intro_y_filas(texto: str) -> tuple[str, list[str]]:
        lineas = texto.split("\n")
        primera_fila_idx = None
        for i, linea in enumerate(lineas):
            if _PATRON_FILA_TABLA.match(linea):
                primera_fila_idx = i
                break
        if primera_fila_idx is None:
            return texto, []
        intro = "\n".join(lineas[:primera_fila_idx])
        cuerpo = lineas[primera_fila_idx:]
        # agrupar líneas de cuerpo en "filas" cortando en cada nueva coincidencia del patrón
        filas: list[str] = []
        actual: list[str] = []
        for linea in cuerpo:
            if _PATRON_FILA_TABLA.match(linea) and actual:
                filas.append("\n".join(actual))
                actual = []
            actual.append(linea)
        if actual:
            filas.append("\n".join(actual))
        return intro, filas

    @staticmethod
    def _construir_bloque(intro: str, filas: list[str]) -> Bloque:
        texto = intro + "\n\n" + "\n".join(filas)
        return Bloque(texto=texto, n_filas=len(filas))


def calcular_max_tokens_salida(n_filas_estimadas: int) -> int:
    """Longitud de salida dinámica según nº de filas detectadas (spec sección 3, paso 5)."""
    estimado = config.LLM_OUTPUT_TOKENS_MIN + n_filas_estimadas * config.LLM_TOKENS_PER_ROW_ESTIMATE
    return max(config.LLM_OUTPUT_TOKENS_MIN, min(estimado, config.LLM_OUTPUT_TOKENS_MAX))