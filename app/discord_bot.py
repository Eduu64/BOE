"""
Discord como ÚNICA interfaz (sustituye la UI Android de la spec original):
- Notificación automática -> mensaje/DM de Discord al completar el chequeo diario.
- Botón de guardado (💾) -> componente Button de Discord bajo cada convocatoria.
- Pantallas "Hoy" / "Guardados" / "vaciar bandeja" -> slash commands.
- Comandos bajo demanda (/boe, /estado, /test, /borrar, /help), pensados para
  uso personal: solo DISCORD_USER_ID puede ejecutarlos si está configurado.
"""
import asyncio
import json
import logging
from datetime import date, datetime

import discord
from discord import app_commands

from . import config, db
from .pipeline import forzar_chequeo
from .scheduler import get_time_until_next_run

log = logging.getLogger(__name__)

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

_MAX_CAMPOS_EMBED = 25


def is_authorized(user_id: int) -> bool:
    """Restringe los comandos a tu propio usuario si DISCORD_USER_ID está configurado.
    Si solo usas DISCORD_CHANNEL_ID (modo canal, sin DM), se asume canal privado/de confianza
    y se permite a cualquiera que pueda escribir ahí."""
    if not config.DISCORD_USER_ID:
        return True
    return str(user_id) == str(config.DISCORD_USER_ID)


async def _denegar_si_no_autorizado(interaction: discord.Interaction) -> bool:
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("❌ No tienes permiso para usar este bot.", ephemeral=True)
        return True
    return False


def _fmt_lista(campo_json: str) -> str:
    try:
        vals = json.loads(campo_json or "[]")
        return ", ".join(vals) if vals else "—"
    except (json.JSONDecodeError, TypeError):
        return "—"


def _embed_convocatoria(row) -> discord.Embed:
    color = discord.Color.orange() if row["revisar_manualmente"] else discord.Color.blurple()
    e = discord.Embed(title=row["puesto"] or "(sin título)", color=color)
    e.add_field(name="Organismo", value=row["organismo"] or "—", inline=False)
    e.add_field(name="Plazas", value=str(row["plazas"] or 0))
    e.add_field(name="Turno", value=row["turno"] or "no_especificado")
    e.add_field(name="Grupo", value=_fmt_lista(row["grupo"]))
    e.add_field(name="Categorías", value=_fmt_lista(row["categorias"]))
    if row["revisar_manualmente"]:
        e.add_field(name="⚠️", value="Marcado para revisión manual", inline=False)
    if row["url_pdf"]:
        e.add_field(name="PDF", value=row["url_pdf"], inline=False)
    e.set_footer(text=f"cve: {row['cve_boe']} · id: {row['id']}")
    return e


class GuardarView(discord.ui.View):
    def __init__(self, item_id: int, ya_guardado: bool = False):
        super().__init__(timeout=None)
        self.item_id = item_id
        boton = discord.ui.Button(
            label="Guardado ✅" if ya_guardado else "Guardar 💾",
            style=discord.ButtonStyle.success if ya_guardado else discord.ButtonStyle.secondary,
            custom_id=f"guardar:{item_id}",
            disabled=ya_guardado,
        )
        boton.callback = self._on_click
        self.add_item(boton)

    async def _on_click(self, interaction: discord.Interaction):
        if not is_authorized(interaction.user.id):
            await interaction.response.send_message("❌ No tienes permiso para usar este bot.", ephemeral=True)
            return
        ok = db.guardar_item(self.item_id)
        if ok:
            nueva_view = GuardarView(self.item_id, ya_guardado=True)
            await interaction.response.edit_message(view=nueva_view)
        else:
            await interaction.response.send_message("No se encontró esa convocatoria.", ephemeral=True)


async def _destino():
    """Canal o usuario (DM) donde enviar las notificaciones AUTOMÁTICAS del scheduler."""
    if config.DISCORD_CHANNEL_ID:
        canal = client.get_channel(int(config.DISCORD_CHANNEL_ID))
        if canal:
            return canal
    if config.DISCORD_USER_ID:
        user = await client.fetch_user(int(config.DISCORD_USER_ID))
        return await user.create_dm()
    raise RuntimeError("No hay DISCORD_CHANNEL_ID ni DISCORD_USER_ID configurados.")


async def notificar_resultado_diario(fecha_iso: str):
    rows = db.listar_bandeja(fecha_iso)
    destino = await _destino()

    if not rows:
        await destino.send(f"📋 Chequeo del BOE ({fecha_iso}) completado: no se han encontrado oposiciones hoy.")
        return

    resumen = discord.Embed(
        title=f"📋 BOE {fecha_iso}: {len(rows)} convocatoria(s) encontradas",
        color=discord.Color.green(),
    )
    revisar = sum(1 for r in rows if r["revisar_manualmente"])
    if revisar:
        resumen.description = f"⚠️ {revisar} marcada(s) para revisión manual."
    await destino.send(embed=resumen)

    for row in rows:
        await destino.send(embed=_embed_convocatoria(row), view=GuardarView(row["id"]))


