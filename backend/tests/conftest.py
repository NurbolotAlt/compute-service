"""Общие фикстуры: НАСТОЯЩИЙ Redis в контейнере (Testcontainers), без fakeredis.

Мок Redis проверял бы наш код против нашего же представления о Redis. А вся
суть проекта — в семантике настоящих команд: атомарность SET NX, EXPIRE NX,
MULTI/EXEC, Lua-скриптов, реальное истечение TTL.
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from app import compute
from app.config import Settings
from app.main import create_app
from app.redis_client import create_client, create_pool

try:
    from testcontainers.community.redis import RedisContainer
except ImportError:  # старые версии testcontainers держали модуль в корне пакета
    from testcontainers.redis import RedisContainer


@pytest.fixture(scope="session")
def redis_container():
    # Один контейнер на всю сессию: старт занимает секунды, а изоляцию между
    # тестами даёт FLUSHDB в фикстуре redis ниже.
    with RedisContainer("redis:7-alpine") as container:
        yield container


@pytest.fixture(scope="session")
def redis_url(redis_container) -> str:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture
async def redis(redis_url):
    # Пул на каждый тест: у pytest-asyncio свой event loop на тест, а соединения
    # asyncio привязаны к loop, в котором созданы.
    pool = create_pool(Settings(redis_url=redis_url, redis_max_connections=20))
    client = create_client(pool)
    await client.flushdb()
    yield client
    await pool.disconnect()


@pytest.fixture
def settings(redis_url) -> Settings:
    # Короткие интервалы, чтобы тесты на ожидание лока шли за доли секунды.
    return Settings(
        redis_url=redis_url,
        lock_recheck_interval_seconds=0.05,
        lock_wait_timeout_seconds=5,
        lock_ttl_seconds=30,
    )


class GatedRunner:
    """Вычисление, которое "висит", пока тест не откроет gate.

    Нужен, чтобы детерминированно держать вычисление в работе (per-IP лимит,
    backpressure), не завися от скорости машины. Считает реальные вызовы.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.order: list[int] = []  # в каком порядке N реально пошли считаться
        self.gate = asyncio.Event()
        self._changed = asyncio.Condition()

    async def __call__(self, n: int) -> int:
        async with self._changed:
            self.calls += 1
            self.order.append(n)
            self._changed.notify_all()
        await self.gate.wait()
        return compute.fibonacci(n)

    async def wait_calls(self, expected: int) -> None:
        # Таймаут, чтобы сломанный код ронял тест, а не вешал его навсегда.
        async with asyncio.timeout(5), self._changed:
            await self._changed.wait_for(lambda: self.calls >= expected)


async def wait_pending(client, expected: int) -> None:
    """Ждёт, пока в воркере наберётся expected задач (считаются + ждут слот)."""
    # Опрос именно через HTTP, как это увидел бы внешний наблюдатель, — поэтому
    # цикл с паузой, а не asyncio.Event (события внутри приложения отсюда не видно).
    async with asyncio.timeout(5):
        while (await client.get("/api/debug/instance-info")).json()[  # noqa: ASYNC110
            "pending_computations"
        ] != expected:
            await asyncio.sleep(0.02)


@asynccontextmanager
async def running_app(settings: Settings, runner=None):
    """Приложение с настоящим lifespan (пул Redis, подписка, пул процессов)
    и httpx-клиент к нему без сети — через ASGI-транспорт."""
    app = create_app(settings, runner)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
