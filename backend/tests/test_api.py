"""HTTP-уровень: валидация, лимиты, коды ответов. Redis — настоящий (Testcontainers)."""

import asyncio
import time

import pytest
from conftest import GatedRunner, running_app, wait_pending

from app.cache import input_hash
from app.distributed_lock import KEY_PREFIX as LOCK_PREFIX
from app.distributed_lock import RedisLock


def ip(addr: str) -> dict[str, str]:
    # Так nginx передаёт IP клиента; в тестах им же имитируем разных клиентов.
    return {"X-Real-IP": addr}


# --- Порог на N ---------------------------------------------------------------


async def test_input_cap(redis, settings):
    # Настоящий ProcessPoolExecutor: N = MAX реально считается в отдельном процессе.
    settings = settings.model_copy(update={"max_fibonacci_n": 20})
    async with running_app(settings) as client:
        too_big = await client.post("/api/compute", json={"input": 21}, headers=ip("10.0.0.1"))
        assert too_big.status_code == 422
        assert too_big.json()["detail"][0]["msg"] == "N не должно превышать 20"
        # Отказ случился ДО Redis: ни счётчика rate limit, ни кэша, ни лока.
        assert await redis.dbsize() == 0

        ok = await client.post("/api/compute", json={"input": 20}, headers=ip("10.0.0.1"))
        assert ok.status_code == 200
        assert ok.json()["result"] == 6765


@pytest.mark.parametrize("bad", [-1, "10", 10.5, True, None])
async def test_invalid_input_rejected(redis, settings, bad):
    async with running_app(settings, GatedRunner()) as client:
        response = await client.post("/api/compute", json={"input": bad})
        assert response.status_code == 422


