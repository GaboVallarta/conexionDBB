#!/usr/bin/env python3
"""
sim_local.py - Simula en tu laptop lo que hará la Jetson.

Instalación:
    pip install ultralytics opencv-python requests

Ejemplos:
    # 1) Definir el ROI con el mouse en el primer frame (imprime el valor para copiar)
    python sim_local.py --video camara.mp4 --select-roi

    # 2) Correr con ROI fijo, solo imprimiendo el JSON
    python sim_local.py --video camara.mp4 --roi 0.15,0.45,0.85,1.0

    # 3) Mandando a tu backend
    python sim_local.py --video camara.mp4 --url http://localhost:8000/api/parada --interval 5

    # Maqueta: contar también 'truck' y 'car' como camión
    python sim_local.py --video camara.mp4 --bus-classes bus,truck,car
"""
import argparse
import sys

import cv2
from ultralytics import YOLO

from parada_logic import Det, Reporter, StopState, norm_to_px

# IDs de COCO que le pedimos a YOLO (mismos nombres de clase que SSD-Mobilenet en jetson-inference)
COCO_IDS = {"person": 0, "car": 2, "bus": 5, "truck": 7}


def parse_roi(text):
    vals = [float(v) for v in text.split(",")]
    if len(vals) != 4:
        raise argparse.ArgumentTypeError("El ROI son 4 números: x1,y1,x2,y2 (entre 0 y 1)")
    return tuple(vals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--model", default="yolov8n.pt")
    ap.add_argument("--roi", type=parse_roi, default=(0.20, 0.40, 0.80, 1.00))
    ap.add_argument("--bus-roi", type=parse_roi, default=None)
    ap.add_argument("--select-roi", action="store_true", help="Dibuja el ROI con el mouse")
    ap.add_argument("--bus-classes", default="bus", help="Ej: bus,truck,car")
    ap.add_argument("--conf", type=float, default=0.40)
    ap.add_argument("--min-bus-area", type=float, default=0.02)
    ap.add_argument("--stop-id", default="TEC_GDL_C110")
    ap.add_argument("--url", default=None, help="Endpoint POST; si se omite solo imprime")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--loop", action="store_true", help="Repetir el video al terminar")
    ap.add_argument("--no-show", action="store_true", help="Sin ventana (solo consola)")
    args = ap.parse_args()

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        sys.exit(f"No pude abrir {args.video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    roi = args.roi
    if args.select_roi:
        ok, first = cap.read()
        if not ok:
            sys.exit("No pude leer el primer frame")
        h, w = first.shape[:2]
        x, y, rw, rh = cv2.selectROI("Zona de espera (ENTER para aceptar)", first, False)
        cv2.destroyAllWindows()
        roi = (x / w, y / h, (x + rw) / w, (y + rh) / h)
        print(f"\nROI normalizado -> --roi {roi[0]:.3f},{roi[1]:.3f},{roi[2]:.3f},{roi[3]:.3f}\n")
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    bus_labels = tuple(s.strip() for s in args.bus_classes.split(","))
    wanted = ["person"] + [b for b in bus_labels if b in COCO_IDS]
    class_ids = sorted({COCO_IDS[c] for c in wanted})

    model = YOLO(args.model)
    state = StopState(
        roi=roi, bus_roi=args.bus_roi, bus_labels=bus_labels,
        person_conf=args.conf, bus_conf=args.conf, min_bus_area=args.min_bus_area,
    )
    reporter = Reporter(args.stop_id, url=args.url, interval=args.interval)

    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            if args.loop:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            break
        h, w = frame.shape[:2]

        res = model.predict(frame, conf=min(args.conf, 0.25), classes=class_ids, verbose=False)[0]
        dets = []
        for b in res.boxes:
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            dets.append(Det(res.names[int(b.cls[0])], float(b.conf[0]), x1, y1, x2, y2))

        people, buses = state.update(dets, w, h)

        # Reloj = tiempo del video, así la simulación es determinista aunque la laptop sea más rápida
        reporter.maybe_send(state, frame_idx / fps)
        frame_idx += 1

        if not args.no_show:
            rx1, ry1, rx2, ry2 = map(int, norm_to_px(state.roi, w, h))
            cv2.rectangle(frame, (rx1, ry1), (rx2, ry2), (0, 255, 0), 2)
            for d in dets:
                if d in people:
                    color = (0, 255, 0)
                elif d in buses:
                    color = (0, 0, 255)
                else:
                    color = (150, 150, 150)
                cv2.rectangle(frame, (int(d.x1), int(d.y1)), (int(d.x2), int(d.y2)), color, 2)
                cv2.putText(frame, f"{d.label} {d.conf:.2f}", (int(d.x1), int(d.y1) - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
            txt = f"En zona: {len(people)} (suavizado {state.people_smoothed}) | Camion: {state.bus_on}"
            cv2.putText(frame, txt, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            cv2.imshow("Simulador parada", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
