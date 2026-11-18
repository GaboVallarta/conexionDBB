#!/usr/bin/env python3
"""
parada_jetson.py - Jetson Nano en la PARADA. SOLO CAMARA.

Detecta objetos con detectNet y manda las detecciones CRUDAS a la Raspberry
por HTTP (POST /jetson/frame). No cuenta, no suaviza, no habla con Supabase.
Compatible con Python 3.6 (JetPack 4.x): solo usa `requests`.
"""
import argparse
import os
import queue
import threading
import time

import requests
from jetson_inference import detectNet
from jetson_utils import videoSource, videoOutput

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


def sender_loop(url, token, cola, stop):
    """Hilo aparte: manda frames a la Raspberry. Un frame viejo no sirve, asi que NO se reintenta."""
    s = requests.Session()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Token"] = token
    ultimo_error = 0.0
    while not stop.is_set():
        try:
            payload = cola.get(timeout=1)
        except queue.Empty:
            continue
        try:
            r = s.post(url + "/jetson/frame", json=payload, headers=headers, timeout=2)
            r.raise_for_status()
        except Exception as e:
            if time.time() - ultimo_error > 5:      # no inundar la consola
                print("[RASP] error enviando frame: %s" % e, flush=True)
                ultimo_error = time.time()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?", default="/dev/video0")
    ap.add_argument("--raspberry", default=os.environ.get("RASP_URL"))
    ap.add_argument("--token", default=os.environ.get("RASP_TOKEN"))
    ap.add_argument("--network", default="ssd-mobilenet-v2")
    ap.add_argument("--conf", type=float, default=0.40)
    ap.add_argument("--hz", type=float, default=5.0, help="frames por segundo que se mandan a la Raspberry")
    ap.add_argument("--headless", action="store_true", help="sin ventana de video")
    args = ap.parse_args()
    if not args.raspberry:
        raise SystemExit("Falta --raspberry (o RASP_URL), ej. http://192.168.1.50:8000")
    url = args.raspberry.rstrip("/")

    stop = threading.Event()
    cola = queue.Queue(maxsize=2)
    hilo = threading.Thread(target=sender_loop, args=(url, args.token, cola, stop))
    hilo.daemon = True
    hilo.start()

    net = detectNet(args.network, threshold=args.conf)
    cam = videoSource(args.input)
    out = None if args.headless else videoOutput("display://0")
    overlay = "none" if args.headless else "box,labels,conf"

    periodo = 1.0 / max(args.hz, 0.1)
    ultimo = 0.0

    try:
        while True:
            img = cam.Capture()
            if img is None:
                if not cam.IsStreaming():
                    break
                continue

            dets = net.Detect(img, overlay=overlay)
            ahora = time.time()

            if ahora - ultimo >= periodo:
                ultimo = ahora
                payload = {
                    "w": img.width,
                    "h": img.height,
                    "dets": [
                        [net.GetClassDesc(d.ClassID), round(d.Confidence, 3),
                         round(d.Left, 1), round(d.Top, 1), round(d.Right, 1), round(d.Bottom, 1)]
                        for d in dets
                    ],
                }
                try:
                    cola.put_nowait(payload)
                except queue.Full:                  # descarta el mas viejo, deja el nuevo
                    try:
                        cola.get_nowait()
                    except queue.Empty:
                        pass
                    cola.put_nowait(payload)

            if out is not None:
                out.Render(img)
                out.SetStatus("Detecciones: %d | %.0f FPS" % (len(dets), net.GetNetworkFPS()))
                if not out.IsStreaming():
                    break
    finally:
        stop.set()


if __name__ == "__main__":
    main()
