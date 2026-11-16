#!/usr/bin/env python3
"""Clipper - watches streamers on Twitch/Kick/YouTube and turns new VODs into vertical shorts.

  python run.py check                     list new VODs without downloading anything
  python run.py once                      check all streamers + clip new VODs (what the timer runs)
  python run.py process URL --name Adin   clip one specific VOD/video URL
  python run.py loop --every 30           run "once" forever, every N minutes
"""
import argparse
import fcntl
import logging
import os
import sys
import time

import yaml

from clipper import monitor, pipeline
from clipper.db import DB

HERE = os.path.dirname(os.path.abspath(__file__))


def setup_logging():
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-10s %(message)s",
                        datefmt="%m-%d %H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(os.path.join(HERE, "logs", "clipper.log"))])


def load_cfg():
    path = os.path.join(HERE, "config.yaml")
    if not os.path.exists(path):
        sys.exit("config.yaml missing - run: cp config.example.yaml config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def once(cfg, db, no_captions=False):
    monitor.discover(cfg, db)
    limit, done = cfg["run"].get("max_vods_per_run", 3), 0
    for vod in db.pending(50):
        if done >= limit:
            break
        try:
            res = pipeline.process(vod, cfg, transcribe=not no_captions)
            if res == "live":
                logging.info("[%s] still live, will retry later", vod["streamer"])
                continue  # doesn't count toward the limit
            done += 1
            db.set_status(vod["key"], "done")
            logging.info("[%s] done: %d clips", vod["streamer"], len(res))
        except Exception as e:
            done += 1
            logging.error("[%s] failed: %s -> %s", vod["streamer"], vod["url"], e)
            db.set_status(vod["key"], "failed", str(e)[:500])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "once", "process", "loop"])
    ap.add_argument("url", nargs="?")
    ap.add_argument("--name", default="Streamer", help="streamer name for 'process'")
    ap.add_argument("--every", type=int, default=30, help="minutes between runs for 'loop'")
    ap.add_argument("--no-captions", action="store_true", help="skip Whisper (faster, no captions)")
    a = ap.parse_args()
    os.chdir(HERE)
    setup_logging()
    cfg = load_cfg()

    lock = open(os.path.join(HERE, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another clipper run is already going")

    db = DB(os.path.join(HERE, "clipper.db"))
    if a.cmd == "check":
        new = monitor.discover(cfg, db, dry_run=True)
        print(f"\n{len(new)} new VOD(s) would be clipped.")
    elif a.cmd == "process":
        if not a.url:
            sys.exit("usage: python run.py process URL --name 'Streamer'")
        plat = "kick" if "kick.com" in a.url else "twitch" if "twitch.tv" in a.url else "youtube"
        vod = {"key": f"manual:{abs(hash(a.url))}", "platform": plat, "streamer": a.name,
               "title": "manual", "url": a.url}
        res = pipeline.process(vod, cfg, transcribe=not a.no_captions)
        print("still live - try again after the stream" if res == "live" else "\n".join(res))
    elif a.cmd == "once":
        once(cfg, db, a.no_captions)
    else:
        while True:
            once(cfg, db, a.no_captions)
            time.sleep(a.every * 60)


if __name__ == "__main__":
    main()
