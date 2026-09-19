"""Validación post-LLM en dos capas (spec sección 3, paso 6). No sustituye al LLM,
solo detecta cuando algo salió muy mal."""
from .categorias import PALABRAS_CLAVE_OPOSICION
from .pdf_extractor import contar_filas_tabla_heuristico


def contiene_palabra_esperada(texto_limpio: str) -> bool:
    texto_low = texto_limpio.lower()
    return any(p in texto_low for p in PALABRAS_CLAVE_OPOSICION)


def necesita_revision(texto_limpio: str, convocatorias: list[dict], formato_fuente: str) -> bool:
    # Capa 1: semántica
    if convocatorias and not contiene_palabra_esperada(texto_limpio):
        return True

    # Capa 2: recuento (solo aplica si el formato detectado es tabla)
    if formato_fuente == "tabla":
        filas_heuristicas = contar_filas_tabla_heuristico(texto_limpio)
        if filas_heuristicas > 0 and abs(filas_heuristicas - len(convocatorias)) > max(1, filas_heuristicas // 3):
            return True

    return False