# --- Comandos "de bandeja / guardados" ---

@tree.command(name="hoy", description="Muestra las oposiciones detectadas en el último chequeo")
async def cmd_hoy(interaction: discord.Interaction):
    if await _denegar_si_no_autorizado(interaction):
        return
    rows = db.listar_bandeja()
    if not rows:
        await interaction.response.send_message("La bandeja diaria está vacía.", ephemeral=True)
        return
    await interaction.response.send_message(f"Bandeja diaria: {len(rows)} convocatoria(s).", ephemeral=True)
    for row in rows[:_MAX_CAMPOS_EMBED]:
        await interaction.followup.send(embed=_embed_convocatoria(row), view=GuardarView(row["id"], bool(row["guardado"])))


@tree.command(name="guardados", description="Muestra tu histórico de oposiciones guardadas")
async def cmd_guardados(interaction: discord.Interaction):
    if await _denegar_si_no_autorizado(interaction):
        return
    rows = db.listar_guardados()
    if not rows:
        await interaction.response.send_message("No tienes oposiciones guardadas todavía.", ephemeral=True)
        return
    await interaction.response.send_message(f"Guardados: {len(rows)} convocatoria(s).", ephemeral=True)
    for row in rows[:_MAX_CAMPOS_EMBED]:
        await interaction.followup.send(embed=_embed_convocatoria(row))


@tree.command(name="vaciar_bandeja", description="Borra manualmente toda la bandeja diaria")
async def cmd_vaciar_bandeja(interaction: discord.Interaction):
    if await _denegar_si_no_autorizado(interaction):
        return
    db.vaciar_bandeja()
    await interaction.response.send_message("🗑️ Bandeja diaria vaciada.", ephemeral=True)


@tree.command(name="borrar_guardado", description="Borra un guardado por su id")
@app_commands.describe(item_id="id que aparece en el pie del mensaje (id: N)")
async def cmd_borrar_guardado(interaction: discord.Interaction, item_id: int):
    if await _denegar_si_no_autorizado(interaction):
        return
    ok = db.borrar_guardado(item_id)
    msg = "🗑️ Borrado." if ok else "No se encontró ese id en guardados."
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="vaciar_guardados", description="Borra TODO el histórico de guardados")
async def cmd_vaciar_guardados(interaction: discord.Interaction):
    if await _denegar_si_no_autorizado(interaction):
        return
    db.vaciar_guardados()
    await interaction.response.send_message("🗑️ Guardados vaciados por completo.", ephemeral=True)


# --- Comandos bajo demanda (integrados del otro bot) ---

@tree.command(name="boe", description="Lee el BOE de una fecha (por defecto hoy) y te envía los resultados por privado.")
@app_commands.describe(
    fecha="Fecha a consultar, formato DD-MM-AAAA (por defecto hoy). Útil para probar en fin de semana con un día laborable pasado, ej: 18-09-2026.",
    forzar="Ignora si esa fecha ya se comprobó antes y vuelve a consultar el BOE de cero (borra lo que hubiera guardado de ese día). Por defecto: false.",
)
async def cmd_boe(interaction: discord.Interaction, fecha: str = None, forzar: bool = False):
    if await _denegar_si_no_autorizado(interaction):
        return

    fecha_dt = date.today()
    if fecha:
        try:
            fecha_dt = datetime.strptime(fecha, "%d-%m-%Y").date()
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Formato de fecha inválido. Usa DD-MM-AAAA, ej: 18-09-2026.", ephemeral=True
            )
            return

    await interaction.response.defer(ephemeral=True)
    # forzar_chequeo hace peticiones de red bloqueantes: se ejecuta en un hilo
    # aparte para no bloquear el bucle de eventos de Discord.
    estado, fecha_iso = await asyncio.to_thread(forzar_chequeo, fecha_dt, forzar)

    if estado == "no_publicado":
        await interaction.followup.send(
            f"⏳ El sumario del BOE de {fecha_dt.strftime('%d-%m-%Y')} aún no está publicado "
            "(recuerda: los fines de semana y festivos el BOE no publica sumario de oposiciones).",
            ephemeral=True,
        )
        return
    if estado == "error":
        await interaction.followup.send("⚠️ No se ha podido consultar el BOE ahora mismo (fallo de red). Inténtalo de nuevo en unos minutos.", ephemeral=True)
        return

    rows = db.listar_bandeja(fecha_iso)
    if not rows:
        await interaction.followup.send(f"✅ Comprobación completada: no hay oposiciones para el {fecha_dt.strftime('%d-%m-%Y')}.", ephemeral=True)
        return

    # Un mensaje por convocatoria (así cada una lleva su propio botón 💾, que un
    # embed combinado no permite).
    for row in rows:
        await interaction.user.send(embed=_embed_convocatoria(row), view=GuardarView(row["id"], bool(row["guardado"])))

    await interaction.followup.send(
        f"✅ Comprobación del {fecha_dt.strftime('%d-%m-%Y')} completada y enviada a tus mensajes privados "
        f"({len(rows)} convocatoria(s)).",
        ephemeral=True,
    )


