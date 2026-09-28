"""Rate limit в Redis: fixed window через INCR + EXPIRE.

Счётчик живёт в Redis, а не в памяти процесса: реплик и воркеров несколько, и
лимит "5 в минуту" должен быть ОБЩИМ для всех, иначе реальный лимит был бы
5 × (реплики × воркеры).
"""

from dataclasses import dataclass

from redis.asyncio import Redis

KEY_PREFIX = "ratelimit:compute:"


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    current: int
    limit: int
    retry_after: int  # секунд до сброса окна (для заголовка Retry-After)


async def check_rate_limit(
    redis: Redis, ip: str, limit: int, window_seconds: int
) -> RateLimitResult:
    key = f"{KEY_PREFIX}{ip}"
    # INCR и EXPIRE — в одной транзакции MULTI/EXEC. Если выполнить их отдельными
    # командами и процесс умрёт между ними, ключ останется без TTL навсегда, и
    # этот IP будет заблокирован до ручной чистки Redis.
    # EXPIRE ... NX ставит TTL только если его ещё нет: окно отсчитывается от
    # ПЕРВОГО запроса, а не продлевается каждым следующим (иначе клиент, стучащий
    # раз в 30 с, не разблокировался бы никогда).
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        pipe.ttl(key)
        current, _, ttl = await pipe.execute()

    return RateLimitResult(
        allowed=current <= limit,
        current=current,
        limit=limit,
        retry_after=max(ttl, 1),
    )


# ---------------------------------------------------------------------------
# Сколько вычислений с IP идёт ПРЯМО СЕЙЧАС (в отличие от rate limit выше, который
# считает запросы за минуту). Счётчик в Redis, потому что вычисления одного клиента
# nginx может раскидать по разным репликам.
# ---------------------------------------------------------------------------

CONCURRENCY_PREFIX = "concurrency:compute:"

# DECR и удаление ключа на нуле — одним атомарным скриптом. Если счётчик успел
# истечь по TTL посреди вычисления, голый DECR создал бы ключ со значением -1
# (без TTL!), и следующий клиент получил бы право на ДВА вычисления сразу.
_RELEASE_SLOT_SCRIPT = """
local value = redis.call('decr', KEYS[1])
if value <= 0 then
    redis.call('del', KEYS[1])
end
return value
"""


class ConcurrencySlot:
    def __init__(self, redis: Redis, ip: str, limit: int, ttl_seconds: int) -> None:
        self._redis = redis
        self._key = f"{CONCURRENCY_PREFIX}{ip}"
        self._limit = limit
        # TTL — страховка на случай, если процесс умрёт между INCR и DECR (SIGKILL,
        # OOM): иначе IP навсегда остался бы "занятым". Больше худшего вычисления.
        self._ttl = ttl_seconds

    async def acquire(self) -> bool:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(self._key)
            pipe.expire(self._key, self._ttl)
            current, _ = await pipe.execute()
        if current > self._limit:
            # Откатываем свой INCR: отказанный запрос не должен занимать слот.
            await self._release()
            return False
        return True

    async def release(self) -> None:
        await self._release()

    async def _release(self) -> None:
        await self._redis.eval(_RELEASE_SLOT_SCRIPT, 1, self._key)
