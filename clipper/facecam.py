"""Finds the streamer's facecam in a video section so we can put it on top (split layout)."""
import logging
import subprocess

import numpy as np

log = logging.getLogger("facecam")
DW = 960  # detection width


def _frames(src, offset, duration, n):
    out = []
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", src], capture_output=True, text=True)
    w, h = [int(x) for x in probe.stdout.strip().split(",")[:2]]
    dh = int(DW * h / w) // 2 * 2
    for i in range(n):
        t = offset + duration * (i + 0.5) / n
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", src, "-frames:v", "1",
                              "-vf", f"scale={DW}:{dh}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True).stdout
        if len(raw) == DW * dh:
            out.append(np.frombuffer(raw, dtype=np.uint8).reshape(dh, DW))
    return out, w, h


def detect(src, offset, duration, aspect=1.5, n=8):
    """Returns (x, y, w, h) crop in source pixels around the streamer's face, or None."""
    try:
        import cv2
    except ImportError:
        log.warning("opencv not installed (pip install opencv-python-headless); no split layout")
        return None
    frames, sw, sh = _frames(src, offset, duration, n)
    if not frames:
        return None
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = []
    for f in frames:
        found = casc.detectMultiScale(f, scaleFactor=1.1, minNeighbors=6, minSize=(36, 36))
        if len(found):
            faces.append(max(found, key=lambda b: b[2] * b[3]))
    if len(faces) < max(2, len(frames) // 3):
        log.info("no steady face found (%d/%d frames)", len(faces), len(frames))
        return None
    faces = np.array(faces, dtype=float)
    cx = np.median(faces[:, 0] + faces[:, 2] / 2)
    cy = np.median(faces[:, 1] + faces[:, 3] / 2)
    fh = np.median(faces[:, 3])
    # keep only detections near the median (drops stray faces in the gameplay)
    near = faces[(abs(faces[:, 0] + faces[:, 2] / 2 - cx) < fh * 1.5) & (abs(faces[:, 1] + faces[:, 3] / 2 - cy) < fh * 1.5)]
    if len(near) < 2:
        return None
    k = sw / DW
    ch = min(sh, fh * 2.6 * k)
    cw = min(sw, ch * aspect)
    ch = min(ch, cw / aspect) if cw / aspect <= sh else ch
    x = min(max(0, cx * k - cw / 2), sw - cw)
    y = min(max(0, (cy + fh * 0.15) * k - ch / 2), sh - ch)
    box = tuple(int(v) // 2 * 2 for v in (x, y, cw, ch))
    log.info("facecam at x=%d y=%d w=%d h=%d (face in %d/%d frames)", *box, len(near), len(frames))
    return box
