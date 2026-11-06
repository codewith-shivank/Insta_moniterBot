import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from instagram import AccountState

@dataclass
class Monitor:
    username: str
    mode: str
    channel_id: int
    user_id: int
    started_at: datetime
    last_state: AccountState
    followers: int = 0
    following: int = 0
    posts: int = 0
    profile_pic_url: Optional[str] = None
    userid: Optional[str] = None
    bio: Optional[str] = None
    fullname: Optional[str] = None
    is_verified: bool = False
    external_url: Optional[str] = None
    check_interval: int = 0
    muted_until: Optional[datetime] = None
    alert_mention: Optional[str] = None
    last_checked: Optional[datetime] = None

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

    def _add_column(self, con, table, column, definition):
        """Add a column if it doesn't already exist. Safe to call on every boot."""
        cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
            # Migrations for older databases created before these columns existed.
            self._add_column(con, "monitors", "userid", "TEXT")
            self._add_column(con, "monitors", "bio", "TEXT")
            self._add_column(con, "monitors", "fullname", "TEXT")
            self._add_column(con, "monitors", "is_verified", "INTEGER DEFAULT 0")
            self._add_column(con, "monitors", "external_url", "TEXT")
            self._add_column(con, "monitors", "check_interval", "INTEGER DEFAULT 0")
            self._add_column(con, "monitors", "muted_until", "TEXT")
            self._add_column(con, "monitors", "alert_mention", "TEXT")

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
            con.execute("""
                CREATE TABLE IF NOT EXISTS stats_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    followers INTEGER DEFAULT 0,
                    following INTEGER DEFAULT 0,
                    posts INTEGER DEFAULT 0
                )
            """)
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_stats_history_username "
                "ON stats_history(username, checked_at)"
            )

    def add_monitor(self, username, mode, channel_id, user_id, result, check_interval=0):
        now = datetime.now(timezone.utc).isoformat()
        p = result.profile
        with self._connect() as con:
            con.execute("""
                INSERT OR REPLACE INTO monitors
                (username, mode, channel_id, user_id, started_at, last_state, last_checked,
                 followers, following, posts, profile_pic_url, userid, bio, fullname,
                 is_verified, external_url, check_interval, muted_until, alert_mention)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                username, mode, channel_id, user_id, now, result.state.value, now,
                p.followers if p else 0, p.following if p else 0, p.posts if p else 0,
                p.profile_pic_url if p else None,
                p.userid if p else None,
                p.bio if p else None,
                p.fullname if p else None,
                int(p.is_verified) if p else 0,
                p.external_url if p else None,
                check_interval,
                None,
                None,
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
            muted_until = None
            if r["muted_until"]:
                try:
                    muted_until = datetime.fromisoformat(r["muted_until"])
                except ValueError:
                    muted_until = None
            out.append(Monitor(
                username=r["username"],
                mode=r["mode"],
                channel_id=r["channel_id"],
                user_id=r["user_id"],
                started_at=datetime.fromisoformat(r["started_at"]),
                last_state=AccountState(r["last_state"]),
                followers=r["followers"],
                following=r["following"],
                posts=r["posts"],
                profile_pic_url=r["profile_pic_url"],
                userid=r["userid"],
                bio=r["bio"],
                fullname=r["fullname"],
                is_verified=bool(r["is_verified"]),
                external_url=r["external_url"],
                check_interval=r["check_interval"] or 0,
                muted_until=muted_until,
                alert_mention=r["alert_mention"],
                last_checked=datetime.fromisoformat(r["last_checked"]) if r["last_checked"] else None,
            ))
        return out

    def update_last_check(self, username, result):
        now = datetime.now(timezone.utc).isoformat()
        p = result.profile
        with self._connect() as con:
            con.execute("""
                UPDATE monitors
                SET last_state=?, last_checked=?, followers=?, following=?, posts=?,
                    profile_pic_url=?, userid=?, bio=?, fullname=?, is_verified=?, external_url=?
                WHERE username=?
            """, (
                result.state.value, now,
                p.followers if p else 0, p.following if p else 0, p.posts if p else 0,
                p.profile_pic_url if p else None,
                p.userid if p else None,
                p.bio if p else None,
                p.fullname if p else None,
                int(p.is_verified) if p else 0,
                p.external_url if p else None,
                username,
            ))

    def set_muted_until(self, username, muted_until):
        value = muted_until.isoformat() if muted_until else None
        with self._connect() as con:
            con.execute("UPDATE monitors SET muted_until=? WHERE username=?", (value, username))

    def set_alert_mention(self, username, mention):
        with self._connect() as con:
            cur = con.execute(
                "UPDATE monitors SET alert_mention=? WHERE username=?",
                (mention or None, username),
            )
            return cur.rowcount > 0

    def set_check_interval(self, username, interval):
        with self._connect() as con:
            cur = con.execute(
                "UPDATE monitors SET check_interval=? WHERE username=?",
                (interval, username),
            )
            return cur.rowcount > 0

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

    def get_all_history(self):
        with self._connect() as con:
            return con.execute("SELECT * FROM history ORDER BY id ASC").fetchall()

    def add_stats_history(self, username, profile):
        """Append a per-check snapshot for trend graphs. Compact: one row per check."""
        if not profile:
            return
        with self._connect() as con:
            con.execute("""
                INSERT INTO stats_history (username, checked_at, followers, following, posts)
                VALUES (?, ?, ?, ?, ?)
            """, (
                username, datetime.now(timezone.utc).isoformat(),
                profile.followers, profile.following, profile.posts,
            ))

    def get_stats_history(self, username, limit=60):
        with self._connect() as con:
            return con.execute(
                "SELECT * FROM stats_history WHERE username=? ORDER BY id DESC LIMIT ?",
                (username, limit),
            ).fetchall()

    def prune_stats_history(self, username, keep=500):
        """Keep the most recent `keep` rows per username to bound DB growth."""
        with self._connect() as con:
            con.execute(
                "DELETE FROM stats_history WHERE username=? AND id NOT IN "
                "(SELECT id FROM stats_history WHERE username=? ORDER BY id DESC LIMIT ?)",
                (username, username, keep),
            )

    def get_dashboard_stats(self):
        """Aggregate counts for the /stats dashboard."""
        with self._connect() as con:
            monitors = con.execute("SELECT COUNT(*) AS c FROM monitors").fetchone()["c"]
            ban = con.execute(
                "SELECT COUNT(*) AS c FROM monitors WHERE mode='ban'"
            ).fetchone()["c"]
            unban = con.execute(
                "SELECT COUNT(*) AS c FROM monitors WHERE mode='unban'"
            ).fetchone()["c"]
            transitions = con.execute("SELECT COUNT(*) AS c FROM history").fetchone()["c"]
            banned = con.execute(
                "SELECT COUNT(*) AS c FROM history WHERE event='banned'"
            ).fetchone()["c"]
            recovered = con.execute(
                "SELECT COUNT(*) AS c FROM history WHERE event='recovered'"
            ).fetchone()["c"]
            last_event = con.execute(
                "SELECT detected_at FROM history ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "monitors": monitors,
            "ban": ban,
            "unban": unban,
            "transitions": transitions,
            "banned": banned,
            "recovered": recovered,
            "last_event": last_event["detected_at"] if last_event else None,
        }
