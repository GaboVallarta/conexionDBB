import os
from dotenv import load_dotenv
from supabase import create_client, Client

# Cargar las llaves del archivo .env que ya tienes configurado
load_dotenv()

url: str = os.getenv("SUPABASE_URL")
key: str = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(url, key)

try:
    # 1. AUTENTICACIÓN
    print("1. Iniciando sesión con el usuario del camión...")
    supabase.auth.sign_in_with_password({
        "email": "camion_c110b@rutas.com", 
        "password": "C110-B" # Reemplaza esto con la contraseña real
    })
    print("✅ Autenticación exitosa.\n")

    # 2. PRUEBA DE ENVÍO (Escritura hacia la nube)
    print("2. Enviando paquete de datos de prueba...")
    datos = {
        "unidad_id": "2ddeb88a-8609-4b40-b469-f1c9d9400dde", 
        "pasajeros_totales": 12 # Número ficticio de prueba
    }
    
    # Nota de código: Nunca invento métodos de bibliotecas. Si esta sintaxis arroja 
    # algún error de atributo, es mejor verificar la documentación actual de supabase-py.
    respuesta_insert = supabase.table("registros_ocupacion").insert(datos).execute()
    print("✅ Datos enviados correctamente.")
    print(f"Respuesta del servidor: {respuesta_insert.data}\n")

    # 3. PRUEBA DE RECEPCIÓN (Lectura desde la nube)
    print("3. Solicitando información de la base de datos...")
    # Le pedimos a la DB que nos devuelva el estado de esa unidad en específico
    respuesta_select = supabase.table("unidades").select("estado_actual").eq("id", "2ddeb88a-8609-4b40-b469-f1c9d9400dde").execute()
    print("✅ Datos recibidos correctamente.")
    print(f"El estado actual en la nube para este camión es: {respuesta_select.data}\n")
    
    print("🎉 LA CONEXIÓN BIDIRECCIONAL FUNCIONA PERFECTAMENTE.")

except Exception as e:
    print(f"❌ Ocurrió un error en la conexión: {e}")
