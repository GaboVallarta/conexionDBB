#!/usr/bin/env python3
"""
rasp_parada.py - Raspberry Pi en la PARADA. Cerebro del sistema.

Recibe por HTTP (red local de la parada):
    POST /jetson/frame     detecciones crudas de la camara (Jetson)
    POST /esp/heartbeat    {"unidad_id": "...", "pir": 0/1}  (ESP32 del camion, cada ~5 s)
    POST /esp/tarjeta      {"unidad_id": "...", "uid": "AABBCCDD"}  (tarjeta pasada)
    GET  /estado           depuracion

Hace:
    - StopState (suavizado + histeresis) sobre las detecciones crudas.
    - Conteo de pasajeros por unidad: +1 por tarjeta, -1 por bajada vista en camara.
    - Unica que habla con Supabase (REST con requests).

Variables de entorno (.env): SUPABASE_URL, SUPABASE_KEY, SUPABASE_EMAIL,
    SUPABASE_PASSWORD, PARADA_ID, RASP_TOKEN (token compartido, opcional)

Ejemplo:
    python3 rasp_parada.py --bus-labels bus,truck --exit-roi 0.0,0.2,0.25,1.0
    python3 rasp_parada.py --dry-run      # sin Supabase, solo imprime
"""
import argparse
import collections
import json
import os
import queue
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# --- CONFIRMAR: nombres de columnas de registros_ocupacion ------------------
OCUP = {
    "unidad": "unidad_id",
    "subieron": "ingresaron",
    "bajaron": "salieron",
    "total": "total_abordo",
}


def iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Logica de la parada (antes en parada_logic.py / Jetson)
# ---------------------------------------------------------------------------
@dataclass
class Det:
    label: str
    conf: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def foot(self):
        return ((self.x1 + self.x2) / 2, self.y2)

    @property
    def center(self):
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    @property
    def area(self):
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


def norm_to_px(roi, w, h):
    x1, y1, x2, y2 = roi
    return (x1 * w, y1 * h, x2 * w, y2 * h)


def inside(pt, rect):
    x, y = pt
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


class StopState:
    """Personas esperando (mediana) + camion presente (histeresis). Ajustado a ~5 Hz."""

    def __init__(self, roi, bus_roi=None, bus_labels=("bus",), person_conf=0.40,
                 bus_conf=0.40, min_bus_area=0.02, smooth_frames=7,
                 bus_on_frames=3, bus_hold_frames=15):
        self.roi = roi
        self.bus_roi = bus_roi
        self.bus_labels = set(bus_labels)
        self.person_conf = person_conf
        self.bus_conf = bus_conf
        self.min_bus_area = min_bus_area
        self.hist = collections.deque(maxlen=smooth_frames)
        self.bus_on_frames = bus_on_frames
        self.bus_hold_frames = bus_hold_frames
        self.bus_score = 0
        self.bus_on = False
        self.bus_latched = False

    def update(self, dets, w, h):
        roi_px = norm_to_px(self.roi, w, h)
        people = [d for d in dets if d.label == "person" and d.conf >= self.person_conf
                  and inside(d.foot, roi_px)]
        self.hist.append(len(people))

        bus_roi_px = norm_to_px(self.bus_roi, w, h) if self.bus_roi else None
        buses = [d for d in dets if d.label in self.bus_labels and d.conf >= self.bus_conf
                 and d.area >= self.min_bus_area * w * h
                 and (bus_roi_px is None or inside(d.center, bus_roi_px))]

        if buses:
            self.bus_score = min(self.bus_score + 1, self.bus_hold_frames)
        else:
            self.bus_score = max(self.bus_score - 1, 0)
        if not self.bus_on and self.bus_score >= self.bus_on_frames:
            self.bus_on = True
        elif self.bus_on and self.bus_score == 0:
            self.bus_on = False
        if self.bus_on:
            self.bus_latched = True
        return people, buses

    @property
    def people_smoothed(self):
        return int(round(statistics.median(self.hist))) if self.hist else 0

    @property
    def camion_flag(self):
        return bool(self.bus_on or self.bus_latched)

    def payload(self, stop_id):
        data = {"id_parada": stop_id,
                "personas_esperando": self.people_smoothed,
                "camion_detectado": self.camion_flag}
        self.bus_latched = False
        return data


