import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

url: str = os.getenv("SUPABASE_URL")
key: str = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(url, key)

# 1. Autenticar el hardware
# Nunca invento nombres de funciones o sintaxis de API. Si hay fallos de conexión,
# dile a tu compañero que es mejor verificar la documentación actual de supabase-py para la función auth.
supabase.auth.sign_in_with_password({
    "email": "camion_c110b@rutas.com", 
    "password": "password_seguro_aqui"
})

# 2. Bucle infinito del hardware
# Aquí va el código de tu compañero que lee los infrarrojos y la IA
pasajeros_actuales = 45 

# 3. Envío de paquete JSON a tu API
datos = {
    "unidad_id": "AQUÍ-VA-EL-UUID-DE-LA-UNIDAD", 
    "pasajeros_totales": pasajeros_actuales
}

# Inserción directa. Si cambia la sintaxis, es mejor verificar la documentación actual.
respuesta = supabase.table("registros_ocupacion").insert(datos).execute()
