import asyncio
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import instaloader

class AccountState(str, Enum):
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"

@dataclass
class ProfileData:
    username: str
    followers: int = 0
    following: int = 0
    posts: int = 0
    profile_pic_url: Optional[str] = None
    userid: Optional[str] = None
    bio: Optional[str] = None
    fullname: Optional[str] = None
    is_verified: bool = False
    external_url: Optional[str] = None

    @property
    def followers_display(self):
        return compact_number(self.followers)

    @property
    def following_display(self):
        return compact_number(self.following)

def compact_number(value: int) -> str:
    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(value)

class InstagramChecker:
    def __init__(self):
        try:
            self.timeout = max(5, int(os.getenv("INSTAGRAM_CHECK_TIMEOUT_SECONDS", "25")))
        except ValueError:
            self.timeout = 25
        try:
            self.retry_count = max(0, int(os.getenv("INSTAGRAM_RETRY_COUNT", "2")))
        except ValueError:
            self.retry_count = 2
        try:
            self.rate_limit_backoff = max(10, int(os.getenv("INSTAGRAM_RATE_LIMIT_BACKOFF_SECONDS", "120")))
        except ValueError:
            self.rate_limit_backoff = 120
        # Monotonic cooldown deadline; 0.0 means "not currently rate-limited".
        self.rate_limited_until = 0.0
        self.loader = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
        )
        # Optional session support. Set INSTAGRAM_SESSIONID if Instagram requires a logged-in session.
        sessionid = os.getenv("INSTAGRAM_SESSIONID")
        if sessionid:
            try:
                self.loader.context._session.cookies.set("sessionid", sessionid, domain=".instagram.com")
            except Exception:
                pass
        try:
            self.loader.context.max_connection_attempts = 1
        except Exception:
            pass

    def _check_sync(self, username: str):
        try:
            profile = instaloader.Profile.from_username(self.loader.context, username)

            def safe_prop(getter, default=None):
                try:
                    val = getter()
                    return val if val is not None else default
                except Exception:
                    return default

            data = ProfileData(
                username=profile.username,
                followers=int(safe_prop(lambda: profile.followers, 0)),
                following=int(safe_prop(lambda: profile.followees, 0)),
                posts=int(safe_prop(lambda: profile.mediacount, 0)),
                profile_pic_url=safe_prop(lambda: profile.profile_pic_url, None),
                userid=str(safe_prop(lambda: profile.userid)) if safe_prop(lambda: profile.userid) is not None else None,
                bio=safe_prop(lambda: profile.biography, None),
                fullname=safe_prop(lambda: profile.full_name, None),
                is_verified=bool(safe_prop(lambda: profile.is_verified, False)),
                external_url=safe_prop(lambda: profile.external_url, None),
            )
            return AccountState.ACTIVE, data
        except instaloader.exceptions.ProfileNotExistsException:
            # This is a strong signal, but the monitor layer confirms it before alerting.
            return AccountState.UNAVAILABLE, None
        except (
            instaloader.exceptions.ConnectionException,
            instaloader.exceptions.QueryReturnedNotFoundException,
            instaloader.exceptions.BadResponseException,
            instaloader.exceptions.LoginRequiredException,
        ) as exc:
            # Detect Instagram rate-limiting (HTTP 429) so the bot can back off.
            msg = str(exc).lower()
            if "429" in msg or "too many requests" in msg or "rate limit" in msg:
                self.rate_limited_until = asyncio.get_event_loop().time() + self.rate_limit_backoff
            return AccountState.UNKNOWN, None
        except Exception:
            return AccountState.UNKNOWN, None

    def _check_by_userid_sync(self, userid: str):
        """Resolve a profile by its stable internal id. Used for rename detection:
        when a username returns UNAVAILABLE but the same user id is still active
        under a new handle, the account was renamed, not banned."""
        try:
            profile = instaloader.Profile.from_id(self.loader.context, int(userid))

            def safe_prop(getter, default=None):
                try:
                    val = getter()
                    return val if val is not None else default
                except Exception:
                    return default

            data = ProfileData(
                username=profile.username,
                followers=int(safe_prop(lambda: profile.followers, 0)),
                following=int(safe_prop(lambda: profile.followees, 0)),
                posts=int(safe_prop(lambda: profile.mediacount, 0)),
                profile_pic_url=safe_prop(lambda: profile.profile_pic_url, None),
                userid=str(safe_prop(lambda: profile.userid)) if safe_prop(lambda: profile.userid) is not None else None,
                bio=safe_prop(lambda: profile.biography, None),
                fullname=safe_prop(lambda: profile.full_name, None),
                is_verified=bool(safe_prop(lambda: profile.is_verified, False)),
                external_url=safe_prop(lambda: profile.external_url, None),
            )
            return AccountState.ACTIVE, data
        except Exception:
            return AccountState.UNKNOWN, None

    async def check_by_userid(self, userid: str):
        """Async wrapper around _check_by_userid_sync with the same timeout/retry
        semantics as check()."""
        if not userid:
            return CheckResult(state=AccountState.UNKNOWN, profile=None)
        last_result = None
        for attempt in range(self.retry_count + 1):
            try:
                state, profile = await asyncio.wait_for(
                    asyncio.to_thread(self._check_by_userid_sync, userid),
                    timeout=self.timeout,
                )
                last_result = state, profile
                if state != AccountState.UNKNOWN or attempt == self.retry_count:
                    break
            except asyncio.TimeoutError:
                last_result = AccountState.UNKNOWN, None
                if attempt == self.retry_count:
                    break
            if attempt < self.retry_count:
                await asyncio.sleep(min(2 ** attempt, 8))
        state, profile = last_result or (AccountState.UNKNOWN, None)
        return CheckResult(state=state, profile=profile)

    async def check(self, username: str):
        # Honor a global rate-limit cooldown: if Instagram recently returned 429,
        # skip checks for a short window instead of hammering and getting worse.
        if self.rate_limited_until:
            loop = asyncio.get_event_loop()
            if loop.time() < self.rate_limited_until:
                return CheckResult(state=AccountState.UNKNOWN, profile=None, rate_limited=True)
            self.rate_limited_until = 0.0

        last_result = None
        for attempt in range(self.retry_count + 1):
            try:
                state, profile = await asyncio.wait_for(
                    asyncio.to_thread(self._check_sync, username),
                    timeout=self.timeout,
                )
                last_result = state, profile
                if state != AccountState.UNKNOWN or attempt == self.retry_count:
                    break
            except asyncio.TimeoutError:
                last_result = AccountState.UNKNOWN, None
                if attempt == self.retry_count:
                    break
            if attempt < self.retry_count:
                await asyncio.sleep(min(2 ** attempt, 8))

        state, profile = last_result or (AccountState.UNKNOWN, None)
        return CheckResult(state=state, profile=profile)


@dataclass
class CheckResult:
    state: AccountState
    profile: Optional[ProfileData] = None
    rate_limited: bool = False
