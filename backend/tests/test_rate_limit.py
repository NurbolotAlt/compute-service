import asyncio

from app.rate_limit import KEY_PREFIX, check_rate_limit


async def test_rate_limit_sixth_request_is_rejected(redis):
    results = [
        await check_rate_limit(redis, "1.2.3.4", limit=5, window_seconds=60) for _ in range(6)
    ]

    assert [r.allowed for r in results] == [True] * 5 + [False]
    assert results[-1].retry_after > 0


async def test_rate_limit_is_per_ip(redis):
    for _ in range(5):
        await check_rate_limit(redis, "1.1.1.1", limit=5, window_seconds=60)

    assert not (await check_rate_limit(redis, "1.1.1.1", limit=5, window_seconds=60)).allowed
    assert (await check_rate_limit(redis, "2.2.2.2", limit=5, window_seconds=60)).allowed


async def test_rate_limit_key_always_has_ttl(redis):
    # Ключ без TTL = вечная блокировка IP. Проверяем, что EXPIRE реально ставится.
    await check_rate_limit(redis, "3.3.3.3", limit=5, window_seconds=60)
    assert 0 < await redis.ttl(f"{KEY_PREFIX}3.3.3.3") <= 60


async def test_rate_limit_window_does_not_slide(redis):
    # EXPIRE NX: повторные запросы не продлевают окно.
    await check_rate_limit(redis, "4.4.4.4", limit=5, window_seconds=60)
    await redis.expire(f"{KEY_PREFIX}4.4.4.4", 10)
    await check_rate_limit(redis, "4.4.4.4", limit=5, window_seconds=60)
    assert await redis.ttl(f"{KEY_PREFIX}4.4.4.4") <= 10


async def test_rate_limit_resets_after_window(redis):
    for _ in range(2):
        await check_rate_limit(redis, "5.5.5.5", limit=1, window_seconds=1)
    await asyncio.sleep(1.2)

    assert (await check_rate_limit(redis, "5.5.5.5", limit=1, window_seconds=1)).allowed
