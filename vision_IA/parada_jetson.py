#!/usr/bin/env python3
"""
parada_jetson.py  -  Jetson Nano instalada EN LA PARADA
Webcam (personas esperando + camión) + RFID RC522 (identifica el camión) + PIR HC-SR501

Ubicación sugerida: jetson-nano/parada_jetson.py (junto a una copia de parada_logic.py)
Compatible con Python 3.6 (JetPack 4.x): habla con Supabase por REST (requests).

Variables de entorno (.env): SUPABASE_URL, SUPABASE_KEY, SUPABASE_EMAIL, SUPABASE_PASSWORD
                             PARADA_ID (uuid de paradas.id donde está instalada la Jetson)

Flujo:
  - Cada --intervalo s  -> INSERT en registros_parada
        {id_parada, personas_esperando, camion_detectado}      (payload de StopState)
  - Un camión pasa su tarjeta por el RC522 ->
        INSERT en registros_ubicacion {unidad_id, parada_id, fecha_hora}
        UPDATE unidades.ultima_parada_id
        y se fuerza camion_detectado=True en el siguiente envío
  - El PIR acelera el envío cuando hay movimiento reciente en la parada.

Ejemplo:
    python3 parada_jetson.py /dev/video0 --pir-pin 7 --headless
"""
import argparse
import os
import queue
import sys
import threading
import time

import requests
from datetime import datetime, timezone
from jetson_inference import detectNet
from jetson_utils import videoSource, videoOutput

# ---------------------------------------------------------------------------
# (A) IMPORT DE parada_logic: Det y StopState (conteo suavizado + histéresis de camión)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parada_logic import Det, StopState  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


def ahora_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Cliente mínimo de Supabase por REST (reemplaza a supabase-py en Python 3.6)
# ---------------------------------------------------------------------------
class Supa(object):
    def __init__(self):
        self.url = os.environ["SUPABASE_URL"].rstrip("/")
        self.key = os.environ["SUPABASE_KEY"]
        self.email = os.environ["SUPABASE_EMAIL"]
        self.password = os.environ["SUPABASE_PASSWORD"]
        self.token = None
        self.s = requests.Session()

    def login(self):
        r = self.s.post(
            self.url + "/auth/v1/token?grant_type=password",
            headers={"apikey": self.key, "Content-Type": "application/json"},
            json={"email": self.email, "password": self.password},
            timeout=10,
        )
        r.raise_for_status()
        self.token = r.json()["access_token"]
        print("[SUPABASE] sesión iniciada", flush=True)

    def _req(self, method, tabla, params=None, data=None):
        if self.token is None:
            self.login()
        for _ in range(2):
            headers = {
                "apikey": self.key,
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }
            r = self.s.request(method, self.url + "/rest/v1/" + tabla,
                               headers=headers, params=params, json=data, timeout=10)
            if r.status_code == 401:      # token vencido: re-login y reintenta
                self.login()
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()

    def select(self, tabla, params):
        return self._req("GET", tabla, params=params).json()

    def insert(self, tabla, fila):
        self._req("POST", tabla, data=fila)

    def update(self, tabla, params, datos):
        self._req("PATCH", tabla, params=params, data=datos)


# ---------------------------------------------------------------------------
# Sensores (cada uno en su hilo)
# ---------------------------------------------------------------------------
class Mov(object):
    """Último instante en que el PIR detectó movimiento."""
    def __init__(self):
        self.ts = 0.0


def pir_loop(mov, pin, stop):
    try:
        import Jetson.GPIO as GPIO
    except ImportError:
        print("[PIR] Jetson.GPIO no instalado, deshabilitado")
        return
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(pin, GPIO.IN)
    try:
        while not stop.is_set():
            if GPIO.input(pin):
                mov.ts = time.time()
            time.sleep(0.1)
    finally:
        GPIO.cleanup(pin)


def rfid_loop(db, rfid_q, rearme, stop):
    """Lee la tarjeta del camión con el RC522, resuelve uid -> unidad_id y lo encola."""
    try:
        from mfrc522 import MFRC522
    except ImportError:
        print("[RFID] librería mfrc522 no instalada, deshabilitado")
        return
    reader = MFRC522()
    cache = {}
    ultimo_uid, ultimo_ts = None, 0.0
    while not stop.is_set():
        try:
            status, _ = reader.MFRC522_Request(reader.PICC_REQIDL)
            if status == reader.MI_OK:
                status, raw = reader.MFRC522_Anticoll()
                if status == reader.MI_OK:
                    uid = "".join("%02X" % b for b in raw[:4])
                    ahora = time.time()
                    if uid == ultimo_uid and ahora - ultimo_ts < rearme:
                        continue
                    ultimo_uid, ultimo_ts = uid, ahora
                    unidad_id = cache.get(uid)
                    if unidad_id is None:
                        filas = db.select("unidades", {"rfid_uid": "eq." + uid,
                                                       "select": "id", "limit": 1})
                        if not filas:
                            print("[RFID] tarjeta sin unidad:", uid, flush=True)
                            continue
                        unidad_id = cache[uid] = filas[0]["id"]
                    print("[RFID] unidad", unidad_id, "(uid %s)" % uid, flush=True)
                    rfid_q.put(unidad_id)
        except Exception as e:
            print("[RFID] error:", e, flush=True)
            time.sleep(1)
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# (B) ENVÍO A SUPABASE (hilo aparte para no bajar los FPS)
# ---------------------------------------------------------------------------
def ejecutar(db, parada_id, job):
    tipo = job[0]
    if tipo == "parada":                       # lo que antes hacía Reporter._post
        db.insert("registros_parada", job[1])
    elif tipo == "ubicacion":
        _, unidad_id, ts = job
        db.insert("registros_ubicacion",
                  {"unidad_id": unidad_id, "parada_id": parada_id, "fecha_hora": ts})
        db.update("unidades", {"id": "eq." + unidad_id}, {"ultima_parada_id": parada_id})


