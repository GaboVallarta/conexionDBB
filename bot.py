import os
import httpx
from dotenv import load_dotenv

load_dotenv()

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    PicklePersistence,
    filters,
)

# Variables extraídas de forma segura (Asegúrate de tenerlas en tu .env)
TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

RUTAS = ["C110-B", "C72"]

# Textos de los botones fijos
BTN_FLUJO = "🚌 Ver flujo de personas"
BTN_SUSCRIBIR = "🔔 Avisarme si hay mucha gente"
BTN_CANCELAR = "🔕 Dejar de avisarme"

MENU = ReplyKeyboardMarkup(
    [[BTN_FLUJO], [BTN_SUSCRIBIR, BTN_CANCELAR]],
    resize_keyboard=True,
)

async def obtener_flujo(ruta: str):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Estructura del endpoint de Supabase (PostgREST)
            # Nota: Esto es una consulta unida (JOIN) conceptual.
            url = f"{SUPABASE_URL}/rest/v1/unidades?select=estado_actual,rutas!inner(codigo),registros_ocupacion(pasajeros_totales)&rutas.codigo=eq.{ruta}&order=id.desc&limit=1"
            
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}"
            }
            
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()

            if not data:
                return None

            # Extrayendo los nombres exactos de tus tablas
            # data[0] porque Supabase siempre devuelve una lista en lecturas REST
            return {
                "ruta": ruta,
                "pasajeros_totales": data[0]["registros_ocupacion"][0]["pasajeros_totales"],
                "estado_actual": data[0]["estado_actual"],
            }
    except Exception as e:
        print(f"Error consultando la API: {e}")
        return None

# ---------- Acciones reutilizables ----------

async def mostrar_flujo(chat, ruta: str):
    d = await obtener_flujo(ruta) # CORRECCIÓN: Se agregó 'await'
    
    if not d:
        await chat.send_message(f"Lo siento, no pude obtener datos actuales de la ruta {ruta}. 😔")
        return

    await chat.send_message(
        f"🚌 Ruta {d['ruta']}: {d['pasajeros_totales']} personas "
        f"(estado: {d['estado_actual']})"
    )

async def pedir_ruta(update: Update):
    botones = [[InlineKeyboardButton(r, callback_data=f"ruta:{r}")] for r in RUTAS]
    await update.effective_chat.send_message(
        "¿De qué ruta quieres saber?", reply_markup=InlineKeyboardMarkup(botones)
    )

async def suscribir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data.setdefault("suscritos", set()).add(update.effective_chat.id)
    await update.effective_chat.send_message(
        "Listo, te avisaré cuando haya mucha gente. 🔔"
    )

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data.setdefault("suscritos", set()).discard(update.effective_chat.id)
    await update.effective_chat.send_message(
        "Listo, ya no te enviaré avisos. 🔕"
    )

# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! 👋 Puedo decirte cuánta gente hay en las rutas.\n"
        "Usa los botones de abajo o escríbeme normal, por ejemplo: "
        "\"¿cómo va la C72?\"",
        reply_markup=MENU,
    )

async def mensaje_libre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    t = texto.lower()

    if texto == BTN_FLUJO:
        return await pedir_ruta(update)
    if texto == BTN_SUSCRIBIR:
        return await suscribir(update, context)
    if texto == BTN_CANCELAR:
        return await cancelar(update, context)

    if any(p in t for p in ["dejar de avis", "no me avis", "cancelar", "desuscrib"]):
        return await cancelar(update, context)

    for r in RUTAS:
        if r.lower() in t:
            return await mostrar_flujo(update.effective_chat, r)

    if any(p in t for p in ["avis", "alerta", "notific", "suscrib"]):
        return await suscribir(update, context)
    if any(p in t for p in ["flujo", "gente", "lleno", "llena", "ocupa", "personas", "ruta"]):
        return await pedir_ruta(update)

    await update.message.reply_text(
        "No te entendí 🤔. Usa los botones de abajo o dime una ruta "
        f"({', '.join(RUTAS)}).",
        reply_markup=MENU,
    )

async def boton_ruta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ruta = query.data.split(":", 1)[1]
    await mostrar_flujo(update.effective_chat, ruta)

async def revisar_alertas(context: ContextTypes.DEFAULT_TYPE):
    for ruta in RUTAS:
        d = await obtener_flujo(ruta) # CORRECCIÓN: Se agregó 'await' y se revisa la ruta
        
        # CORRECCIÓN: Evitar fallo si 'd' es None. 
        # Cambia 'Lleno' por la palabra exacta que tu Raspberry guarda en estado_actual
        if d and d["estado_actual"] == "Lleno": 
            for chat_id in context.application.bot_data.get("suscritos", set()):
                await context.bot.send_message(
                    chat_id, f"⚠️ Alerta: La ruta {d['ruta']} se encuentra {d['estado_actual']}."
                )

def main():
    if not TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
        print("Error: Faltan variables de entorno en el archivo .env")
        return

    persistence = PicklePersistence(filepath="datos_bot.pkl")
    app = Application.builder().token(TOKEN).persistence(persistence).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(boton_ruta, pattern=r"^ruta:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_libre))

    app.job_queue.run_repeating(revisar_alertas, interval=30, first=5)
    app.run_polling()

if __name__ == "__main__":
    main()