class ExitTracker:
    """
    Detecta BAJADAS: una persona cuenta como bajada solo si
      - nacio (primera vez vista) dentro de exit_roi mientras HAY camion confirmado, y
      - se mantiene en pantalla al menos `min_hits` lecturas (filtro de falsos positivos).
    Seguimiento por vecino mas cercano de los pies, en coordenadas normalizadas.
    """

    def __init__(self, min_hits=4, max_dist=0.10, max_missed=3):
        self.min_hits = min_hits
        self.max_dist = max_dist
        self.max_missed = max_missed
        self.tracks = {}
        self.next_id = 1

    def update(self, pts, bus_on, exit_roi):
        pares = []
        for tid, t in self.tracks.items():
            for i, p in enumerate(pts):
                d = ((t["x"] - p[0]) ** 2 + (t["y"] - p[1]) ** 2) ** 0.5
                if d <= self.max_dist:
                    pares.append((d, tid, i))
        pares.sort()
        usados_t, usados_p = set(), set()
        nuevos = 0
        for d, tid, i in pares:
            if tid in usados_t or i in usados_p:
                continue
            usados_t.add(tid)
            usados_p.add(i)
            t = self.tracks[tid]
            t["x"], t["y"] = pts[i]
            t["hits"] += 1
            t["missed"] = 0
            if t["born_exit"] and not t["counted"] and t["hits"] >= self.min_hits:
                t["counted"] = True
                nuevos += 1
        for tid in list(self.tracks):
            if tid not in usados_t:
                self.tracks[tid]["missed"] += 1
                if self.tracks[tid]["missed"] > self.max_missed:
                    del self.tracks[tid]
        for i, p in enumerate(pts):
            if i not in usados_p:
                self.tracks[self.next_id] = {
                    "x": p[0], "y": p[1], "hits": 1, "missed": 0, "counted": False,
                    "born_exit": bool(bus_on and inside(p, exit_roi)),
                }
                self.next_id += 1
        return nuevos


# ---------------------------------------------------------------------------
# Supabase por REST
# ---------------------------------------------------------------------------
class Supa:
    def __init__(self):
        self.url = os.environ["SUPABASE_URL"].rstrip("/")
        self.key = os.environ["SUPABASE_KEY"]
        self.email = os.environ["SUPABASE_EMAIL"]
        self.password = os.environ["SUPABASE_PASSWORD"]
        self.token = None
        self.s = requests.Session()

    def login(self):
        r = self.s.post(self.url + "/auth/v1/token?grant_type=password",
                        headers={"apikey": self.key, "Content-Type": "application/json"},
                        json={"email": self.email, "password": self.password}, timeout=10)
        r.raise_for_status()
        self.token = r.json()["access_token"]
        print("[SUPABASE] sesion iniciada", flush=True)

    def _req(self, method, tabla, params=None, data=None):
        if self.token is None:
            self.login()
        for _ in range(2):
            headers = {"apikey": self.key, "Authorization": "Bearer " + self.token,
                       "Content-Type": "application/json", "Prefer": "return=minimal"}
            r = self.s.request(method, self.url + "/rest/v1/" + tabla, headers=headers,
                               params=params, json=data, timeout=10)
            if r.status_code == 401:
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


class DryDB:
    """Modo --dry-run: no toca Supabase."""

    def select(self, *a, **k):
        return []

    def insert(self, tabla, fila):
        print("[DRY] INSERT", tabla, fila, flush=True)

    def update(self, tabla, params, datos):
        print("[DRY] UPDATE", tabla, params, datos, flush=True)


def ejecutar(db, parada_id, job):
    t = job[0]
    if t == "parada":
        db.insert("registros_parada", job[1])
    elif t == "ubicacion":
        _, unidad, ts = job
        db.insert("registros_ubicacion", {"unidad_id": unidad, "parada_id": parada_id, "fecha_hora": ts})
        db.update("unidades", {"id": "eq." + unidad}, {"ultima_parada_id": parada_id})
    elif t == "ocupacion":
        _, unidad, sub, baj, total = job
        db.insert("registros_ocupacion", {OCUP["unidad"]: unidad, OCUP["subieron"]: sub,
                                          OCUP["bajaron"]: baj, OCUP["total"]: total})
    elif t == "pasajeros":
        _, unidad, total = job
        db.update("unidades", {"id": "eq." + unidad}, {"pasajeros_actuales": total})


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
                print("[SUPABASE] error (%d/3) %s: %s" % (intento + 1, job[0], e), flush=True)
                time.sleep(2)


