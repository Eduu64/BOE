"""
Modelo de datos en dos niveles (spec sección 5):
- bandeja_diaria: auto-expira a los N días (config.BANDEJA_EXPIRA_DIAS) o borrado manual.
- guardados: persiste indefinidamente, solo se borra a mano.

Un mismo PDF puede generar varios registros (uno por fila/puesto del array
"convocatorias"), todos compartiendo el mismo cve_boe.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta

from . import config

_ESQUEMA_CAMPOS = """
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cve_boe TEXT NOT NULL,
    fecha_publicacion TEXT NOT NULL,
    puesto TEXT,
    organismo TEXT,
    plazas INTEGER,
    turno TEXT,
    grupo TEXT,               -- JSON list
    categorias TEXT,          -- JSON list
    formato_fuente TEXT,
    hereda_contexto_general INTEGER,
    texto_extraido_resumen TEXT,
    url_pdf TEXT,
    url_html TEXT,
    guardado INTEGER DEFAULT 0,
    fecha_guardado TEXT,
    revisar_manualmente INTEGER DEFAULT 0
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as c:
        c.execute(f"CREATE TABLE IF NOT EXISTS bandeja_diaria ({_ESQUEMA_CAMPOS})")
        c.execute(f"CREATE TABLE IF NOT EXISTS guardados ({_ESQUEMA_CAMPOS})")
        c.execute("""CREATE TABLE IF NOT EXISTS ejecuciones (
            fecha TEXT PRIMARY KEY,
            completada_en TEXT NOT NULL
        )""")


def ya_ejecutado_hoy(fecha_iso: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT 1 FROM ejecuciones WHERE fecha = ?", (fecha_iso,)).fetchone()
        return row is not None
    
def borrar_marca_ejecutado(fecha_iso: str):
    """Quita la marca de 'ya procesado' de una fecha, para poder forzar un
    re-chequeo (usado por /boe con forzar=true)."""
    with _conn() as c:
        c.execute("DELETE FROM ejecuciones WHERE fecha = ?", (fecha_iso,))


def borrar_bandeja_fecha(fecha_iso: str):
    """Borra los registros de bandeja_diaria de una fecha concreta, para no
    duplicar filas al forzar un re-chequeo de un día ya procesado."""
    with _conn() as c:
        c.execute("DELETE FROM bandeja_diaria WHERE fecha_publicacion = ?", (fecha_iso,))


def marcar_ejecutado(fecha_iso: str):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO ejecuciones (fecha, completada_en) VALUES (?, datetime('now'))",
            (fecha_iso,),
        )


def insertar_convocatoria(item, convocatoria: dict, texto_resumen: str, revisar: bool) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO bandeja_diaria
            (cve_boe, fecha_publicacion, puesto, organismo, plazas, turno, grupo, categorias,
             formato_fuente, hereda_contexto_general, texto_extraido_resumen, url_pdf, url_html,
             guardado, revisar_manualmente)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
            (
                item.cve_boe,
                item.fecha_publicacion,
                convocatoria.get("puesto", ""),
                convocatoria.get("organismo", ""),
                convocatoria.get("plazas", 0),
                convocatoria.get("turno", "no_especificado"),
                json.dumps(convocatoria.get("grupo", []), ensure_ascii=False),
                json.dumps(convocatoria.get("categorias", []), ensure_ascii=False),
                convocatoria.get("formato_fuente", "texto"),
                int(bool(convocatoria.get("hereda_contexto_general", False))),
                texto_resumen,
                item.url_pdf,
                item.url_html,
                int(revisar),
            ),
        )
        return cur.lastrowid


def listar_bandeja(fecha_iso: str | None = None) -> list[sqlite3.Row]:
    with _conn() as c:
        if fecha_iso:
            return c.execute(
                "SELECT * FROM bandeja_diaria WHERE fecha_publicacion = ? ORDER BY id", (fecha_iso,)
            ).fetchall()
        return c.execute("SELECT * FROM bandeja_diaria ORDER BY fecha_publicacion DESC, id").fetchall()


def obtener_bandeja_item(item_id: int) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM bandeja_diaria WHERE id = ?", (item_id,)).fetchone()


def guardar_item(item_id: int) -> bool:
    """Copia un registro de bandeja_diaria a guardados (botón 💾)."""
    with _conn() as c:
        row = c.execute("SELECT * FROM bandeja_diaria WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return False
        ya = c.execute("SELECT 1 FROM guardados WHERE cve_boe=? AND puesto=? AND organismo=?",
                        (row["cve_boe"], row["puesto"], row["organismo"])).fetchone()
        if ya:
            return True
        cols = [d[0] for d in row.keys() if d[0] not in ("id",)]
        campos = [k for k in row.keys() if k != "id"]
        valores = [row[k] for k in campos]
        placeholders = ",".join(["?"] * len(campos))
        campos_sql = ",".join(campos)
        c.execute(f"INSERT INTO guardados ({campos_sql}) VALUES ({placeholders})", valores)
        c.execute("UPDATE bandeja_diaria SET guardado=1, fecha_guardado=datetime('now') WHERE id=?", (item_id,))
        return True


def listar_guardados() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM guardados ORDER BY fecha_publicacion DESC, id").fetchall()


def borrar_guardado(item_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM guardados WHERE id=?", (item_id,))
        return cur.rowcount > 0


def vaciar_guardados():
    with _conn() as c:
        c.execute("DELETE FROM guardados")


def vaciar_bandeja():
    with _conn() as c:
        c.execute("DELETE FROM bandeja_diaria")


def purgar_bandeja_expirada():
    limite = (date.today() - timedelta(days=config.BANDEJA_EXPIRA_DIAS)).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM bandeja_diaria WHERE fecha_publicacion < ?", (limite,))
