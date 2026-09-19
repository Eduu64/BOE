"""
Extracción de texto de PDFs del BOE + limpieza de RUIDO tipográfico.

Importante (spec sección 3, paso 2): el regex aquí SOLO limpia ruido
(cabeceras repetidas, número de página, línea CVE de verificación).
NUNCA decide turno/grupo/categoría — eso es responsabilidad exclusiva del LLM.
"""
import re

from pypdf import PdfReader

_RUIDO_PATRONES = [
    re.compile(r"^\s*BOLET[IÍ]N OFICIAL DEL ESTADO\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*N[uú]m\.\s*\d+.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*P[aá]gina\s*\d+\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Sec\.\s*[IVX]+[-.\s]*[A-Z]?\.?\s*.*Pág\.\s*\d+\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*cve:\s*BOE-[A-Z0-9-]+.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Verificable en https?://\S+.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*ISSN:\s*[\d-]+\s*$", re.IGNORECASE | re.MULTILINE),
]


def extraer_texto(pdf_bytes: bytes) -> str:
    reader = PdfReader(_bytes_io(pdf_bytes))
    paginas = [p.extract_text() or "" for p in reader.pages]
    return "\n".join(paginas)


def _bytes_io(b: bytes):
    import io
    return io.BytesIO(b)


def limpiar_ruido(texto: str) -> str:
    limpio = texto
    for patron in _RUIDO_PATRONES:
        limpio = patron.sub("", limpio)
    # colapsar líneas en blanco excesivas dejadas por la limpieza
    limpio = re.sub(r"\n{3,}", "\n\n", limpio)
    return limpio.strip()


# --- Heurística texto vs tabla (spec sección 2) ---

_PATRON_FILA_TABLA = re.compile(
    r"^\s*(\d{1,4})\s+[A-ZÁÉÍÓÚÑ].{3,}",  # nº de orden/plaza al inicio de línea + texto
    re.MULTILINE,
)


def detectar_formato(texto_limpio: str) -> str:
    """Heurística simple: si hay muchas líneas con patrón "nº + texto" repetido, es tabla."""
    filas = _PATRON_FILA_TABLA.findall(texto_limpio)
    lineas_totales = max(texto_limpio.count("\n"), 1)
    if len(filas) >= 3 and (len(filas) / lineas_totales) > 0.05:
        return "tabla"
    return "texto"


def contar_filas_tabla_heuristico(texto_limpio: str) -> int:
    """Recuento heurístico de filas de tabla, usado en la validación post-LLM (sección 3.6)."""
    return len(_PATRON_FILA_TABLA.findall(texto_limpio))
