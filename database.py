import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from instagram import AccountState

@dataclass
class Monitor:
    username: str
    mode: str
    channel_id: int
    user_id: int
    started_at: datetime
    last_state: AccountState

class Database:
    def __init__(self, path: str):
        self.path = path
        parent = Path(path).expanduser().parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self):
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS monitors (
                    username TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    last_state TEXT NOT NULL,
                    last_checked TEXT NOT NULL,
                    followers INTEGER DEFAULT 0,
                    following INTEGER DEFAULT 0,
                    posts INTEGER DEFAULT 0,
                    profile_pic_url TEXT
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    elapsed_seconds INTEGER NOT NULL,
                    followers INTEGER DEFAULT 0,
                    following INTEGER DEFAULT 0,
                    posts INTEGER DEFAULT 0
                )
            """)

    def add_monitor(self, username, mode, channel_id, user_id, result):
        now = datetime.now(timezone.utc).isoformat()
        p = result.profile
        with self._connect() as con:
            con.execute("""
                INSERT OR REPLACE INTO monitors
                (username, mode, channel_id, user_id, started_at, last_state, last_checked,
                 followers, following, posts, profile_pic_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                username, mode, channel_id, user_id, now, result.state.value, now,
                p.followers if p else 0, p.following if p else 0, p.posts if p else 0,
                p.profile_pic_url if p else None,
            ))

    def stop_monitor(self, username):
        with self._connect() as con:
            cur = con.execute("DELETE FROM monitors WHERE username = ?", (username,))
            return cur.rowcount > 0

    def get_monitors(self):
        with self._connect() as con:
            rows = con.execute("SELECT * FROM monitors ORDER BY started_at ASC").fetchall()
        out = []
        for r in rows:
            out.append(Monitor(
                username=r["username"],
                mode=r["mode"],
                channel_id=r["channel_id"],
                user_id=r["user_id"],
                started_at=datetime.fromisoformat(r["started_at"]),
                last_state=AccountState(r["last_state"]),
            ))
        return out

    def update_last_check(self, username, result):
        now = datetime.now(timezone.utc).isoformat()
        p = result.profile
        with self._connect() as con:
            con.execute("""
                UPDATE monitors
                SET last_state=?, last_checked=?, followers=?, following=?, posts=?, profile_pic_url=?
                WHERE username=?
            """, (
                result.state.value, now,
                p.followers if p else 0, p.following if p else 0, p.posts if p else 0,
                p.profile_pic_url if p else None, username
            ))

    def add_history(self, username, event, started_at, profile):
        elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
        with self._connect() as con:
            con.execute("""
                INSERT INTO history
                (username, event, detected_at, elapsed_seconds, followers, following, posts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                username, event, datetime.now(timezone.utc).isoformat(), elapsed,
                profile.followers if profile else 0,
                profile.following if profile else 0,
                profile.posts if profile else 0,
            ))

    def get_history(self, limit=10):
        with self._connect() as con:
            return con.execute(
                "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