# ---------------------------------------------------------------------------
# Flota: unidades presentes y su cuenta de pasajeros
# ---------------------------------------------------------------------------
class Flota:
    def __init__(self, cola, db, ttl, rearme, terminal):
        self.q = cola
        self.db = db
        self.ttl = ttl
        self.rearme = rearme
        self.terminal = terminal
        self.units = {}

    def _get(self, uid):
        u = self.units.get(uid)
        if u is None:
            total = 0
            try:
                rows = self.db.select("unidades", {"id": "eq." + uid,
                                                   "select": "pasajeros_actuales", "limit": 1})
                if rows and rows[0].get("pasajeros_actuales") is not None:
                    total = int(rows[0]["pasajeros_actuales"])
            except Exception as e:
                print("[FLOTA] no pude leer pasajeros_actuales de %s: %s" % (uid, e), flush=True)
            u = {"total": total, "last_seen": 0.0, "present": False, "cards": {}, "pir": 0}
            self.units[uid] = u
        return u

    def _seen(self, uid, now):
        u = self._get(uid)
        if not u["present"] or now - u["last_seen"] > self.ttl:
            u["present"] = True
            print("[FLOTA] unidad %s LLEGO a la parada" % uid, flush=True)
            self.q.put(("ubicacion", uid, iso()))
            if self.terminal and u["total"] != 0:
                bajan = u["total"]
                u["total"] = 0
                print("[FLOTA] terminal: reinicio a 0 (%d)" % bajan, flush=True)
                self.q.put(("ocupacion", uid, 0, bajan, 0))
                self.q.put(("pasajeros", uid, 0))
        u["last_seen"] = now
        return u

    def heartbeat(self, uid, pir, now):
        u = self._seen(uid, now)
        u["pir"] = int(bool(pir))

    def tarjeta(self, uid, card, now):
        u = self._seen(uid, now)
        ultimo = u["cards"].get(card)
        if ultimo is not None and now - ultimo < self.rearme:
            print("[FLOTA] tarjeta %s repetida, ignorada" % card, flush=True)
            return u["total"], True
        u["cards"][card] = now
        u["total"] += 1
        print("[FLOTA] +1 unidad %s (tarjeta %s) -> %d" % (uid, card, u["total"]), flush=True)
        self.q.put(("ocupacion", uid, 1, 0, u["total"]))
        self.q.put(("pasajeros", uid, u["total"]))
        return u["total"], False

    def presente(self, now):
        c = [(u["last_seen"], uid) for uid, u in self.units.items()
             if u["present"] and now - u["last_seen"] <= self.ttl]
        if not c:
            return None
        if len(c) > 1:
            print("[FLOTA] aviso: varias unidades presentes, se usa la mas reciente", flush=True)
        return max(c)[1]

    def bajada(self, now):
        uid = self.presente(now)
        if uid is None:
            print("[FLOTA] bajada vista pero ninguna unidad presente: ignorada", flush=True)
            return
        u = self.units[uid]
        if u["total"] <= 0:
            print("[FLOTA] bajada ignorada: unidad %s ya esta en 0" % uid, flush=True)
            return
        u["total"] -= 1
        print("[FLOTA] -1 unidad %s (camara) -> %d" % (uid, u["total"]), flush=True)
        self.q.put(("ocupacion", uid, 0, 1, u["total"]))
        self.q.put(("pasajeros", uid, u["total"]))

    def tick(self, now):
        for uid, u in self.units.items():
            if u["present"] and now - u["last_seen"] > self.ttl:
                u["present"] = False
                print("[FLOTA] unidad %s SE FUE (sin latido)" % uid, flush=True)
            u["cards"] = {c: t for c, t in u["cards"].items() if now - t < 3600}


# ---------------------------------------------------------------------------
# App + servidor HTTP
# ---------------------------------------------------------------------------
class App:
    pass


APP = App()


