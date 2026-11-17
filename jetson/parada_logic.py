"""
parada_logic.py - Lógica compartida laptop <-> Jetson conectada a Supabase.
"""
import collections
import json
import os
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client


@dataclass
class Det:
    label: str      # "person", "bus", "truck", "car"...
    conf: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def foot(self):
        """Punto de los pies (centro-abajo del bbox): mejor para saber si alguien está en la zona."""
        return ((self.x1 + self.x2) / 2, self.y2)

    @property
    def center(self):
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    @property
    def area(self):
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


def norm_to_px(roi, w, h):
    """ROI normalizado (0-1) -> píxeles. Así el mismo ROI sirve con cualquier resolución."""
    x1, y1, x2, y2 = roi
    return (x1 * w, y1 * h, x2 * w, y2 * h)


def inside(pt, rect):
    x, y = pt
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


class StopState:
    """Estado de la parada con suavizado temporal para evitar parpadeos."""

    def __init__(
        self,
        roi=(0.20, 0.40, 0.80, 1.00),   # zona de espera (x1,y1,x2,y2) normalizada
        bus_roi=None,                   # zona donde cuenta un camión; None = todo el cuadro
        bus_labels=("bus",),            # clases que cuentan como "camión"
        person_conf=0.40,
        bus_conf=0.40,
        min_bus_area=0.02,              # área mínima del camión (fracción del cuadro)
        smooth_frames=15,               # ventana para la mediana del conteo
        bus_on_frames=5,                # frames seguidos para declarar "llegó"
        bus_hold_frames=30,             # frames que tarda en declarar "se fue"
    ):
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
        self.bus_latched = False   # "hubo camión desde el último envío"

    def update(self, dets, w, h):
        """Procesa un frame. Devuelve (personas_en_roi, camiones) para dibujar."""
        roi_px = norm_to_px(self.roi, w, h)
        people = [
            d for d in dets
            if d.label == "person" and d.conf >= self.person_conf and inside(d.foot, roi_px)
        ]
        self.hist.append(len(people))

        bus_roi_px = norm_to_px(self.bus_roi, w, h) if self.bus_roi else None
        buses = [
            d for d in dets
            if d.label in self.bus_labels
            and d.conf >= self.bus_conf
            and d.area >= self.min_bus_area * w * h
            and (bus_roi_px is None or inside(d.center, bus_roi_px))
        ]

        # Histéresis: sube de a 1 por frame con camión, baja de a 1 sin camión
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

    def payload(self, stop_id):
        """Arma el JSON. 'camion_detectado' = True si hubo camión en cualquier momento desde el último envío."""
        data = {
            "id_parada": stop_id,
            "personas_esperando": self.people_smoothed,
            "camion_detectado": bool(self.bus_on or self.bus_latched),
        }
        self.bus_latched = False
        return data


class Reporter:
    """Envía el JSON a Supabase cada `interval` segundos en un hilo aparte (no bloquea el video)."""

    def __init__(self, stop_id, url=None, interval=5.0, timeout=3.0, dry_run=False):
        self.stop_id = stop_id
        self.interval = interval
        self.dry_run = dry_run
        self._last = None
        self.supabase: Client | None = None

        if not self.dry_run:
            load_dotenv(dotenv_path=Path(__file__).parent / ".env")

            sb_url = url or os.getenv("SUPABASE_URL")
            sb_key = os.getenv("SUPABASE_KEY")
            sb_email = os.getenv("SUPABASE_EMAIL")
            sb_password = os.getenv("SUPABASE_PASSWORD")

            if sb_url and sb_key and sb_email and sb_password:
                try:
                    self.supabase = create_client(sb_url, sb_key)
                    self.supabase.auth.sign_in_with_password({
                        "email": sb_email,
                        "password": sb_password,
                    })
                    print("✅ Reporter autenticado con Supabase.", flush=True)
                except Exception as e:
                    print(f"❌ Error al autenticar Supabase (correrá en modo local): {e}", flush=True)
                    self.supabase = None
            else:
                print("⚠️ Faltan variables en .env; solo se imprimirá en consola.", flush=True)

    def maybe_send(self, state, now):
        """`now` en segundos (tiempo de video en la laptop, time.time() en la Jetson)."""
        if self._last is None:
            self._last = now
            return
        if now - self._last < self.interval:
            return
        self._last = now
        payload = state.payload(self.stop_id)
        threading.Thread(target=self._post, args=(payload,), daemon=True).start()

    def _post(self, payload):
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] JSON -> {json.dumps(payload, ensure_ascii=False)}", flush=True)
        if not self.supabase:
            return
        try:
            self.supabase.table("registros_parada").insert(payload).execute()
            print(f"[{stamp}] ✅ Datos insertados en Supabase", flush=True)
        except Exception as e:
            print(f"[{stamp}] ❌ Error Supabase: {e}", flush=True)
