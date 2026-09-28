"""Pub/Sub: лента и уведомления о локах доходят до воркеров всех "реплик"."""

import asyncio
import time

from fastapi.testclient import TestClient

from app import compute
from app.compute_service import ComputeService
from app.distributed_lock import LOCK_RELEASED_CHANNEL, LockReleaseNotifier
from app.main import create_app
from app.pubsub import RESULTS_CHANNEL, RedisSubscriber, publish_result


class Collector:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.received = asyncio.Event()

    def __call__(self, message: str) -> None:
        self.messages.append(message)
        self.received.set()


async def test_publish_reaches_every_subscribed_worker(redis):
    # Два подписчика = два воркера/реплики, общий у них только Redis.
    collectors = [Collector(), Collector()]
    subscribers = [RedisSubscriber(redis, {RESULTS_CHANNEL: c}) for c in collectors]
    for s in subscribers:
        await s.start()
    try:
        delivered = await publish_result(redis, {"input": 10, "result": 55})
        assert delivered == 2
        async with asyncio.timeout(3):
            for c in collectors:
                await c.received.wait()
        assert all('"result": 55' in c.messages[0] for c in collectors)
    finally:
        for s in subscribers:
            await s.stop()


async def test_waiter_wakes_on_release_notification(redis, settings):
    # Перепроверка раз в 10 с: если ждущий вернётся заметно раньше, его разбудило
    # именно уведомление о снятии лока с другой "реплики", а не опрос.
    settings = settings.model_copy(update={"lock_recheck_interval_seconds": 10})

    async def slow_runner(n: int) -> int:
        await asyncio.sleep(0.3)
        return compute.fibonacci(n)

    replicas = []
    for _ in range(2):
        notifier = LockReleaseNotifier()
        subscriber = RedisSubscriber(redis, {LOCK_RELEASED_CHANNEL: notifier.notify})
        await subscriber.start()
        replicas.append((ComputeService(redis, settings, slow_runner, notifier), subscriber))
    try:
        started = time.monotonic()
        holder = asyncio.create_task(replicas[0][0].get_or_compute(25, "10.1.1.1"))
        await asyncio.sleep(0.05)  # holder гарантированно взял лок
        waiter = await replicas[1][0].get_or_compute(25, "10.1.1.2")

        assert waiter.from_cache
        assert time.monotonic() - started < 2
        assert not (await holder).from_cache
    finally:
        for _, subscriber in replicas:
            await subscriber.stop()


async def instant_runner(n: int) -> int:
    return n


def test_websocket_feed_across_replicas(settings):
    # Вычисление на "реплике" A, зритель подключён по WebSocket к "реплике" B.
    # Каждая TestClient поднимает своё приложение со своим event loop и lifespan.
    replica_a = create_app(settings, instant_runner)
    replica_b = create_app(settings, instant_runner)
    with (
        TestClient(replica_a) as a,
        TestClient(replica_b) as b,
        b.websocket_connect("/ws") as ws,
    ):
        response = a.post("/api/compute", json={"input": 7}, headers={"X-Real-IP": "10.9.9.9"})
        assert response.status_code == 200

        message = ws.receive_json()
        assert message["input"] == 7
        assert "from_cache" in message