def procesar_frame(data, now):
    w, h = float(data["w"]), float(data["h"])
    dets = [Det(d[0], float(d[1]), float(d[2]), float(d[3]), float(d[4]), float(d[5]))
            for d in data["dets"]]
    with APP.lock:
        APP.last_frame = now
        APP.state.update(dets, w, h)
        pts = []
        for d in dets:
            if d.label == "person" and d.conf >= APP.state.person_conf:
                fx, fy = d.foot
                pts.append((fx / w, fy / h))
        nuevos = APP.tracker.update(pts, APP.state.bus_on, APP.exit_roi)
        for _ in range(nuevos):
            APP.flota.bajada(now)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/estado":
            return self._reply(404, {"ok": False})
        with APP.lock:
            self._reply(200, {
                "personas_esperando": APP.state.people_smoothed,
                "camion_en_camara": APP.state.bus_on,
                "unidades": {uid: {"total": u["total"], "presente": u["present"]}
                             for uid, u in APP.flota.units.items()},
                "ultimo_frame_hace_s": round(time.time() - APP.last_frame, 1) if APP.last_frame else None,
            })

    def do_POST(self):
        if APP.token and self.headers.get("X-Token") != APP.token:
            return self._reply(401, {"ok": False, "error": "token"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._reply(400, {"ok": False, "error": "json"})
        now = time.time()
        try:
            if self.path == "/jetson/frame":
                procesar_frame(data, now)
                return self._reply(200, {"ok": True})
            if self.path == "/esp/heartbeat":
                with APP.lock:
                    APP.flota.heartbeat(data["unidad_id"], data.get("pir", 0), now)
                return self._reply(200, {"ok": True})
            if self.path == "/esp/tarjeta":
                with APP.lock:
                    total, dup = APP.flota.tarjeta(data["unidad_id"], data["uid"], now)
                return self._reply(200, {"ok": True, "total": total, "duplicada": dup})
        except (KeyError, ValueError, IndexError, TypeError) as e:
            return self._reply(400, {"ok": False, "error": str(e)})
        self._reply(404, {"ok": False})


def tick_loop(args, stop):
    """Cada segundo: expira unidades. Cada --intervalo: sube registros_parada si cambio o toca latido."""
    ultimo_check = 0.0
    ultima_clave = None
    ultima_subida = 0.0
    while not stop.wait(1.0):
        now = time.time()
        with APP.lock:
            APP.flota.tick(now)
        if now - ultimo_check < args.intervalo:
            continue
        ultimo_check = now
        if not APP.last_frame or now - APP.last_frame > 15:
            continue        # la Jetson no manda datos: no subir cifras viejas
        with APP.lock:
            clave = (APP.state.people_smoothed, APP.state.camion_flag)
            if clave != ultima_clave or now - ultima_subida >= args.latido:
                APP.cola.put(("parada", APP.state.payload(args.parada_id)))
                ultima_clave = clave
                ultima_subida = now


def parse_roi(txt):
    return tuple(float(v) for v in txt.split(","))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parada-id", default=os.environ.get("PARADA_ID"))
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--token", default=os.environ.get("RASP_TOKEN"))
    ap.add_argument("--dry-run", action="store_true", help="no escribe en Supabase")
    ap.add_argument("--terminal", action="store_true", help="esta parada es terminal: reinicia la cuenta a 0 al llegar")
    # zonas y deteccion
    ap.add_argument("--roi", default="0.20,0.40,0.80,1.00", help="zona de espera (0-1)")
    ap.add_argument("--bus-roi", default=None)
    ap.add_argument("--exit-roi", default="0.0,0.2,0.25,1.0", help="zona de la puerta del camion (CALIBRAR)")
    ap.add_argument("--conf", type=float, default=0.40)
    ap.add_argument("--bus-labels", default="bus")
    ap.add_argument("--min-area", type=float, default=0.02)
    ap.add_argument("--min-hits", type=int, default=4, help="lecturas minimas para validar una bajada")
    ap.add_argument("--smooth", type=int, default=7)
    ap.add_argument("--bus-on", type=int, default=3)
    ap.add_argument("--bus-hold", type=int, default=15)
    # tiempos
    ap.add_argument("--intervalo", type=float, default=5.0)
    ap.add_argument("--latido", type=float, default=60.0)
    ap.add_argument("--ttl", type=float, default=20.0, help="s sin latido para dar por ida una unidad")
    ap.add_argument("--rearme", type=float, default=5.0, help="s para ignorar la misma tarjeta")
    args = ap.parse_args()
    if not args.parada_id:
        raise SystemExit("Falta --parada-id (o PARADA_ID en .env)")

    db = DryDB() if args.dry_run else Supa()
    stop = threading.Event()

    APP.lock = threading.RLock()
    APP.token = args.token
    APP.cola = queue.Queue(maxsize=500)
    APP.last_frame = 0.0
    APP.exit_roi = parse_roi(args.exit_roi)
    APP.state = StopState(
        roi=parse_roi(args.roi),
        bus_roi=parse_roi(args.bus_roi) if args.bus_roi else None,
        bus_labels=[s.strip() for s in args.bus_labels.split(",")],
        person_conf=args.conf, bus_conf=args.conf, min_bus_area=args.min_area,
        smooth_frames=args.smooth, bus_on_frames=args.bus_on, bus_hold_frames=args.bus_hold)
    APP.tracker = ExitTracker(min_hits=args.min_hits)
    APP.flota = Flota(APP.cola, db, args.ttl, args.rearme, args.terminal)

    for target, targs in ((sender_loop, (db, args.parada_id, APP.cola, stop)),
                          (tick_loop, (args, stop))):
        t = threading.Thread(target=target, args=targs)
        t.daemon = True
        t.start()

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print("[RASP] escuchando en :%d (dry-run=%s, terminal=%s)" % (args.port, args.dry_run, args.terminal), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        t0 = time.time()
        while not APP.cola.empty() and time.time() - t0 < 20:
            time.sleep(0.5)
        time.sleep(3)
        stop.set()


if __name__ == "__main__":
    main()
