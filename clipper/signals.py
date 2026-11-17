"""Crowd signals: where viewers clipped the stream.

If many people clipped the same minute of a VOD, that's a proven highlight.
Kick clips are public. Twitch needs a free app at dev.twitch.tv (client id + secret
in config.yaml under `twitch:`). Every function here fails soft: no hints, no crash.
"""
import logging
import math
import re
from datetime import datetime, timezone

log = logging.getLogger("signals")


def _ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _get(url, **kw):
    from curl_cffi import requests as creq
    r = creq.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- Kick
def _kick_videos(slug):
    vids = _get(f"https://kick.com/api/v2/channels/{slug}/videos", impersonate="chrome")
    return vids if isinstance(vids, list) else vids.get("data", [])


def kick_latest(url):
    """kick.com/<slug> (no video id) -> URL of that channel's newest finished VOD."""
    m = re.fullmatch(r"https?://(?:www\.)?kick\.com/([\w-]+)/?", url.strip())
    if not m:
        return url
    slug = m.group(1)
    for v in _kick_videos(slug):
        uuid = (v.get("video") or {}).get("uuid")
        if uuid and not v.get("is_live"):
            return f"https://kick.com/{slug}/videos/{uuid}"
    return url


def _kick_offset(c, start, dur):
    v = c.get("vod_starts_at")
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d+(\.\d+)?", v)):
        off = float(v)
        return off / 1000 if off > dur * 5 else off
    t = _ts(v) if isinstance(v, str) else None
    if t and t > 0 and start:
        return t - start
    made = _ts(c.get("created_at"))
    if made and start:
        return made - float(c.get("duration") or 30) - start
    return None


def kick_clips(url):
    m = re.search(r"kick\.com/([\w-]+)/videos?/([0-9a-f-]{36})", url)
    if not m:
        return []
    slug, uuid = m.groups()
    item = next((v for v in _kick_videos(slug)
                 if (v.get("video") or {}).get("uuid") == uuid or v.get("uuid") == uuid), None)
    if not item:
        log.info("kick: VOD %s no longer in the channel's list (deleted?)", uuid)
        return []
    stream_id = str(item.get("id"))
    dur = item.get("duration") or 0
    dur = dur / 1000 if dur > 86400 else dur
    ca = item.get("created_at") or ""
    start = _ts(ca) if ("Z" in ca or "+" in ca) else _ts(item.get("start_time"))
    oldest_ok = (start or 0) - 86400
    out, cursor, sample = [], None, None
    for _ in range(15):  # clips come newest first; stop once we're well before the stream
        q = f"https://kick.com/api/v2/channels/{slug}/clips?sort=date&time=all"
        if cursor:
            q += f"&cursor={cursor}"
        data = _get(q, impersonate="chrome")
        clips = data.get("clips") or []
        if not clips:
            break
        for c in clips:
            if str(c.get("livestream_id")) != stream_id:
                continue
            sample = sample or c.get("vod_starts_at")
            off = _kick_offset(c, start, dur)
            cdur = float(c.get("duration") or 30)
            if off is not None and 0 <= off <= dur:
                out.append({"start": off, "end": off + cdur,
                            "views": int(c.get("views") or c.get("view_count") or 0),
                            "title": c.get("title") or ""})
        last = _ts(clips[-1].get("created_at")) or 0
        cursor = data.get("nextCursor")
        if not cursor or (start and last < oldest_ok):
            break
    if sample is not None:
        log.info("kick: sample vod_starts_at=%r", sample)
    return out


# ---------------------------------------------------------------- Twitch
def _dur(s):
    h = re.search(r"(\d+)h", s or "")
    m = re.search(r"(\d+)m", s or "")
    sec = re.search(r"(\d+)s", s or "")
    return sum(int(x.group(1)) * k for x, k in ((h, 3600), (m, 60), (sec, 1)) if x)


def twitch_clips(url, tcfg):
    m = re.search(r"twitch\.tv/videos/(\d+)", url)
    if not m or not tcfg or not tcfg.get("client_id") or not tcfg.get("client_secret"):
        if m:
            log.info("twitch: no client_id/secret in config - skipping viewer clips")
        return []
    vid = m.group(1)
    from curl_cffi import requests as creq
    tok = creq.post("https://id.twitch.tv/oauth2/token", timeout=30, params={
        "client_id": tcfg["client_id"], "client_secret": tcfg["client_secret"],
        "grant_type": "client_credentials"}).json()["access_token"]
    h = {"Client-Id": tcfg["client_id"], "Authorization": f"Bearer {tok}"}
    v = _get(f"https://api.twitch.tv/helix/videos?id={vid}", headers=h)["data"][0]
    start = v["created_at"]
    end_ts = _ts(start) + _dur(v["duration"]) + 2 * 86400
    end = datetime.fromtimestamp(end_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, cursor = [], None
    for _ in range(5):
        q = (f"https://api.twitch.tv/helix/clips?broadcaster_id={v['user_id']}&started_at={start}"
             f"&ended_at={end}&first=100")
        if cursor:
            q += f"&after={cursor}"
        data = _get(q, headers=h)
        for c in data.get("data", []):
            if c.get("video_id") == vid and c.get("vod_offset") is not None:
                off = float(c["vod_offset"])
                out.append({"start": off, "end": off + float(c.get("duration") or 30),
                            "views": int(c.get("view_count") or 0), "title": c.get("title") or ""})
        cursor = (data.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    return out


# ---------------------------------------------------------------- combine
def viewer_clips(vod, cfg):
    try:
        if vod["platform"] == "kick" or "kick.com" in vod["url"]:
            clips = kick_clips(vod["url"])
        elif vod["platform"] == "twitch" or "twitch.tv" in vod["url"]:
            clips = twitch_clips(vod["url"], cfg.get("twitch"))
        else:
            clips = []
    except Exception as e:
        log.warning("viewer clips unavailable (%s)", str(e)[:200])
        return []
    log.info("found %d viewer clips on this VOD", len(clips))
    return cluster(clips)


def cluster(clips, gap=45):
    """Merge clips of the same moment. Returns hints sorted by strength."""
    groups = []
    for c in sorted(clips, key=lambda c: c["start"]):
        if groups and c["start"] - groups[-1]["end"] <= gap / 3 and c["start"] - groups[-1]["start"] <= gap:
            g = groups[-1]
            g["end"] = max(g["end"], c["end"])
            g["count"] += 1
            g["views"] += c["views"]
            if c["views"] > g["top_views"]:
                g["top_views"], g["title"] = c["views"], c["title"]
        else:
            groups.append({**c, "count": 1, "top_views": c["views"]})
    for g in groups:
        g["strength"] = g["count"] + 0.5 * math.log1p(g["views"])
    return sorted(groups, key=lambda g: -g["strength"])
