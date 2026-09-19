# BOE Oposiciones Watcher (Docker + Discord + Ollama)

Versión dockerizada de la spec original (pensada para Android). Como no hay
app móvil, **Discord sustituye toda la UI**, y **Ollama sirve el LLM local**
en vez de embeber llama.cpp dentro del contenedor de la app.

| Spec original (Android) | Aquí (Docker + Discord + Ollama) |
|---|---|
| Notificación push | Mensaje/DM de Discord |
| Botón 💾 en la tarjeta | Botón de Discord bajo cada convocatoria |
| Pantalla "Hoy" | Comando `/hoy` |
| Pantalla "Guardados" | Comando `/guardados`, `/borrar_guardado`, `/vaciar_guardados` |
| Pantalla "Bandeja diaria" + botón "vaciar" | Comando `/vaciar_bandeja` |
| WorkManager 8:00 + recuperación si el móvil estaba apagado | Bucle que reintenta cada 15 min desde las 8:00 hasta ejecutar, idempotente |
| llama.cpp (JNI) en el Worker | **Ollama** (contenedor aparte), la app le habla por su API REST |
| Room (`bandeja_diaria`, `guardados`) | SQLite con el mismo esquema, en `./data` |

El resto del pipeline (extracción PDF → limpieza de ruido → chunking →
LLM con salida forzada a JSON → validación en dos capas) está implementado
tal cual describe `ESPECIFICACION.md`.

### ¿Por qué Ollama y no llama-cpp-python embebido?

La primera versión de este proyecto compilaba `llama-cpp-python` dentro del
contenedor de la app. Daba problemas de compatibilidad de librerías del
sistema (`libc`/`libgomp`) según la imagen base. Con Ollama como servicio
aparte:
- No hay que compilar nada en la imagen de la app (Dockerfile trivial).
- Ollama descarga y cachea el modelo él solo (`ollama pull`), no hace falta
  bajar el `.gguf` a mano de Hugging Face.
- Ollama detecta y usa GPU (NVIDIA vía NVIDIA Container Toolkit, o ROCm en
  AMD) automáticamente si está disponible, sin tocar nada de este proyecto.
- La salida JSON forzada se pide vía el parámetro `format` (JSON Schema) de
  la API de Ollama — funcionalmente equivalente al grammar-constrained
  decoding (GBNF) de la spec original, y construida en `classifier.py`
  directamente desde `categorias.py` (una sola fuente de verdad, sin
  gramática aparte que mantener sincronizada a mano).

---

## 1. Requisitos previos

### 1.1 Crear el bot de Discord y obtener el `.env`

1. Ve a https://discord.com/developers/applications → **New Application**.
2. Pestaña **Bot** → **Reset Token** → copia el token → `DISCORD_BOT_TOKEN`.
3. No hace falta activar "Message Content Intent" (solo se usan slash
   commands y DMs).
4. Pestaña **OAuth2 → URL Generator**: marca los scopes `bot` y
   `applications.commands`, y en permisos marca al menos `Send Messages`,
   `Embed Links`, `Use Application Commands`. Abre la URL generada e invita
   el bot **a un servidor donde tú también estés** (aunque uses DM, Discord
   exige que el bot comparta un servidor contigo para poder enviarte DMs).
5. Tu `DISCORD_USER_ID`: activa el modo desarrollador en Discord
   (Ajustes → Avanzado → Modo desarrollador), luego clic derecho sobre tu
   nombre → **Copiar ID de usuario**.
   - **Sí se usa**: es el destinatario del DM diario, y restringe todos los
     comandos a solo tu usuario. Si prefieres que el bot publique en un
     canal fijo en vez de por DM, pon ese canal en `DISCORD_CHANNEL_ID` y
     deja `DISCORD_USER_ID` vacío (en ese caso los comandos quedan abiertos
     a quien pueda escribir en ese canal).

### 1.2 El modelo LLM

No hace falta descargar nada a mano: el contenedor `ollama` del
`docker-compose.yml` **descarga el modelo la primera vez que arranca la
app** (`qwen2.5:3b-instruct-q4_K_M` por defecto, ~2 GB). Verás el progreso
en los logs (`docker compose logs -f boe-watcher`). Tarda varios minutos la
primera vez; las siguientes veces ya está cacheado en el volumen
`ollama_data`.

