"""
Redis-backed sliding-window rate limiter.

Used by anon-token (already wired in tokens.py via its own copy) and the
sensitive auth endpoints (signup, login, password-reset, voucher redeem).
Keys are namespaced by purpose so unrelated endpoints don't share buckets.

Fail-open on Redis errors: if Redis is down we'd rather accept the request
than block all logins. The downside is that an attacker could DoS Redis to
defeat the limiter; we accept that, the alternative (locking everyone out
when Redis flaps) is worse.
"""
from __future__ import annotations

import time

import redis
from fastapi import HTTPException
from ulid import ULID

from app.config import settings

_redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)


def check(bucket: str, key: str, *, limit: int, window_seconds: int, detail: str | None = None) -> None:
    """Increment the counter for (bucket, key) and 429 if it exceeds limit
    in the trailing `window_seconds`. Sliding window via a sorted set of
    timestamps; one ZSET entry per request."""
    rkey = f"rl:{bucket}:{key}"
    now = int(time.time())
    try:
        pipe = _redis.pipeline()
        pipe.zremrangebyscore(rkey, 0, now - window_seconds)
        pipe.zcard(rkey)
        pipe.zadd(rkey, {f"{now}:{ULID()}": now})
        pipe.expire(rkey, window_seconds)
        _, count, _, _ = pipe.execute()
    except redis.RedisError:
        return
    if count >= limit:
        raise HTTPException(status_code=429, detail=detail or "too many requests, try again later")
