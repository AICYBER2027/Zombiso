"""Facecam detection: find the streamer's camera in the picture from where a face keeps appearing.

Works for boxed webcams and for cut-out (green-screen) cameras: the face is found with OpenCV's built-in face
detector on a few frames, the position that keeps coming back is taken as the streamer (game characters'
faces come and go), and the camera area is the face box grown to head-and-shoulders, kept inside the frame.

OpenCV is optional (``pip install opencv-python-headless``). Without it, detection returns None and a facecam
can still be given by hand (``--facecam x,y,w,h``).
"""

from __future__ import annotations

import statistics

from . import media


def _frames(path, in_s: float, dur: float, crop: dict | None, width: int, count: int):
    import numpy as np

    vf = (f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}," if crop else "")
    src_w, src_h = (crop["w"], crop["h"]) if crop else (None, None)
    if src_w is None:
        info = media.probe(path)
        src_w, src_h = info["width"], info["height"]
    h = max(2, int(round(width * src_h / src_w / 2)) * 2)
    fps = max(count / max(dur, 1.0), 0.05)
    raw = media.run_bytes(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{in_s:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
                           "-an", "-vf", f"{vf}fps={fps:.4f},scale={width}:{h},format=gray", "-f", "rawvideo", "-"])
    n = width * h
    return [np.frombuffer(raw[i:i + n], np.uint8).reshape(h, width) for i in range(0, len(raw) - n + 1, n)], width, h


def detect_facecam(path, in_s: float, out_s: float, *, crop: dict | None = None, samples: int = 16) -> dict | None:
    """Camera area as fractions of the (cropped) frame: {x, y, w, h, mode: 'auto', confidence, face}, or None."""
    try:
        import cv2
    except ImportError:
        return None
    frames, fw, fh = _frames(path, in_s, out_s - in_s, crop, 640, samples)
    if not frames:
        return None
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    boxes = []
    for img in frames:
        faces = cascade.detectMultiScale(img, scaleFactor=1.1, minNeighbors=6, minSize=(int(fw * 0.05), int(fw * 0.05)))
        if len(faces):
            boxes.append(max(faces, key=lambda b: b[2] * b[3]))
    if len(boxes) < max(3, 0.3 * len(frames)):
        return None
    mx = statistics.median(b[0] + b[2] / 2 for b in boxes)
    my = statistics.median(b[1] + b[3] / 2 for b in boxes)
    near = [b for b in boxes if abs(b[0] + b[2] / 2 - mx) < 0.12 * fw and abs(b[1] + b[3] / 2 - my) < 0.15 * fh]
    if len(near) < max(3, 0.6 * len(boxes)):
        return None                              # faces all over the place: game characters, not a webcam
    size = statistics.median(b[2] for b in near)
    cw, ch = size * 2.7, size * 2.7              # head and shoulders
    x0 = max(0.0, mx - cw / 2)
    y0 = max(0.0, my - size * 0.95)
    x1, y1 = min(fw, x0 + cw), min(fh, y0 + ch)
    if y1 - y0 < ch * 0.9:                       # camera sits on the bottom edge: grow upwards instead
        y0 = max(0.0, y1 - ch)
    r = lambda v: round(float(v), 3)
    return {"x": r(x0 / fw), "y": r(y0 / fh), "w": r((x1 - x0) / fw), "h": r((y1 - y0) / fh),
            "mode": "auto", "confidence": round(len(near) / len(frames), 2),
            "face": {"x": r((mx - size / 2) / fw), "y": r((my - size / 2) / fh), "size": r(size / fw)}}


def active_area(path, in_s: float, dur: float, *, samples: int = 10) -> dict | None:
    """The picture without black bars (letterbox / pillarbox), or None when there are none.

    A row or column counts as bar when it is dark in almost every sampled frame, so a small logo or
    "subscribe" button sitting inside a bar does not stop the bar from being found."""
    import numpy as np

    info = media.probe(path)
    w, h = info["width"], info["height"]
    fps = max(samples / max(dur, 1.0), 0.05)
    raw = media.run_bytes(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{in_s:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
                           "-an", "-vf", f"fps={fps:.4f},format=gray", "-f", "rawvideo", "-"])
    n = w * h
    if len(raw) < n:
        return None
    frames = np.frombuffer(raw[: len(raw) // n * n], np.uint8).reshape(-1, h, w)
    dark = frames < 24
    col_dark = dark.mean(axis=(0, 1)) > 0.85
    row_dark = dark.mean(axis=(0, 2)) > 0.85

    def span(flags):
        idx = np.flatnonzero(~flags)
        return (int(idx[0]), int(idx[-1]) + 1) if idx.size else (0, len(flags))

    x0, x1 = span(col_dark)
    y0, y1 = span(row_dark)
    x0, y0 = x0 + (x0 % 2), y0 + (y0 % 2)
    cw, ch = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    if cw >= w - 8 and ch >= h - 8:
        return None
    if cw < w * 0.4 or ch < h * 0.4:
        return None                              # mostly dark footage, not bars
    return {"w": cw, "h": ch, "x": x0, "y": y0}
