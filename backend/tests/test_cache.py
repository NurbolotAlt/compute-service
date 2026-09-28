import asyncio

from app.cache import ResultCache, cache_key


async def test_cache_roundtrip(redis):
    cache = ResultCache(redis, ttl_seconds=3600)
    assert await cache.get(10) is None

    await cache.set(10, {"result": 55})
    assert await cache.get(10) == {"result": 55}
    assert 3590 < await redis.ttl(cache_key(10)) <= 3600


async def test_cache_key_is_deterministic():
    # Ключ должен совпадать во всех процессах/репликах — поэтому sha256, а не hash().
    assert cache_key(35) == cache_key(35)
    assert cache_key(35) != cache_key(34)


async def test_cache_ttl(redis):
    # TTL сокращён "через конфиг" — тот же параметр, что cache_ttl_seconds в Settings.
    cache = ResultCache(redis, ttl_seconds=1)
    await cache.set(20, {"result": 6765})
    assert await cache.get(20) is not None

    await asyncio.sleep(1.5)
    assert await cache.get(20) is None
