"""Orquestación del pipeline diario (spec sección 1 y 3)."""
import logging
import re
from datetime import date

from . import boe_client, config, db
from .chunker import DocumentChunker
from .classifier import Clasificador
from .pdf_extractor import detectar_formato, extraer_texto, limpiar_ruido
from .validation import necesita_revision

log = logging.getLogger(__name__)


def ejecutar_chequeo_diario(hoy: date | None = None) -> str | None:
    """Uso del scheduler automático. Devuelve la fecha ISO procesada, o None si
    el sumario aún no está publicado o ya se había ejecutado hoy."""
    hoy = hoy or date.today()
    fecha_iso = hoy.isoformat()

    if db.ya_ejecutado_hoy(fecha_iso):
        log.info("Ya se ejecutó el chequeo de %s, no se duplica.", fecha_iso)
        return None

    if not boe_client.sumario_publicado(hoy):
        log.info("Sumario del BOE de %s aún no publicado.", fecha_iso)
        return None

    _procesar_dia(hoy)
    db.marcar_ejecutado(fecha_iso)
    db.purgar_bandeja_expirada()
    return fecha_iso


def forzar_chequeo(hoy: date | None = None, forzar: bool = False) -> tuple[str, str | None]:
    """Uso del comando /boe (bajo demanda).
    Devuelve (estado, fecha_iso) con estado en:
      - "ya_hecho"     -> hoy ya se procesó, se reutiliza lo que ya hay en bandeja_diaria
      - "no_publicado" -> el sumario de hoy no está disponible todavía
      - "error"        -> fallo de red/parseo al consultar el BOE
      - "ok"           -> procesado ahora mismo con éxito

    `forzar=True` ignora la marca de "ya procesado" y vuelve a consultar el
    BOE de cero para esa fecha (borrando antes lo que hubiera en la bandeja
    diaria de ese día, para no duplicar filas). Útil para volver a intentar
    una fecha que se marcó como procesada por error (p. ej. durante pruebas
    con un parser que aún no funcionaba bien).
    """
    hoy = hoy or date.today()
    fecha_iso = hoy.isoformat()

    if forzar:
        db.borrar_marca_ejecutado(fecha_iso)
        db.borrar_bandeja_fecha(fecha_iso)
    elif db.ya_ejecutado_hoy(fecha_iso):
        return "ya_hecho", fecha_iso

    try:
        if not boe_client.sumario_publicado(hoy):
            return "no_publicado", None
        _procesar_dia(hoy)
    except Exception:
        log.exception("Error en chequeo bajo demanda para %s", fecha_iso)
        return "error", None

    db.marcar_ejecutado(fecha_iso)
    db.purgar_bandeja_expirada()
    return "ok", fecha_iso


def _titulo_ignorable(titulo: str) -> bool:
    """Prefiltro por título del sumario, ANTES de descargar el PDF y llamar al
    modelo (cada item cuesta minutos en CPU). Opt-in: solo actúa si defines en
    config.py una lista TITULOS_IGNORAR de expresiones regulares, p. ej.
        TITULOS_IGNORAR = ["por la que se nombra", "por la que se dispone el cese"]
    Sin la lista no se descarta nada."""
    patrones = getattr(config, "TITULOS_IGNORAR", [])
    return any(re.search(p, titulo or "", re.IGNORECASE) for p in patrones)


def _procesar_dia(hoy: date):
    items = boe_client.obtener_items_oposiciones(hoy)
    log.info("Encontrados %d items de oposiciones en el sumario de %s", len(items), hoy.isoformat())
    if not items:
        return

    clasificador = Clasificador()
    chunker = DocumentChunker()
    ignorados = 0
    con_error = []
    try:
        for item in items:
            if _titulo_ignorable(item.titulo):
                ignorados += 1
                log.info("Ignorado por título: %s (%s)", item.cve_boe, item.titulo[:120])
                continue
            try:
                if _procesar_item(item, chunker, clasificador):
                    con_error.append(item.cve_boe)
            except Exception:
                con_error.append(item.cve_boe)
                log.exception("Error procesando item %s (%s)", item.cve_boe, item.titulo)
    finally:
        clasificador.cerrar()  # el modelo NO se mantiene cargado el resto del día

    log.info("Chequeo %s: %d items, %d ignorados por título, %d con errores%s.",
             hoy.isoformat(), len(items), ignorados, len(con_error),
             f" ({', '.join(con_error)})" if con_error else "")


def _procesar_item(item, chunker: DocumentChunker, clasificador: Clasificador) -> bool:
    """Devuelve True si el item se procesó con errores (total o parcialmente)."""
    pdf_bytes = boe_client.descargar_pdf(item)
    texto_crudo = extraer_texto(pdf_bytes)
    texto_limpio = limpiar_ruido(texto_crudo)
    formato_heuristico = detectar_formato(texto_limpio)

    bloques = chunker.trocear(texto_limpio)
    resultado = clasificador.clasificar_documento(bloques)
    convocatorias = resultado.get("convocatorias", [])
    formato_fuente = resultado.get("formato_fuente", formato_heuristico)
    hubo_error = bool(resultado.get("error"))

    revisar = necesita_revision(texto_limpio, convocatorias, formato_fuente)
    if hubo_error:
        # Resultado incompleto (algún trozo falló): lo que haya se guarda pero
        # marcado para revisión, en vez de parecer un resultado fiable.
        revisar = True
        log.error("Item %s clasificado con errores: %d convocatorias recuperadas (parcial).",
                  item.cve_boe, len(convocatorias))

    if not convocatorias:
        return hubo_error

    resumen_texto = texto_limpio[:4000]  # texto_extraido_resumen (spec sección 5)
    for conv in convocatorias:
        conv["formato_fuente"] = formato_fuente
        db.insertar_convocatoria(item, conv, resumen_texto, revisar)
    return hubo_error