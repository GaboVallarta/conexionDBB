## Bot de Telegram (`Bot_telegram/bot (2).py`)

Bot interactivo conectado a **Supabase** que permite a los usuarios consultar en tiempo real la cantidad de pasajeros y el estado de las unidades por ruta (`C110-B` y `C72`).

### Funcionalidades principales
* **Consulta en tiempo real:** Obtiene el último registro de ocupación (`pasajeros_totales` y `estado_actual`) directamente desde la tabla `registros_ocupacion` en Supabase.
* **Interacción amigable:** Funciona mediante un menú de botones fijos o escribiendo mensajes naturales (ej. *"hola"*, *"¿cómo va la C72?"*), sin necesidad de escribir comandos técnicos.
* **Alertas automáticas:** Los usuarios pueden suscribirse para recibir notificaciones automáticas cada 30 segundos cuando alguna unidad reporte estado `Lleno`.
* **Persistencia local:** Guarda los usuarios suscritos en `datos_bot.pkl` para no perder las suscripciones si el servidor o la Raspberry Pi se reinicia.

###  Configuración y ejecución local
1. **Instalar dependencias:**
   ```bash
   pip install "python-telegram-bot[job-queue]" httpx python-dotenv
