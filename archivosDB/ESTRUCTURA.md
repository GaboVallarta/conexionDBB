# archivosDB — Backend

Este directorio contiene los scripts de comunicación entre el hardware IoT (Raspberry Pi / Jetson) y la base de datos en **Supabase (PostgreSQL)**, así como la documentación de la arquitectura relacional y la automatización SQL del sistema de monitoreo de transporte público.

## Contenido de la Carpeta

* `raspDB.py` : Script en Python ejecutado en la Raspberry Pi para autenticar la unidad mediante Supabase Auth e insertar paquetes JSON
* `bot.py`: Bot asíncrono de Telegram que consulta los niveles de ocupación en la nube para notificar a los usuarios.
* `schema_y_triggers.sql`: Respaldo en texto plano de la función y el trigger SQL desplegados en Supabase.
* `.gitignore`: Reglas de exclusión para impedir que credenciales sensibles (`.env`) y entornos virtuales (`venv/`) se suban al repositorio remoto.
* `.env` *(archivo local, no versionado)*: Almacena `SUPABASE_URL` y `SUPABASE_KEY`.

---

## Arquitectura de la Base de Datos (Supabase con PostgreSQL)

El esquema `public` opera bajo políticas de **Row Level Security (RLS)** —escritura restringida a hardware autenticado y lectura pública para la app móvil en Ionic con Angular— y se divide en cuatro tablas conectadas mediante llaves foráneas (`uuid`):

| Tabla | Rol en el Sistema | Columnas y Restricciones Clave |
| :--- | :--- | :--- |
| **`rutas`** | Catálogo de líneas de transporte. | `id` (`uuid`, PK). |
| **`paradas`** | Coordenadas geográficas fijas de las estaciones para renderizado en AAAAAAAA. | `id` (`uuid`, PK), `latitud` (`float8`), `longitud` (`float8`). |
| **`unidades`** | Inventario de camiones y **caché de estado en tiempo real** consumida por el frontend. | `id` (`uuid`, PK), `ruta_id` (FK), `placa` (`text`, `UNIQUE`, `NOT NULL`), `pasajeros_actuales`, `estado_actual`, `ultima_parada_id` (FK $\rightarrow$ `paradas.id`, `ON DELETE SET NULL`). |
| **`registros_ocupacion`** | Historial de eventos capturados en cada parada. | `id` (`uuid`, PK), `unidad_id` (FK), `parada_id` (FK), `ingresaron`, `salieron`, `pasajeros_totales`, `fecha_hora` (`timestamptz` UTC). |

---

## Automatización en Base de Datos (Trigger SQL)

Para evitar que la Raspberry Pi realice múltiples peticiones HTTP por parada y evitar que la aplicación móvil procese todo el historial de telemetría, la sincronización del estado vivo ocurre automáticamente dentro de PostgreSQL:

1. **Disparador (`trigger_actualizar_camion`):** Se activa `AFTER INSERT` por cada fila nueva insertada en `public.registros_ocupacion`.
2. **Función (`actualizar_estado_camion()`):** Toma `NEW.parada_id` y `NEW.pasajeros_totales` del registro entrante y sobrescribe `ultima_parada_id` y `pasajeros_actuales` en la fila correspondiente de `public.unidades`.

---

## Configuración e Instalación en Raspberry Pi

1. **Crear el entorno virtual e instalar dependencias:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install supabase python-dotenv
   ```

2. **Configurar variables de entorno locales:**
   Crear un archivo `.env` dentro de esta carpeta con las llaves del proyecto en Supabase:
   ```env
   SUPABASE_URL=""
   SUPABASE_KEY=""
   ```

3. **Ejecutar validación de conexión:**
   ```bash
   python raspDB.py
   ```
