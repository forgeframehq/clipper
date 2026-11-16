"""Tiny SQLite store that remembers which VODs we've already seen / clipped."""
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS vods (
    key TEXT PRIMARY KEY,          -- platform:id
    platform TEXT, streamer TEXT, title TEXT, url TEXT, alt_url TEXT,
    status TEXT,                   -- pending | done | skipped | failed | seen
    attempts INTEGER DEFAULT 0,
    error TEXT,
    found_at REAL, processed_at REAL
);
CREATE TABLE IF NOT EXISTS channels (key TEXT PRIMARY KEY, first_seen REAL);
"""


class DB:
    def __init__(self, path="clipper.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def channel_known(self, key):
        return self.conn.execute("SELECT 1 FROM channels WHERE key=?", (key,)).fetchone() is not None

    def mark_channel(self, key):
        self.conn.execute("INSERT OR IGNORE INTO channels VALUES (?,?)", (key, time.time()))
        self.conn.commit()

    def has(self, key):
        return self.conn.execute("SELECT 1 FROM vods WHERE key=?", (key,)).fetchone() is not None

    def add(self, vod, status="pending"):
        self.conn.execute(
            "INSERT OR IGNORE INTO vods (key, platform, streamer, title, url, alt_url, status, found_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (vod["key"], vod["platform"], vod["streamer"], vod["title"], vod["url"],
             vod.get("alt_url"), status, time.time()))
        self.conn.commit()

    def pending(self, limit):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM vods WHERE status='pending' OR (status='failed' AND attempts<3)"
            " ORDER BY found_at DESC LIMIT ?", (limit,))]

    def set_status(self, key, status, error=None):
        self.conn.execute(
            "UPDATE vods SET status=?, error=?, processed_at=?,"
            " attempts=attempts + (CASE WHEN ?='failed' THEN 1 ELSE 0 END) WHERE key=?",
            (status, error, time.time(), status, key))
        self.conn.commit()
