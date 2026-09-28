import asyncio

import pytest

from app import compute
from app.cache import input_hash
from app.compute_service import ComputeService, LockWaitTimeout
from app.distributed_lock import RedisLock


class CountingRunner:
    """Подменяет пул процессов: считает РЕАЛЬНЫЕ вызовы fibonacci.

    Настоящий ProcessPoolExecutor тут не подходит — счётчик в дочернем процессе
    не виден родителю. Пауза имитирует долгое вычисление, чтобы запросы гарантированно
    пересеклись во времени.
    """

    def __init__(self, delay: float = 0.5, fail: bool = False) -> None:
        self.calls = 0
        self.delay = delay
        self.fail = fail

    async def __call__(self, n: int) -> int:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("вычисление упало")
        return compute.fibonacci(n)


async def test_cache_stampede(redis, settings):
    runner = CountingRunner()
    service = ComputeService(redis, settings, runner)

    results = await asyncio.gather(*(service.get_or_compute(25) for _ in range(5)))

    assert runner.calls == 1
    assert {r.result for r in results} == {75025}
    assert sorted(r.from_cache for r in results) == [False] + [True] * 4


async def test_stampede_across_service_instances(redis, settings):
    # Имитация нескольких реплик: отдельные сервисы, общий только Redis.
    runner = CountingRunner()
    services = [ComputeService(redis, settings, runner) for _ in range(5)]

    await asyncio.gather(*(s.get_or_compute(20) for s in services))

    assert runner.calls == 1


async def test_second_call_is_served_from_cache(redis, settings):
    runner = CountingRunner(delay=0)
    service = ComputeService(redis, settings, runner)

    first = await service.get_or_compute(15)
    second = await service.get_or_compute(15)

    assert not first.from_cache
    assert second.from_cache
    assert second.result == first.result == 610
    assert runner.calls == 1


async def test_lock_released_when_compute_fails(redis, settings):
    service = ComputeService(redis, settings, CountingRunner(delay=0, fail=True))

    with pytest.raises(RuntimeError):
        await service.get_or_compute(10)

    assert not await RedisLock(redis, input_hash(10), 30).is_locked()


async def test_waiter_takes_over_when_holder_vanishes(redis, settings):
    # Держатель исчез без результата — ждущий не ждёт таймаута, а считает сам.
    foreign = RedisLock(redis, input_hash(12), ttl_seconds=30)
    await foreign.acquire()

    runner = CountingRunner(delay=0)
    task = asyncio.create_task(ComputeService(redis, settings, runner).get_or_compute(12))
    await asyncio.sleep(0.2)
    await foreign.release()

    result = await asyncio.wait_for(task, timeout=2)
    assert result.result == 144
    assert not result.from_cache
    assert runner.calls == 1


async def test_lock_wait_timeout(redis, settings):
    settings = settings.model_copy(update={"lock_wait_timeout_seconds": 0.3})
    await RedisLock(redis, input_hash(11), ttl_seconds=30).acquire()

    with pytest.raises(LockWaitTimeout):
        await ComputeService(redis, settings, CountingRunner()).get_or_compute(11)