Si en pruebas el 3B se queda corto (spec sección 4), cambia
`OLLAMA_MODEL=gemma3:4b` (o el tag que prefieras del catálogo de Ollama:
https://ollama.com/library) en el `.env`.

---

## 2. Levantar el proyecto

```bash
docker compose up -d --build
docker compose logs -f
```

Al arrancar: crea `./data/boe_watcher.db` (SQLite), espera a que Ollama
esté listo y descarga el modelo si hace falta, conecta el bot, y desde ese
momento reintenta el chequeo diario cada 15 minutos a partir de
`RUN_TIME` (por defecto 08:00, hora `TZ=Europe/Madrid`) hasta que el
sumario del BOE esté publicado y el chequeo tenga éxito — sin duplicar,
igual que pedía la spec para el caso de "móvil apagado a las 8:00".

---

## 3. Comandos de Discord

Si has puesto `DISCORD_USER_ID` en el `.env`, **todos** los comandos están
restringidos a ese usuario (cualquier otra persona recibe "❌ No tienes
permiso para usar este bot"). Si solo usas `DISCORD_CHANNEL_ID` sin
`DISCORD_USER_ID`, los comandos quedan abiertos a quien pueda escribir en
ese canal.

**Bajo demanda:**
- `/boe` — consulta el BOE de hoy en el momento (sin esperar a las 8:00) y
  te envía el resultado por DM, con botón 💾 en cada convocatoria.
- `/estado` — cuánto falta para la próxima comprobación automática.
- `/test` — mensaje de prueba por DM, para verificar que el bot te puede
  escribir.
- `/borrar` — borra todos los mensajes que el bot te ha enviado por DM.
- `/help` — lista de comandos.

**Bandeja / guardados:**
- `/hoy` — lista lo que ya hay en la bandeja diaria (sin volver a consultar
  el BOE), con botón 💾 por convocatoria.
- `/guardados` — histórico persistente de lo guardado.
- `/borrar_guardado item_id:<n>` — borra un guardado (el id aparece en el
  pie de cada tarjeta: `id: N`).
- `/vaciar_guardados` — borra todo el histórico de guardados.
- `/vaciar_bandeja` — vacía manualmente la bandeja diaria (equivalente al
  botón "vaciar bandeja" de la spec).

---

## 4. Rendimiento: ¿te hace falta la GPU?

Con Ollama esto es automático: si tienes GPU NVIDIA con drivers y el
**NVIDIA Container Toolkit** instalados en el host, descomenta el bloque
`deploy.resources.reservations` del servicio `ollama` en el
`docker-compose.yml` y listo — Ollama la detecta y la usa sola, sin tocar
nada más de este proyecto.

Si no tienes GPU NVIDIA (por ejemplo GPU Intel/AMD integrada), Ollama corre
en CPU sin más configuración. Ten en cuenta que el pipeline corre **una vez
al día**, procesa unos pocos PDFs de 1-4 páginas, y la propia spec estima
10-30 s por documento en CPU (sección 4): no es una carga interactiva, así
que aunque tarde 1-3 minutos en total no lo notarás, ocurre en segundo
plano a las 8:00.

---

## 5. Notas de implementación / lo que queda pendiente (spec sección 7)

- **Taxonomía de categorías**: definida en `app/categorias.py`
  (`CATEGORIAS_CANONICAS`), y usada directamente para construir el JSON
  Schema que se le pasa a Ollama en `app/classifier.py` — una sola fuente
  de verdad, sin nada más que sincronizar a mano.
- **Prompt + ejemplos de anclaje**: en `app/classifier.py`
  (`_SYSTEM_PROMPT`), con un ejemplo en formato texto y otro en formato
  tabla, como pide la spec (sección 4).
- **Salida forzada a JSON**: vía el parámetro `format` (JSON Schema) de la
  API de Ollama (`app/ollama_client.py`), equivalente funcional al
  grammar-constrained decoding (GBNF) de la spec original.
- **Validación con PDFs reales**: no incluida aquí — recomiendo probar
  contra sumarios reales de días con muchas convocatorias (Ministerio de
  Hacienda, ayuntamientos grandes) antes de confiar en el recuento
  heurístico de filas de `app/pdf_extractor.py`.
- **"Modelo cargado una vez al día"**: Ollama mantiene el modelo en memoria
  durante todo un chequeo (`OLLAMA_KEEP_ALIVE`, por defecto 5 min, para no
  recargarlo entre cada PDF del mismo día) y `Clasificador.cerrar()` fuerza
  su descarga explícita al terminar el chequeo, para que no quede cargado
  el resto del día.
- El parseo del XML/JSON del sumario del BOE (`app/boe_client.py`) intenta
  cubrir la estructura documentada, pero **la API del BOE no tiene un
  esquema JSON 100% estable entre secciones/departamentos** — si algún día
  no detecta items que sí existen, revisa `_iter_items`/`_item_from_raw`
  contra la respuesta real de ese día (`curl` al endpoint del sumario).

## 6. Estructura del proyecto

```
boe-watcher/
├── docker-compose.yml        # servicios: ollama + boe-watcher
├── Dockerfile                # imagen de la app (sin compilación, solo pip install)
├── requirements.txt
├── .env                      # tus credenciales (no lo subas a git)
├── data/                     # SQLite (se crea solo)
└── app/
    ├── config.py
    ├── categorias.py          # taxonomía cerrada (fuente de verdad del JSON schema)
    ├── boe_client.py          # API del BOE (sumario + descarga PDF)
    ├── pdf_extractor.py       # extracción + limpieza de ruido + heurística texto/tabla
    ├── chunker.py             # DocumentChunker (trocea solo si no cabe en contexto)
    ├── ollama_client.py       # cliente REST de Ollama (esperar/pull/chat/descargar)
    ├── classifier.py          # prompt + JSON schema + llamadas a Ollama
    ├── validation.py          # validación post-LLM en dos capas
    ├── db.py                  # SQLite: bandeja_diaria + guardados
    ├── discord_bot.py         # interfaz: notificaciones, botones, slash commands
    ├── scheduler.py           # 8:00 + reintento sin duplicar
    ├── pipeline.py            # orquestación diaria y bajo demanda
    └── main.py                # entrypoint
```
