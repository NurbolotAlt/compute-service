"""Кэш результатов в Redis.

fibonacci(n) детерминирована: один и тот же вход всегда даёт тот же выход, поэтому
результат можно отдавать из кэша без риска устаревания.
"""

import hashlib
import json
from typing import Any

from redis.asyncio import Redis

KEY_PREFIX = "compute:cache:"


def input_hash(n: int) -> str:
    # НЕ встроенный hash(): для строк он рандомизирован per-process (PYTHONHASHSEED),
    # и у каждого воркера/реплики был бы свой ключ для одного и того же входа —
    # общий кэш бы просто не работал. sha256 одинаков везде.
    # Префикс задачи в хэшируемой строке — чтобы при появлении второй задачи
    # её ключи не пересеклись с фибоначчи.
    return hashlib.sha256(f"fibonacci:{n}".encode()).hexdigest()


def cache_key(n: int) -> str:
    return f"{KEY_PREFIX}{input_hash(n)}"


class ResultCache:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def get(self, n: int) -> dict[str, Any] | None:
        raw = await self._redis.get(cache_key(n))
        return json.loads(raw) if raw is not None else None

    async def set(self, n: int, payload: dict[str, Any]) -> None:
        # Храним JSON, а не голое число: вместе с результатом лежат метаданные
        # (кто и сколько считал) — полезно в ленте и при разборе инцидентов.
        # Большие числа JSON хранит без потери точности (в отличие от float).
        await self._redis.set(cache_key(n), json.dumps(payload), ex=self._ttl)