def sender_loop(db, parada_id, cola, stop):
    while not stop.is_set():
        try:
            job = cola.get(timeout=1)
        except queue.Empty:
            continue
        for intento in range(3):
            try:
                ejecutar(db, parada_id, job)
                print("[SUPABASE] OK", job[0], flush=True)
                break
            except Exception as e:
                print("[SUPABASE] error (%d/3): %s" % (intento + 1, e), flush=True)
                time.sleep(2)


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def parse_roi(txt):
    return tuple(float(v) for v in txt.split(","))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?", default="/dev/video0")   # webcam USB
    ap.add_argument("--parada-id", default=os.environ.get("PARADA_ID"))
    ap.add_argument("--network", default="ssd-mobilenet-v2")
    ap.add_argument("--roi", default="0.20,0.40,0.80,1.00", help="zona de espera x1,y1,x2,y2 (0-1)")
    ap.add_argument("--bus-roi", default=None, help="zona donde cuenta un camión; vacío = todo")
    ap.add_argument("--conf", type=float, default=0.40)
    ap.add_argument("--pir-pin", type=int, default=None, help="pin BOARD del HC-SR501")
    ap.add_argument("--intervalo", type=float, default=5.0, help="s entre envíos (sin movimiento: x3)")
    ap.add_argument("--latido", type=float, default=60.0, help="s max sin enviar aunque no haya cambios")
    ap.add_argument("--rearme", type=float, default=60.0, help="s para ignorar la misma tarjeta")
    ap.add_argument("--headless", action="store_true", help="sin ventana de video")
    args = ap.parse_args()
    if not args.parada_id:
        raise SystemExit("Falta --parada-id (o PARADA_ID en .env)")

    db = Supa()
    stop = threading.Event()
    cola = queue.Queue(maxsize=100)
    rfid_q = queue.Queue()
    mov = Mov()

    hilos = [
        threading.Thread(target=sender_loop, args=(db, args.parada_id, cola, stop)),
        threading.Thread(target=rfid_loop, args=(db, rfid_q, args.rearme, stop)),
    ]
    if args.pir_pin is not None:
        hilos.append(threading.Thread(target=pir_loop, args=(mov, args.pir_pin, stop)))
    for h in hilos:
        h.daemon = True
        h.start()

    net = detectNet(args.network, threshold=args.conf)
    cam = videoSource(args.input)
    out = None if args.headless else videoOutput("display://0")
    overlay = "none" if args.headless else "box,labels,conf"

    estado = StopState(
        roi=parse_roi(args.roi),
        bus_roi=parse_roi(args.bus_roi) if args.bus_roi else None,
        person_conf=args.conf,
        bus_conf=args.conf,
    )
    ultimo_envio = time.time()
    ultimo_clave = None
    ultimo_subida = 0.0

    try:
        while True:
            img = cam.Capture()
            if img is None:
                if not cam.IsStreaming():
                    break
                continue

            dets = net.Detect(img, overlay=overlay)
            estado.update(
                [Det(net.GetClassDesc(d.ClassID), d.Confidence,
                     d.Left, d.Top, d.Right, d.Bottom) for d in dets],
                img.width, img.height,
            )
            ahora = time.time()

            # --- RFID: un camión identificado llegó a ESTA parada ---
            try:
                unidad_id = rfid_q.get_nowait()
            except queue.Empty:
                unidad_id = None
            if unidad_id:
                cola.put(("ubicacion", unidad_id, ahora_iso()))
                estado.bus_latched = True      # el RFID confirma camión aunque la cámara no lo vea

            # --- Envío periódico a registros_parada ---
            # Con movimiento reciente (PIR) se envía cada --intervalo; sin él, 3 veces más lento.
            hay_mov = args.pir_pin is None or (ahora - mov.ts) < 30
            intervalo = args.intervalo if hay_mov else args.intervalo * 3
            if ahora - ultimo_envio >= intervalo:
                ultimo_envio = ahora
                datos = estado.payload(args.parada_id)
                clave = (datos["personas_esperando"], datos["camion_detectado"])
                if clave != ultimo_clave or ahora - ultimo_subida >= args.latido:
                    cola.put(("parada", datos))
                    ultimo_clave = clave
                    ultimo_subida = ahora

            if out is not None:
                out.Render(img)
                out.SetStatus("Esperando: %d | Camion: %s | PIR: %s | %.0f FPS" % (
                    estado.people_smoothed, "SI" if estado.bus_on else "no",
                    "si" if ahora - mov.ts < 2 else "no", net.GetNetworkFPS()))
                if not out.IsStreaming():
                    break
    finally:
        t0 = time.time()
        while not cola.empty() and time.time() - t0 < 20:
            time.sleep(0.5)
        time.sleep(4)
        stop.set()


if __name__ == "__main__":
    main()
