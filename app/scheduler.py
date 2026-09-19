"""
Programación del chequeo diario (spec sección 8: WorkManager anclado a las 8:00
con recuperación si el dispositivo estaba apagado, sin duplicar ejecuciones).

Equivalente aquí: un contenedor puede estar parado a las 8:00, o el sumario del
BOE puede tardar en publicarse. Por eso, en vez de un único disparo puntual,
se reintenta cada POLL_MINUTES minutos desde RUN_TIME en adelante, cada día,
hasta que `ejecutar_chequeo_diario` tenga éxito (idempotente: usa la tabla
`ejecuciones` para no duplicar aunque se reintente).
"""
import asyncio
import datetime as dt
import logging

from . import config, db, discord_bot
from .pipeline import ejecutar_chequeo_diario

log = logging.getLogger(__name__)

POLL_MINUTES = 15


def _hora_alcanzada(ahora: dt.datetime) -> bool:
    objetivo = ahora.replace(hour=config.RUN_HOUR, minute=config.RUN_MINUTE, second=0, microsecond=0)
    return ahora >= objetivo


def get_time_until_next_run(run_time: str | None = None) -> tuple[dt.datetime, int, int]:
    """Usado por /estado: devuelve (fecha/hora del próximo chequeo, horas, minutos restantes)."""
    hora, minuto = (int(x) for x in (run_time or config.RUN_TIME).split(":"))
    ahora = dt.datetime.now()
    proximo = ahora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if proximo <= ahora or db.ya_ejecutado_hoy(ahora.date().isoformat()):
        proximo += dt.timedelta(days=1)
    restante = proximo - ahora
    horas, resto = divmod(int(restante.total_seconds()), 3600)
    minutos = resto // 60
    return proximo, horas, minutos


async def bucle_scheduler():
    """Bucle asíncrono: se ejecuta indefinidamente junto al bot de Discord."""
    while True:
        try:
            ahora = dt.datetime.now()
            if _hora_alcanzada(ahora) and not db.ya_ejecutado_hoy(ahora.date().isoformat()):
                log.info("Intentando chequeo diario para %s", ahora.date().isoformat())
                fecha_procesada = await asyncio.to_thread(ejecutar_chequeo_diario, ahora.date())
                if fecha_procesada:
                    await discord_bot.notificar_resultado_diario(fecha_procesada)
        except Exception:
            log.exception("Error en el bucle del scheduler")

        await asyncio.sleep(POLL_MINUTES * 60)
