"""Finds the best moments in a VOD.

1. Loudness pass over the whole audio (fast): finds spikes above the streamer's normal level.
2. Transcribes only the top candidate windows (slow part, kept small).
3. Re-scores with hype words + how much is being said, keeps the best N.
"""
import logging
import re

import numpy as np

log = logging.getLogger("highlights")
SR = 16000

_model = None


def load_pcm(path):
    """16 kHz mono s16le raw file -> read-only memmap (no RAM blowup on 5h streams)."""
    return np.memmap(path, dtype=np.int16, mode="r")


def loudness_per_second(pcm):
    n = len(pcm) // SR
    db = np.empty(n, dtype=np.float32)
    step = 600  # seconds per chunk
    for i in range(0, n, step):
        j = min(n, i + step)
        x = pcm[i * SR:j * SR].astype(np.float32).reshape(j - i, SR) / 32768.0
        rms = np.sqrt(np.mean(x * x, axis=1) + 1e-10)
        db[i:j] = 20 * np.log10(rms)
    return db


def _moving_avg(x, w):
    if len(x) == 0:
        return x
    w = max(1, min(w, len(x)))
    c = np.cumsum(np.insert(x.astype(np.float64), 0, 0))
    half = w // 2
    idx = np.arange(len(x))
    lo = np.clip(idx - half, 0, len(x))
    hi = np.clip(idx + half + 1, 0, len(x))
    return ((c[hi] - c[lo]) / (hi - lo)).astype(np.float32)


def candidates(db, hl):
    before, after = hl["clip_before"], hl["clip_after"]
    k = hl["clips_per_vod"] * hl.get("candidates_multiplier", 3)
    smooth = _moving_avg(db, 3)
    baseline = _moving_avg(db, 300)
    excite = smooth - baseline
    excite[db < -50] = -99           # ignore silence / BRB screens
    excite[:before] = -99            # need room for the setup
    excite[len(excite) - after:] = -99
    picked = []
    for sec in np.argsort(-excite):
        if excite[sec] <= 0 or len(picked) >= k:
            break
        if all(abs(int(sec) - p) >= hl["min_gap"] for p, _ in picked):
            picked.append((int(sec), float(excite[sec])))
    return [{"peak": p, "start": float(p - before), "end": float(p + after), "excite": e}
            for p, e in picked]


def get_model(tcfg):
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info("loading whisper model %s", tcfg["model"])
        _model = WhisperModel(tcfg["model"], device="cpu", compute_type=tcfg["compute_type"],
                              cpu_threads=tcfg.get("threads", 4))
    return _model


def transcribe_window(pcm, start, end, tcfg):
    """Returns list of words: {start, end, word} relative to window start.
    Audio is passed as a numpy array, which avoids PyAV file decoding entirely."""
    audio = pcm[int(start * SR):int(end * SR)].astype(np.float32) / 32768.0
    segs, _ = get_model(tcfg).transcribe(audio, language=tcfg.get("language"),
                                         word_timestamps=True, vad_filter=True)
    words = []
    for s in segs:
        for w in s.words or []:
            words.append({"start": float(w.start), "end": float(w.end), "word": w.word.strip()})
    return words


def hype_score(words, hype_words):
    text = " " + " ".join(w["word"] for w in words).lower() + " "
    text = re.sub(r"[^a-z' ]", " ", text)
    hits = sum(text.count(f" {h} ") for h in hype_words)
    return min(hits, 6)


def find(pcm, cfg, transcribe=True):
    hl, tcfg = cfg["highlights"], cfg["transcribe"]
    db = loudness_per_second(pcm)
    cands = candidates(db, hl)
    log.info("audio %.1f min, %d candidate moments", len(db) / 60, len(cands))
    if not cands:
        return []
    ex = np.array([c["excite"] for c in cands])
    z = (ex - ex.mean()) / (ex.std() + 1e-6)
    for c, zi in zip(cands, z):
        c["words"] = []
        if transcribe:
            try:
                c["words"] = transcribe_window(pcm, c["start"], c["end"], tcfg)
            except Exception as e:
                log.warning("transcription failed (%s); clipping without captions", e)
                transcribe = False
        dur = c["end"] - c["start"]
        wps = len(c["words"]) / dur
        c["score"] = float(zi) + 0.5 * hype_score(c["words"], hl["hype_words"]) + 0.3 * min(wps, 3)
        # trim dead air at the start, keep at least 20 s
        if c["words"]:
            first = c["words"][0]["start"]
            shift = max(0.0, first - 1.0)
            if shift > 1.5 and dur - shift >= 20:
                c["start"] += shift
                for w in c["words"]:
                    w["start"] -= shift
                    w["end"] -= shift
    best = sorted(cands, key=lambda c: -c["score"])[:hl["clips_per_vod"]]
    return sorted(best, key=lambda c: c["start"])
