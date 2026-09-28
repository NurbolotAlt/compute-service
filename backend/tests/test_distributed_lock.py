import asyncio

from app.distributed_lock import RedisLock


async def test_only_one_holder(redis):
    first = RedisLock(redis, "x", ttl_seconds=30)
    second = RedisLock(redis, "x", ttl_seconds=30)

    assert await first.acquire()
    assert not await second.acquire()

    assert await first.release()
    assert await second.acquire()


async def test_concurrent_acquire_exactly_one_wins(redis):
    locks = [RedisLock(redis, "race", ttl_seconds=30) for _ in range(20)]
    results = await asyncio.gather(*(lock.acquire() for lock in locks))
    assert sum(results) == 1


async def test_lock_has_ttl(redis):
    lock = RedisLock(redis, "ttl", ttl_seconds=30)
    await lock.acquire()
    assert 0 < await redis.ttl(lock.key) <= 30


async def test_lock_expires_if_holder_dies(redis):
    # Держатель "упал" и не снял лок — TTL освобождает его сам.
    await RedisLock(redis, "dead", ttl_seconds=1).acquire()
    await asyncio.sleep(1.3)
    assert await RedisLock(redis, "dead", ttl_seconds=30).acquire()


async def test_release_does_not_delete_foreign_lock(redis):
    # Наш лок истёк, его взял другой. Наш release НЕ должен удалить чужой лок.
    ours = RedisLock(redis, "foreign", ttl_seconds=1)
    await ours.acquire()
    await asyncio.sleep(1.3)

    theirs = RedisLock(redis, "foreign", ttl_seconds=30)
    assert await theirs.acquire()

    assert not await ours.release()
    assert await theirs.is_locked()
