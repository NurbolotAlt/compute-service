"""Redis Pub/Sub: живая лента результатов и уведомления о снятии локов.

Проблема: вычисление прошло на реплике A, а зритель подключён по WebSocket к
реплике B. Память у них разная, напрямую A про сокеты B ничего не знает.
Решение: A публикует результат в канал Redis, а КАЖДЫЙ воркер каждой реплики
подписан на этот канал и рассылает сообщение своим локальным WebSocket-клиентам.
Так же расходятся и уведомления "лок снят" — ждущим этого лока в любом воркере.

Подписка одна на воркер (на все каналы сразу), а не на каждого зрителя или
ждущего: 1000 зрителей = 1000 соединений с Redis, если подписываться per-client.
А так число подписок = число воркеров, а раздачу внутри воркера делают
ConnectionManager и LockReleaseNotifier в памяти.

Pub/Sub — fire-and-forget: кто не подписан в момент PUBLISH, сообщение не получит
и истории нет. Для живой ленты это нормально; ожидание лока подстраховано
периодической перепроверкой (compute_service.py).
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

RESULTS_CHANNEL = "compute:results"
RECONNECT_DELAY_SECONDS = 1.0

# Обработчик сообщения канала. СИНХРОННЫЙ и быстрый: сообщения разбираются по
# одному, и медленный обработчик (например, рассылка по сотне WebSocket) задержал
# бы уведомления о локах. Долгую работу обработчик запускает отдельной задачей.
Handler = Callable[[str], None]


async def publish_result(redis: Redis, payload: dict[str, Any]) -> int:
    """Возвращает число подписчиков (воркеров), получивших сообщение."""
    return await redis.publish(RESULTS_CHANNEL, json.dumps(payload))


class RedisSubscriber:
    def __init__(self, redis: Redis, handlers: dict[str, Handler]) -> None:
        self._redis = redis
        self._handlers = handlers
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        # Первую подписку делаем сразу, чтобы к приёму запросов воркер уже
        # слушал каналы. Но если Redis недоступен, воркер всё равно стартует:
        # /api/health честно ответит 503, а подписка восстановится в фоне.
        try:
            pubsub = await self._subscribe()
        except (RedisError, OSError):
            log.warning("pubsub_initial_subscribe_failed", exc_info=True)
            pubsub = None
        self._task = asyncio.create_task(self._run(pubsub), name="redis-subscriber")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _subscribe(self):
        # PubSub забирает себе ОТДЕЛЬНОЕ соединение из пула: в режиме подписки
        # соединение умеет только принимать сообщения, обычные команды по нему не идут.
        pubsub = self._redis.pubsub(ignore_subscribe_messages=False)
        await pubsub.subscribe(*self._handlers)
        # Ждём подтверждения по каждому каналу: до него PUBLISH с других реплик мог
        # бы пролететь мимо (Pub/Sub не хранит сообщения).
        confirmed: set[str] = set()
        for _ in range(10):
            message = await pubsub.get_message(timeout=1.0)
            if message is not None and message["type"] == "subscribe":
                confirmed.add(message["channel"])
                if confirmed == set(self._handlers):
                    break
        else:
            await pubsub.aclose()
            raise RedisError("Нет подтверждения подписки на каналы")
        log.info("pubsub_subscribed", extra={"channels": sorted(confirmed)})
        return pubsub

    async def _run(self, pubsub) -> None:
        # Внешний цикл — переподключение: рестарт Redis не должен навсегда
        # оставить воркер без ленты (иначе лечится только рестартом backend).
        while True:
            if pubsub is None:
                pubsub = await self._resubscribe()
            try:
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        self._dispatch(message["channel"], message["data"])
            except asyncio.CancelledError:
                await pubsub.aclose()
                raise
            except (RedisError, OSError):
                log.warning("pubsub_connection_lost", exc_info=True)
            await pubsub.aclose()
            pubsub = None

    async def _resubscribe(self):
        while True:
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
            try:
                return await self._subscribe()
            except (RedisError, OSError):
                log.warning("pubsub_reconnect_failed")

    def _dispatch(self, channel: str, data: str) -> None:
        try:
            self._handlers[channel](data)
        except Exception:
            # Ошибка в одном обработчике не должна убивать подписку целиком.
            log.exception("pubsub_handler_failed", extra={"channel": channel})
