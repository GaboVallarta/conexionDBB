import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

url: str = os.getenv("SUPABASE_URL")
key: str = os.getenv("SUPABASE_KEY")
email: str = os.getenv("SUPABASE_EMAIL")
password: str = os.getenv("SUPABASE_PASSWORD")

supabase: Client = create_client(url, key)

try:
    print("1. Iniciando sesión...")
    supabase.auth.sign_in_with_password({
        "email": email,
        "password": password
    })
    print("✅ Autenticación exitosa.\n")

    print("2. Enviando paquete de datos de prueba...")
    datos = {
        "id_parada": "94e70cfe-4f65-4657-b8a9-f10edd5e3036",       # Ajustar si él usa otro nombre de columna o un UUID
        "personas_esperando": 3,
        "camion_detectado": False
    }
    
    respuesta_insert = supabase.table("registros_parada").insert(datos).execute()
    print("✅ Datos enviados correctamente.")
    print(f"Respuesta del servidor: {respuesta_insert.data}\n")

except Exception as e:
    print(f"❌ Ocurrió un error en la conexión: {e}")
