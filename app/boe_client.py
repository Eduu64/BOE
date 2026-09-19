"""
Descarga y parseo del sumario diario del BOE (sección II.B / II.A).

La API del BOE devuelve XML (aunque se pida `Accept: application/json`,
en la práctica el JSON no sigue un esquema estable/documentado). Este
módulo parsea directamente el XML real, con esta forma confirmada:

<response>
  <status><code>200</code><text>ok</text></status>
  <data>
    <sumario>
      <diario numero="...">
        <seccion codigo="2B" nombre="...">
          <departamento codigo="..." nombre="...">
            <epigrafe nombre="...">
              <item>
                <identificador>BOE-A-2026-19441</identificador>
                <titulo>...</titulo>
                <url_pdf>https://.../BOE-A-2026-19441.pdf</url_pdf>
                <url_html>...</url_html>
                <url_xml>...</url_xml>
              </item>
            </epigrafe>
          </departamento>
        </seccion>
      </diario>
    </sumario>
  </data>
</response>

DEBUG_BOE_PARSER=true (por defecto, ver config.py) activa logs detallados
de la estructura recibida: útil para detectar si la respuesta real difiere
de lo documentado (namespaces XML, nombres de atributo distintos, etc.)
sin tener que adivinarlo.
"""
import logging
from dataclasses import dataclass
from datetime import date
from xml.etree import ElementTree as ET

import httpx

from . import config

log = logging.getLogger(__name__)


@dataclass
class ItemBOE:
    cve_boe: str
    titulo: str
    url_pdf: str
    url_html: str
    departamento: str
    fecha_publicacion: str


def _fecha_str(d: date) -> str:
    return d.strftime("%Y%m%d")


def _strip_namespaces(elem: ET.Element) -> ET.Element:
    """Quita el namespace XML ('{http://...}tag' -> 'tag') de todos los
    elementos del árbol. Si el XML real del BOE declara un xmlns por
    defecto, las comparaciones de texto como `tag == "seccion"` fallarían
    en silencio sin este paso (0 resultados, sin ningún error)."""
    for e in elem.iter():
        if isinstance(e.tag, str) and "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    return elem


def _fetch_xml(d: date) -> ET.Element:
    url = config.BOE_SUMARIO_URL.format(fecha=_fecha_str(d))
    r = httpx.get(url, headers={"Accept": "application/xml"}, timeout=config.REQUEST_TIMEOUT)
    r.raise_for_status()

    if config.DEBUG_BOE_PARSER:
        log.info("[BOE %s] HTTP %s, %d bytes, content-type=%s", d.isoformat(), r.status_code,
                  len(r.content), r.headers.get("content-type"))
        log.info("[BOE %s] primeros 1500 caracteres del cuerpo:\n%s", d.isoformat(), r.text[:1500])

    root = ET.fromstring(r.content)
    root = _strip_namespaces(root)
    return root


def sumario_publicado(d: date) -> bool:
    """Comprueba si el sumario del BOE de ese día ya existe (status/code == 200)."""
    try:
        root = _fetch_xml(d)
    except (httpx.HTTPError, ET.ParseError) as e:
        log.warning("Error comprobando sumario BOE: %s", e)
        return False

    codigo = root.findtext("status/code")
    if config.DEBUG_BOE_PARSER:
        log.info("[BOE %s] tag raíz=%s, status/code=%r", d.isoformat(), root.tag, codigo)
    return codigo == "200"


def _text(elem: ET.Element, tag: str) -> str:
    hijo = elem.find(tag)
    return (hijo.text or "").strip() if hijo is not None and hijo.text else ""


def _walk_items(elem: ET.Element, seccion_codigo: str | None = None, departamento_nombre: str = ""):
    """Recorre recursivamente el árbol buscando <item> dentro de las secciones
    de oposiciones (II.A / II.B), arrastrando el nombre del departamento."""
    tag = elem.tag
    if tag == "seccion":
        seccion_codigo = elem.get("codigo")
    if tag == "departamento":
        departamento_nombre = elem.get("nombre", departamento_nombre)
    if tag == "item" and seccion_codigo in config.BOE_SECCIONES_OPOSICIONES:
        yield elem, departamento_nombre
        return  # un <item> no contiene más <item> anidados
    for hijo in elem:
        yield from _walk_items(hijo, seccion_codigo, departamento_nombre)


def _diagnostico(root: ET.Element, d: date, encontrados: int):
    """Solo con DEBUG_BOE_PARSER=true: vuelca a los logs qué hay realmente en
    el árbol para poder comparar contra lo que se esperaba, sin adivinar."""
    todas_secciones = [(e.get("codigo"), e.get("nombre")) for e in root.iter("seccion")]
    total_items = len(list(root.iter("item")))
    log.info("[BOE %s] secciones encontradas en el XML: %s", d.isoformat(), todas_secciones)
    log.info("[BOE %s] total <item> en TODO el documento (todas las secciones): %d",
              d.isoformat(), total_items)
    log.info("[BOE %s] items dentro de las secciones de oposiciones %s: %d",
              d.isoformat(), sorted(config.BOE_SECCIONES_OPOSICIONES), encontrados)
    if encontrados == 0 and total_items > 0:
        log.warning(
            "[BOE %s] Hay %d <item> en el sumario pero NINGUNO cayó dentro de las "
            "secciones esperadas %s. Compara los códigos listados arriba en "
            "'secciones encontradas' con BOE_SECCIONES_OPOSICIONES en config.py: "
            "si no coinciden exactamente (mayúsculas, espacios, etc.), ahí está el fallo.",
            d.isoformat(), total_items, sorted(config.BOE_SECCIONES_OPOSICIONES),
        )
    if total_items == 0:
        log.warning(
            "[BOE %s] El XML no contiene NINGÚN <item> en todo el documento. "
            "O el sumario de ese día realmente no trae contenido en ninguna sección, "
            "o la estructura del XML real no es la esperada (revisa el volcado de "
            "'primeros 1500 caracteres del cuerpo' de este mismo log).",
            d.isoformat(),
        )


def obtener_items_oposiciones(d: date) -> list[ItemBOE]:
    """Descarga el sumario del día y devuelve los items de secciones de oposiciones."""
    root = _fetch_xml(d)
    fecha_iso = d.isoformat()
    resultado = []
    for item_elem, departamento in _walk_items(root):
        resultado.append(ItemBOE(
            cve_boe=_text(item_elem, "identificador"),
            titulo=_text(item_elem, "titulo"),
            url_pdf=_text(item_elem, "url_pdf"),
            url_html=_text(item_elem, "url_html"),
            departamento=departamento,
            fecha_publicacion=fecha_iso,
        ))

    if config.DEBUG_BOE_PARSER:
        _diagnostico(root, d, len(resultado))

    return resultado


def descargar_pdf(item: ItemBOE) -> bytes:
    r = httpx.get(item.url_pdf, timeout=config.REQUEST_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.content