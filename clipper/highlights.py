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


def excitement(db, hl):
    smooth = _moving_avg(db, 3)
    baseline = _moving_avg(db, 300)
    excite = smooth - baseline
    excite[db < -50] = -99           # ignore silence / BRB screens
    return excite


def candidates(excite, hl, k, taken=()):
    before, after = hl["clip_before"], hl["clip_after"]
    ex = excite.copy()
    ex[:before] = -99                # need room for the setup
    ex[len(ex) - after:] = -99
    picked = []
    for sec in np.argsort(-ex):
        if ex[sec] <= 0 or len(picked) >= k:
            break
        sec = int(sec)
        if any(s - hl["min_gap"] / 2 <= sec <= e + hl["min_gap"] / 2 for s, e in taken):
            continue
        if all(abs(sec - p) >= hl["min_gap"] for p, _ in picked):
            picked.append((sec, float(ex[sec])))
    return [{"peak": p, "start": float(p - before), "end": float(p + after), "excite": e, "src": "loud"}
            for p, e in picked]


def hint_candidates(hints, excite, total, limit=10):
    """Turn viewer-clip clusters into 25-55 s windows, a little setup before, reaction after."""
    out = []
    for h in hints[:limit]:
        start, end = max(0.0, h["start"] - 8), min(total, h["end"] + 4)
        if end - start < 25:
            start = max(0.0, end - 25)
        if end - start > 55:
            start = end - 55
        if end - start < 15:
            continue
        seg = excite[int(start):int(end)]
        out.append({"peak": int(start), "start": float(start), "end": float(end),
                    "excite": float(seg.max()) if len(seg) else 0.0, "src": "viewers",
                    "strength": h["strength"], "count": h["count"], "views": h["views"],
                    "clip_title": h.get("title", "")})
    return out


def section_audio(path, secs=20):
    """First `secs` seconds of a downloaded file as 16 kHz mono float."""
    import subprocess
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-t", str(secs), "-vn", "-ac", "1",
                          "-ar", str(SR), "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def align(pcm, path, approx_start, search=25):
    """Find where a downloaded section really starts inside the full VOD audio.
    HLS sections begin on segment boundaries (2-10 s off), so we match audio."""
    probe = section_audio(path)
    if len(probe) < SR * 2:
        return approx_start, 0.0
    lo = max(0.0, approx_start - search)
    ref = pcm[int(lo * SR):int((approx_start + search) * SR) + len(probe)].astype(np.float32) / 32768.0
    if len(ref) <= len(probe):
        return approx_start, 0.0
    n = 1 << int(np.ceil(np.log2(len(ref) + len(probe))))
    corr = np.fft.irfft(np.fft.rfft(ref, n) * np.conj(np.fft.rfft(probe, n)), n)[:len(ref) - len(probe) + 1]
    e = np.concatenate([[0.0], np.cumsum(ref.astype(np.float64) ** 2)])
    seg_norm = np.sqrt(e[len(probe):] - e[:-len(probe)]) + 1e-9
    ncc = corr / (seg_norm * (np.linalg.norm(probe) + 1e-9))
    lag = int(np.argmax(ncc))
    return lo + lag / SR, float(ncc[lag])


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


def snap_end(words, start, end, tail_end):
    """Don't cut mid-sentence: move the end to the next sentence end or pause in speech.
    Looks from 3 s before the planned end up to tail_end. Word times are relative to start."""
    planned = end - start
    for i, w in enumerate(words):
        if w["end"] < planned - 3:
            continue
        if w["start"] > planned + 1:
            break                          # nobody was mid-sentence at the planned end
        nxt = words[i + 1] if i + 1 < len(words) else None
        pause = nxt is None or nxt["start"] - w["end"] > 0.7
        if pause or w["word"][-1:] in ".?!":
            cut = min(w["end"] + 0.6, tail_end - start)
            return start + cut, [x for x in words if x["end"] <= cut]
    cut = min(planned, tail_end - start)   # nobody talking near the end: keep the planned cut
    return start + cut, [x for x in words if x["end"] <= cut]


def hype_score(words, hype_words):
    text = " " + " ".join(w["word"] for w in words).lower() + " "
    text = re.sub(r"[^a-z' ]", " ", text)
    hits = sum(text.count(f" {h} ") for h in hype_words)
    return min(hits, 6)


def find(pcm, cfg, transcribe=True, hints=None):
    hl, tcfg = cfg["highlights"], cfg["transcribe"]
    db = loudness_per_second(pcm)
    excite = excitement(db, hl)
    hinted = hint_candidates(hints or [], excite, len(db))
    k = hl["clips_per_vod"] * (2 if hinted else hl.get("candidates_multiplier", 3))
    cands = hinted + candidates(excite, hl, k, taken=[(c["start"], c["end"]) for c in hinted])
    log.info("audio %.1f min: %d moments from viewer clips + %d from loudness",
             len(db) / 60, len(hinted), len(cands) - len(hinted))
    if not cands:
        return []
    ex = np.array([c["excite"] for c in cands])
    z = (ex - ex.mean()) / (ex.std() + 1e-6)
    for c, zi in zip(cands, z):
        c["words"] = []
        if transcribe:
            try:
                slack = hl.get("end_slack", 12)
                tail_end = min(len(db), c["end"] + slack)
                words = transcribe_window(pcm, c["start"], tail_end, tcfg)
                c["end"], c["words"] = snap_end(words, c["start"], c["end"], tail_end)
            except Exception as e:
                log.warning("transcription failed (%s); clipping without captions", e)
                transcribe = False
        dur = c["end"] - c["start"]
        wps = len(c["words"]) / dur
        c["score"] = float(zi) + 0.5 * hype_score(c["words"], hl["hype_words"]) + 0.3 * min(wps, 3)
        if c["src"] == "viewers":    # people chose to clip this - trust it most
            c["score"] += 1.2 * c["strength"]  # 1 junk clip ~ +1.2; 10 clips w/ 5k views ~ +17
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
    for c in best:
        log.info("  pick %s @%s score %.2f%s", c["src"], _hms(c["start"]), c["score"],
                 f" ({c['count']} clips, {c['views']} views)" if c["src"] == "viewers" else "")
    return sorted(best, key=lambda c: c["start"])


def _hms(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"
