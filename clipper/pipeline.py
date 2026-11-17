"""Processes one VOD end to end: audio -> highlights -> download sections -> render shorts."""
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

from . import edit, highlights, signals

log = logging.getLogger("pipeline")
PAD = 4  # extra seconds downloaded around each section (cuts land on keyframes)
EXTRA = []  # extra yt-dlp args (e.g. --cookies), set from config in process()


class YtdlpError(subprocess.CalledProcessError):
    def __str__(self):
        lines = [l for l in (self.stderr or "").splitlines() if l.strip()]
        return "yt-dlp: " + (" | ".join(lines[-3:]) or f"exit {self.returncode}")


def ytdlp(*args):
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress", *EXTRA, *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise YtdlpError(r.returncode, cmd, r.stdout, r.stderr)
    return r


def info(url):
    return json.loads(ytdlp("-J", "--no-playlist", url).stdout)


def _first_working(urls, fn):
    err = None
    for u in [u for u in urls if u]:
        try:
            return fn(u)
        except subprocess.CalledProcessError as e:
            err = e
            log.warning("failed on %s -> %s", u, e)
    raise err or RuntimeError("no URL to try")


def download_audio(urls, work):
    raw = os.path.join(work, "audio.pcm")

    def go(u):
        ytdlp("-f", "ba/wa/w", "-o", os.path.join(work, "src.%(ext)s"), u)
        src = next(p for p in glob.glob(os.path.join(work, "src.*")) if not p.endswith(".part"))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vn", "-ac", "1",
                        "-ar", "16000", "-f", "s16le", raw], check=True)
        os.remove(src)
        return raw
    return _first_working(urls, go)


def download_section(urls, start, end, out_base, max_h):
    s, e = max(0, start - PAD), end + PAD

    def go(u):
        ytdlp("-f", f"bv*[height<={max_h}]+ba/b[height<={max_h}]/b", "--download-sections", f"*{s:.0f}-{e:.0f}",
              "--merge-output-format", "mp4", "-o", out_base + ".%(ext)s", u)
        return next(p for p in glob.glob(out_base + ".*") if not p.endswith(".part"))
    return _first_working(urls, go), start - s  # file, offset of the clip inside it


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "clip"


def process(vod, cfg, transcribe=True):
    run = cfg["run"]
    key_safe = slug(vod["key"])
    work = os.path.join(run["work_dir"], key_safe)
    out_dir = os.path.join(run["output_dir"], datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(work, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    urls = [vod["url"], vod.get("alt_url")]
    cookies = run.get("cookies_file")
    EXTRA[:] = ["--cookies", cookies] if cookies and os.path.exists(cookies) else []
    try:
        if vod["platform"] in ("twitch", "youtube"):
            try:
                meta = info(vod["url"])
                if meta.get("live_status") == "is_live" or meta.get("is_live"):
                    return "live"
            except YtdlpError as e:
                log.warning("could not read video info (%s); trying the download anyway", e)
        log.info("[%s] downloading audio: %s", vod["streamer"], vod["title"][:70])
        pcm_path = download_audio(urls, work)
        pcm = highlights.load_pcm(pcm_path)
        hints = signals.viewer_clips(vod, cfg)
        moments = highlights.find(pcm, cfg, transcribe=transcribe, hints=hints)
        made = []
        for n, m in enumerate(moments, 1):
            base = os.path.join(work, f"sec{n}")
            src, offset = download_section(urls, m["start"], m["end"], base, cfg["edit"].get("max_height", 1080))
            real_start, conf = highlights.align(pcm, src, m["start"] - offset)
            if conf > 0.5:
                offset = m["start"] - real_start
            else:
                log.warning("  could not align clip %d audio (match %.2f); captions may drift", n, conf)
            name = f"{slug(vod['streamer'])}_{key_safe[-12:]}_{n}"
            out = os.path.join(out_dir, name + ".mp4")
            dur = m["end"] - m["start"]
            edit.render(src, out, offset, dur, m["words"], cfg["edit"], credit=vod["streamer"])
            os.remove(src)
            write_post_notes(out, vod, m)
            made.append(out)
            log.info("  clip %d/%d -> %s (score %.2f)", n, len(moments), out, m["score"])
        del pcm
        return made
    finally:
        if not run.get("keep_temp"):
            shutil.rmtree(work, ignore_errors=True)


def write_post_notes(out, vod, m):
    t = int(m["start"])
    quote = " ".join(w["word"] for w in m["words"])[:120]
    tag = "".join(c for c in vod["streamer"].title() if c.isalnum())
    with open(out[:-4] + ".txt", "w", encoding="utf-8") as f:
        f.write(f"Streamer: {vod['streamer']} ({vod['platform']})\n")
        f.write(f"Source: {vod['url']}  @ {t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}\n")
        f.write(f"VOD title: {vod['title']}\n")
        f.write(f"Said: {quote}\n\n")
        f.write(f"Suggested caption:\n{vod['streamer']} 😭 #{tag} #clips #fyp #streamer\n")
