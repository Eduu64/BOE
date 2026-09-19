import asyncio
import logging

from . import config, db, discord_bot, ollama_client
from .scheduler import bucle_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("boe_watcher")


async def _main():
    if not config.DISCORD_BOT_TOKEN:
        raise RuntimeError("Falta DISCORD_BOT_TOKEN en el .env")
    if not config.DISCORD_USER_ID and not config.DISCORD_CHANNEL_ID:
        raise RuntimeError("Configura DISCORD_USER_ID (para DM) o DISCORD_CHANNEL_ID (para un canal fijo)")

    db.init_db()

    log.info("Comprobando conexión con Ollama en %s ...", config.OLLAMA_HOST)
    await asyncio.to_thread(ollama_client.esperar_ollama)
    log.info("Ollama listo. Asegurando modelo %s (se descarga si es la primera vez)...", config.OLLAMA_MODEL)
    await asyncio.to_thread(ollama_client.asegurar_modelo)
    log.info("Modelo disponible.")

    async with asyncio.TaskGroup() as tg:
        tg.create_task(discord_bot.iniciar_bot())
        tg.create_task(_esperar_bot_y_lanzar_scheduler())


async def _esperar_bot_y_lanzar_scheduler():
    await discord_bot.client.wait_until_ready()
    log.info("Bot listo, arrancando scheduler diario.")
    await bucle_scheduler()


if __name__ == "__main__":
    asyncio.run(_main())