async def test_json_without_content_type_is_accepted(redis, settings):
    # curl -d '{"input": 10}' без -H шлёт Content-Type: x-www-form-urlencoded.
    runner = GatedRunner()
    runner.gate.set()
    async with running_app(settings, runner) as client:
        response = await client.post(
            "/api/compute",
            content=b'{"input": 10}',
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        assert response.json()["result"] == 55


# --- Rate limit -----------------------------------------------------------------


async def test_rate_limit(redis, settings):
    runner = GatedRunner()
    runner.gate.set()
    async with running_app(settings, runner) as client:
        codes = [
            (await client.post("/api/compute", json={"input": 10}, headers=ip("10.0.0.2")))
            for _ in range(6)
        ]
        assert [r.status_code for r in codes] == [200] * 5 + [429]
        assert codes[-1].json()["reason"] == "rate_limit"
        assert int(codes[-1].headers["Retry-After"]) > 0


async def test_rate_limit_uses_x_real_ip(redis, settings):
    runner = GatedRunner()
    runner.gate.set()
    async with running_app(settings, runner) as client:
        for _ in range(5):
            await client.post("/api/compute", json={"input": 10}, headers=ip("10.0.0.3"))
        blocked = await client.post("/api/compute", json={"input": 10}, headers=ip("10.0.0.3"))
        other = await client.post("/api/compute", json={"input": 10}, headers=ip("10.0.0.4"))
        assert blocked.status_code == 429
        assert other.status_code == 200


# --- Защита от перегрузки ----------------------------------------------------------


async def test_per_ip_concurrency(redis, settings):
    runner = GatedRunner()
    async with running_app(settings, runner) as client:
        first = asyncio.create_task(
            client.post("/api/compute", json={"input": 20}, headers=ip("10.0.0.5"))
        )
        await runner.wait_calls(1)

        # Другой N — значит, другой лок: второй запрос не ждёт первый, а упирается
        # именно в лимит параллельных вычислений на IP.
        second = await client.post("/api/compute", json={"input": 19}, headers=ip("10.0.0.5"))
        assert second.status_code == 429
        assert second.json()["reason"] == "ip_busy"
        assert "Retry-After" in second.headers

        runner.gate.set()
        assert (await first).status_code == 200

        again = await client.post("/api/compute", json={"input": 19}, headers=ip("10.0.0.5"))
        assert again.status_code == 200


async def test_backpressure_releases_lock(redis, settings):
    # Пул 1, factor 2 -> "считается + ждёт" не больше 2 задач на воркер.
    settings = settings.model_copy(update={"compute_pool_size": 1, "backpressure_factor": 2})
    runner = GatedRunner()
    async with running_app(settings, runner) as client:
        busy = [
            asyncio.create_task(
                client.post("/api/compute", json={"input": n}, headers=ip(f"10.0.1.{n}"))
            )
            for n in (20, 21)
        ]
        await wait_pending(client, 2)  # одна считается, одна ждёт слот

        rejected = await client.post("/api/compute", json={"input": 22}, headers=ip("10.0.1.99"))
        assert rejected.status_code == 503
        assert rejected.json()["reason"] == "overloaded"
        assert "Retry-After" in rejected.headers

        # Главное: лок для N=22 снят сразу, а не висит до TTL (30 с в тестах).
        assert not await redis.exists(f"{LOCK_PREFIX}{input_hash(22)}")

        runner.gate.set()
        assert [(await t).status_code for t in busy] == [200, 200]

        started = time.monotonic()
        retry = await client.post("/api/compute", json={"input": 22}, headers=ip("10.0.1.99"))
        assert retry.status_code == 200
        assert time.monotonic() - started < 2


async def test_busy_server_queues_instead_of_503_in_fifo_order(redis, settings):
    # Пул 1: считается одна задача, остальные ЖДУТ слот на сервере, а не получают 503.
    settings = settings.model_copy(update={"compute_pool_size": 1, "backpressure_factor": 4})
    runner = GatedRunner()
    async with running_app(settings, runner) as client:
        tasks = []
        for i, n in enumerate((20, 21, 22)):
            tasks.append(
                asyncio.create_task(
                    client.post("/api/compute", json={"input": n}, headers=ip(f"10.0.2.{n}"))
                )
            )
            await wait_pending(client, i + 1)  # фиксируем порядок прихода

        info = (await client.get("/api/debug/instance-info")).json()
        assert (info["running"], info["waiting"]) == (1, 2)

        runner.gate.set()
        assert [(await t).status_code for t in tasks] == [200, 200, 200]
        # Слоты раздаются строго в порядке прихода.
        assert runner.order == [20, 21, 22]


async def test_queue_wait_timeout_returns_503_and_releases_lock(redis, settings):
    settings = settings.model_copy(
        update={"compute_pool_size": 1, "compute_queue_wait_seconds": 0.3}
    )
    runner = GatedRunner()
    async with running_app(settings, runner) as client:
        busy = asyncio.create_task(
            client.post("/api/compute", json={"input": 20}, headers=ip("10.0.3.1"))
        )
        await runner.wait_calls(1)

        started = time.monotonic()
        waited = await client.post("/api/compute", json={"input": 21}, headers=ip("10.0.3.2"))
        assert waited.status_code == 503
        assert waited.json()["reason"] == "queue_timeout"
        assert 0.25 < time.monotonic() - started < 2
        assert not await redis.exists(f"{LOCK_PREFIX}{input_hash(21)}")

        runner.gate.set()
        assert (await busy).status_code == 200
        info = (await client.get("/api/debug/instance-info")).json()
        assert info["pending_computations"] == info["running"] == info["waiting"] == 0


async def test_lock_wait_timeout_returns_504(redis, settings):
    settings = settings.model_copy(update={"lock_wait_timeout_seconds": 0.3})
    await RedisLock(redis, input_hash(13), ttl_seconds=30).acquire()
    async with running_app(settings, GatedRunner()) as client:
        response = await client.post("/api/compute", json={"input": 13})
        assert response.status_code == 504


# --- Event loop не блокируется ---------------------------------------------------------


async def test_event_loop_not_blocked_by_computation(redis, settings):
    # Настоящий пул процессов. Пока fibonacci(32) считается в другом процессе,
    # health отвечает — вычисление не держит event loop этого воркера.
    settings = settings.model_copy(update={"max_fibonacci_n": 32})
    async with running_app(settings) as client:
        heavy = asyncio.create_task(client.post("/api/compute", json={"input": 32}))
        await asyncio.sleep(0.05)

        health = await client.get("/api/health")
        assert health.status_code == 200
        assert not heavy.done()

        assert (await heavy).json()["result"] == 2178309


# --- Служебные эндпоинты -----------------------------------------------------------------


async def test_health(redis, settings):
    async with running_app(settings, GatedRunner()) as client:
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["redis"] == "ok"


async def test_limits_follow_settings(redis, settings):
    settings = settings.model_copy(update={"max_fibonacci_n": 38})
    async with running_app(settings, GatedRunner()) as client:
        assert (await client.get("/api/limits")).json()["max_fibonacci_n"] == 38


async def test_instance_info(redis, settings):
    async with running_app(settings, GatedRunner()) as client:
        data = (await client.get("/api/debug/instance-info")).json()
        assert data["hostname"]
        assert data["pending_computations"] == 0
