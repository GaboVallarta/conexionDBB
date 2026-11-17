# Módulo de Visión Artificial (`vision_IA`)

Sistema de detección en tiempo real mediante YOLOv8 para monitorear una parada de transporte público y enviar los datos de ocupación y llegada de unidades a **Supabase**.

---

## 📁 Estructura de archivos

* **`parada_logic.py`**: Lógica principal de conteo y comunicación con Supabase.
  * Filtra a las personas cuyo punto inferior (`foot`) cae dentro de la zona de espera (**ROI**).
  * Suaviza el conteo con una mediana móvil para evitar parpadeos en la detección.
  * Aplica histéresis para confirmar la llegada/salida de camiones (`bus`) sin falsos positivos.
  * Envía los registros cada *N* segundos en un hilo separado (`Reporter`) mediante `.insert()` hacia la tabla `registros_parada`.
* **`sim_local.py`**: Simulador local que procesa un video de prueba (`.mp4`) usando OpenCV y YOLOv8, dibuja las detecciones en pantalla y ejecuta `parada_logic.py`.
* **`test_supabase.py`**: Script de prueba rápida para verificar la autenticación y la inserción de un paquete de datos de ejemplo en Supabase sin necesidad de correr el modelo de visión.
* **`.gitignore`**: Excluye credenciales (`.env`), caché de Python, videos de prueba (`.mp4`) y pesos de modelos (`.pt`).

---

## ⚙️ Requisitos e instalación

1. Tener **Python 3.10+** instalado.
2. Instalar las dependencias necesarias:
   ```bash
   pip install ultralytics opencv-python supabase python-dotenv