@tree.command(name="estado", description="Muestra cuánto falta para la siguiente comprobación automática del BOE.")
async def cmd_estado(interaction: discord.Interaction):
    if await _denegar_si_no_autorizado(interaction):
        return
    proximo, horas, minutos = get_time_until_next_run(config.RUN_TIME)
    await interaction.response.send_message(
        f"⏰ Próxima comprobación automática: **{proximo.strftime('%d/%m/%Y %H:%M')}** "
        f"(Quedan: **{horas}h {minutos}m**).",
        ephemeral=True,
    )


@tree.command(name="test", description="Envía un mensaje de prueba a tus privados para verificar que todo funciona.")
async def cmd_test(interaction: discord.Interaction):
    if await _denegar_si_no_autorizado(interaction):
        return
    proximo, horas, minutos = get_time_until_next_run(config.RUN_TIME)
    await interaction.user.send(
        f"🤖 **Prueba de conexión BOE Watcher**\n"
        f"✅ El servicio y las notificaciones privadas funcionan correctamente.\n"
        f"⏰ Próxima comprobación programada: **{proximo.strftime('%d/%m/%Y %H:%M')}** ({horas}h {minutos}m)."
    )
    await interaction.response.send_message("✅ Mensaje de prueba enviado a tus privados.", ephemeral=True)


@tree.command(name="borrar", description="Borra los mensajes que el bot te ha enviado por privado.")
async def cmd_borrar(interaction: discord.Interaction):
    if await _denegar_si_no_autorizado(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        canal = interaction.user.dm_channel or await interaction.user.create_dm()
        borrados = 0
        async for mensaje in canal.history(limit=None):
            if mensaje.author.id == client.user.id:
                try:
                    await mensaje.delete()
                    borrados += 1
                except discord.HTTPException as e:
                    log.debug("No se pudo borrar un mensaje (%s): %s", mensaje.id, e)
        await interaction.followup.send(f"🧹 Borrados {borrados} mensaje(s) del bot en tus privados.", ephemeral=True)
    except Exception:
        log.exception("Error borrando mensajes de Discord")
        await interaction.followup.send("⚠️ Ocurrió un error al intentar borrar los mensajes. Revisa los logs.", ephemeral=True)


@tree.command(name="help", description="Muestra la lista de comandos disponibles y su funcionamiento.")
async def cmd_help(interaction: discord.Interaction):
    if await _denegar_si_no_autorizado(interaction):
        return
    ayuda_texto = (
        "📖 **Comandos disponibles en BOE Oposiciones Watcher**\n\n"
        "🔹 **/boe [fecha] [forzar]** : Lee el BOE de una fecha (por defecto hoy, formato DD-MM-AAAA) y te manda el resultado por mensaje privado, con botón 💾. `forzar:true` repite la consulta aunque esa fecha ya se hubiera comprobado antes.\n"        "🔹 **/hoy** : Muestra lo que ya hay en la bandeja diaria (sin volver a consultar el BOE).\n"
        "🔹 **/guardados** : Histórico persistente de lo que has guardado.\n"
        "🔹 **/borrar_guardado item_id:<n>** : Borra un guardado por su id.\n"
        "🔹 **/vaciar_guardados** : Borra todo el histórico de guardados.\n"
        "🔹 **/vaciar_bandeja** : Vacía manualmente la bandeja diaria.\n"
        "🔹 **/estado** : Tiempo restante para la siguiente lectura automática.\n"
        "🔹 **/test** : Envía un mensaje de prueba a tus privados.\n"
        "🔹 **/borrar** : Borra todos los mensajes que el bot te ha enviado por privado.\n"
        "🔹 **/help** : Muestra este mensaje.\n\n"
        f"⚙️ *Hora programada diaria:* **{config.RUN_TIME}** ({config.TIMEZONE})."
    )
    await interaction.response.send_message(ayuda_texto, ephemeral=True)


@client.event
async def on_ready():
    await tree.sync()
    log.info("Bot de Discord conectado como %s", client.user)


async def iniciar_bot():
    await client.start(config.DISCORD_BOT_TOKEN)