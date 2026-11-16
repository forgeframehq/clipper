"""Finds new VODs from Twitch, Kick and YouTube channels."""
import logging
from datetime import datetime, timezone

import yt_dlp

log = logging.getLogger("monitor")


COOKIES = None  # set by discover() from config


def _flat(url, limit=10):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "playlistend": limit, "skip_download": True}
    if COOKIES:
        opts["cookiefile"] = COOKIES
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return [e for e in (info or {}).get("entries") or [] if e]


def twitch_vods(user):
    out = []
    for e in _flat(f"https://www.twitch.tv/{user}/videos?filter=archives&sort=time"):
        vid = str(e.get("id", "")).lstrip("v")
        if not vid:
            continue
        out.append({"key": f"twitch:{vid}", "platform": "twitch", "title": e.get("title") or "",
                    "url": f"https://www.twitch.tv/videos/{vid}",
                    "duration": e.get("duration"), "timestamp": e.get("timestamp")})
    return out


def youtube_vods(handle):
    handle = handle if handle.startswith("@") else "@" + handle
    out, seen = [], set()
    for tab in ("streams", "videos"):
        try:
            entries = _flat(f"https://www.youtube.com/{handle}/{tab}")
        except Exception as ex:  # a channel may not have a streams tab
            log.debug("youtube %s/%s: %s", handle, tab, ex)
            continue
        for e in entries:
            vid = e.get("id")
            if not vid or vid in seen or e.get("live_status") in ("is_live", "is_upcoming"):
                continue
            seen.add(vid)
            out.append({"key": f"youtube:{vid}", "platform": "youtube", "title": e.get("title") or "",
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "duration": e.get("duration"), "timestamp": e.get("timestamp")})
    return out


def _parse_time(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def kick_vods(slug):
    # Kick's site API. Cloudflare blocks plain requests, so impersonate Chrome.
    from curl_cffi import requests as creq
    r = creq.get(f"https://kick.com/api/v2/channels/{slug}/videos", impersonate="chrome", timeout=30)
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else data.get("data", [])
    out = []
    for it in items:
        if it.get("is_live"):
            continue  # still streaming; pick it up once it ends
        video = it.get("video") or {}
        uuid = video.get("uuid") or it.get("uuid")
        if not uuid:
            continue
        dur = it.get("duration") or 0
        dur = dur / 1000 if dur > 86400 else dur  # Kick reports milliseconds
        out.append({"key": f"kick:{uuid}", "platform": "kick",
                    "title": it.get("session_title") or video.get("title") or "",
                    "url": f"https://kick.com/{slug}/videos/{uuid}",
                    "alt_url": it.get("source"),  # direct m3u8, used if the page URL fails
                    "duration": dur or None,
                    "timestamp": _parse_time(it.get("start_time") or it.get("created_at"))})
    return out


FETCHERS = {"twitch": twitch_vods, "kick": kick_vods, "youtube": youtube_vods}


def passes_filters(vod, filters):
    kws = [k.lower() for k in filters.get("title_keywords") or []]
    if kws and not any(k in vod["title"].lower() for k in kws):
        return False, "title has no keyword"
    dur = vod.get("duration")
    if dur and dur < filters.get("min_duration_min", 0) * 60:
        return False, "too short"
    ts = vod.get("timestamp")
    max_age = filters.get("max_age_hours")
    if ts and max_age and (datetime.now(timezone.utc).timestamp() - ts) > max_age * 3600:
        return False, "too old"
    return True, ""


def discover(cfg, db, dry_run=False):
    """Check every streamer on every platform; queue new VODs. Returns list of new VODs."""
    global COOKIES
    import os
    ck = cfg.get("run", {}).get("cookies_file")
    COOKIES = ck if ck and os.path.exists(ck) else None
    filters = cfg.get("filters", {})
    backfill = filters.get("backfill_per_channel", 1)
    new = []
    for s in cfg["streamers"]:
        for platform, fetch in FETCHERS.items():
            handle = s.get(platform)
            if not handle:
                continue
            chan = f"{platform}:{handle}"
            try:
                vods = fetch(handle)
            except Exception as ex:
                log.warning("%s %s: could not list videos (%s)", s["name"], chan, str(ex)[:200])
                continue
            first_time = not db.channel_known(chan)
            log.info("%s %s: %d videos listed", s["name"], chan, len(vods))
            for i, v in enumerate(vods):
                if db.has(v["key"]):
                    continue
                v["streamer"] = s["name"]
                ok, why = passes_filters(v, filters)
                if first_time and i >= backfill:
                    ok, why = False, "backlog"
                if dry_run:
                    log.info("  %s %-9s %s", "NEW " if ok else "skip", why, v["title"][:70])
                    if ok:
                        new.append(v)
                    continue
                db.add(v, "pending" if ok else "seen")
                if ok:
                    new.append(v)
                    log.info("  queued: %s", v["title"][:80])
            if not dry_run:
                db.mark_channel(chan)
    return new
