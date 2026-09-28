"""Поведение пула Redis под нагрузкой и при недоступном Redis."""

import asyncio

import pytest
from conftest import GatedRunner, running_app
from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import Settings
from app.redis_client import create_client, create_pool


async def test_pool_waits_for_free_connection_instead_of_failing(redis_url):
    # 20 команд, каждая держит соединение ~0.1 с, а соединений в пуле всего 2.
    # Обычный ConnectionPool упал бы с MaxConnectionsError; блокирующий — ставит
    # команды в очередь за соединением.
    pool = create_pool(Settings(redis_url=redis_url, redis_max_connections=2))
    client = create_client(pool)
    try:
        results = await asyncio.gather(
            *(client.blpop([f"empty-{i}"], timeout=0.1) for i in range(20))
        )
        assert results == [None] * 20
    finally:
        await pool.disconnect()


async def test_pool_gives_up_after_pool_timeout(redis_url):
    pool = create_pool(
        Settings(redis_url=redis_url, redis_max_connections=1, redis_pool_timeout_seconds=0.2)
    )
    client = create_client(pool)
    try:
        holder = asyncio.create_task(client.blpop(["empty"], timeout=1))
        await asyncio.sleep(0.05)
        with pytest.raises(RedisConnectionError):
            await client.get("x")
        await holder
    finally:
        await pool.disconnect()


async def test_redis_down_returns_503_not_500():
    # Порт, на котором никого нет: Redis "лежит". Приложение должно стартовать
    # и отвечать 503 с причиной, а не 500 и не падать при старте.
    settings = Settings(
        redis_url="redis://127.0.0.1:1/0",
        redis_connect_timeout_seconds=0.2,
        redis_socket_timeout_seconds=0.2,
    )
    async with running_app(settings, GatedRunner()) as client:
        compute = await client.post("/api/compute", json={"input": 10})
        assert compute.status_code == 503
        assert compute.json()["reason"] == "redis_unavailable"
        assert "Retry-After" in compute.headers

        assert (await client.get("/api/health")).status_code == 503
