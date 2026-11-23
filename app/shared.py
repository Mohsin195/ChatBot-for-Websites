"""Shared Vercel crawl state via the Redis REST API."""
import base64
import json
import os
import zlib

import httpx

from . import config

URL = (os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL", "")).rstrip("/")
TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN", "")
enabled = config.ON_VERCEL and bool(URL and TOKEN)
TTL = config.SITE_TTL_MINUTES * 60
PREFIX = f"sitesense:{os.getenv('VERCEL_ENV', 'local')}:"


async def command(*args):
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(URL, json=args, headers={"Authorization": f"Bearer {TOKEN}"})
    if not response.is_success:
        raise RuntimeError(f"Shared storage returned HTTP {response.status_code}")
    data = response.json()
    if "error" in data:
        raise RuntimeError("Shared storage rejected a command")
    return data["result"]


def site_key(site_id):
    return PREFIX + "site:" + site_id


def session_key(session):
    return PREFIX + "session:" + session


async def save_site(site_id, site):
    await command(
        "EVAL", "redis.call('HSET', KEYS[1], 'site', ARGV[1]); return redis.call('EXPIRE', KEYS[1], ARGV[2])",
        1, site_key(site_id), json.dumps(site), TTL + 60,
    )


async def save_batch(site_id, number, batch):
    encoded = json.dumps(batch, separators=(",", ":")).encode()
    payload = base64.b64encode(zlib.compress(encoded, level=1)).decode()
    await command(
        "EVAL", "redis.call('HSET', KEYS[1], ARGV[1], ARGV[2]); return redis.call('EXPIRE', KEYS[1], ARGV[3])",
        1, site_key(site_id), str(number), payload, TTL + 60,
    )


async def load_rows(site_id, batch_count):
    rows = []
    for start in range(0, batch_count, 8):
        numbers = range(start, min(start + 8, batch_count))
        batches = await command("HMGET", site_key(site_id), *map(str, numbers))
        if any(batch is None for batch in batches):
            raise RuntimeError("Shared search index is incomplete")
        for batch in batches:
            rows.extend(json.loads(zlib.decompress(base64.b64decode(batch))))
    return rows


async def load_site(session):
    result = await command(
        "EVAL",
        """
        local id = redis.call('GET', KEYS[1])
        if not id then return nil end
        local key = ARGV[1] .. id
        local site = redis.call('HGET', key, 'site')
        if not site then return nil end
        redis.call('EXPIRE', key, ARGV[3])
        redis.call('EXPIRE', KEYS[1], ARGV[2])
        return site
        """,
        1, session_key(session), site_key(""), TTL, TTL + 60,
    )
    return json.loads(result) if result else None
