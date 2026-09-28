"""Distributed lock на Redis: SET key token NX EX ttl.

Зачем: защита от cache stampede. Реплик и воркеров несколько, у каждого своя
память, поэтому asyncio.Lock или threading.Lock тут бесполезны — они видят только
свой процесс. Общая точка для всех — Redis, и лок живёт там.

Почему именно так:
- NX  — атомарно "создай, только если ключа нет": из N одновременных SET ровно
        один получит OK. Отдельные GET + SET дали бы гонку (оба увидели "нет").
- EX  — TTL как страховка: если держатель упадёт (OOM, SIGKILL), лок сам истечёт,
        а не заблокирует этот вход навсегда. Поэтому TTL обязан быть в разы больше
        худшего времени вычисления — иначе лок истечёт посреди работы.
- token — случайное значение держателя. Снимать лок можно только своим токеном:
        если наш лок успел истечь и его взял другой запрос, простой DEL удалил бы
        ЧУЖОЙ лок. Проверка "мой ли токен" + DEL делается Lua-скриптом, потому что
        Redis выполняет скрипт атомарно — между GET и DEL никто не вклинится.

Это лок на одном инстансе Redis (не Redlock на кластере): для защиты от
дублирующей работы этого достаточно — в худшем случае посчитаем дважды,
корректность результата от лока не зависит.
"""

import asyncio
import secrets
import weakref

from redis.asyncio import Redis

KEY_PREFIX = "compute:lock:"
# Канал, в который держатель публикует имя лока сразу после его снятия.
LOCK_RELEASED_CHANNEL = "compute:lock-released"

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class RedisLock:
    def __init__(self, redis: Redis, name: str, ttl_seconds: int) -> None:
        self._redis = redis
        self.key = f"{KEY_PREFIX}{name}"
        self._ttl = ttl_seconds
        self._token = secrets.token_hex(16)
        self._owned = False

    async def acquire(self) -> bool:
        """Одна неблокирующая попытка. Ожиданием управляет вызывающий код:
        пока ждём, ему нужно ещё и проверять кэш — результат важнее самого лока."""
        self._owned = bool(await self._redis.set(self.key, self._token, nx=True, ex=self._ttl))
        return self._owned

    async def release(self) -> bool:
        """True — сняли свой лок; False — его уже нет или он чужой (истёк TTL)."""
        if not self._owned:
            return False
        self._owned = False
        return bool(await self._redis.eval(_RELEASE_SCRIPT, 1, self.key, self._token))

    async def is_locked(self) -> bool:
        return bool(await self._redis.exists(self.key))


# ---------------------------------------------------------------------------
# Уведомления о снятии лока — вместо опроса "готово? готово? готово?".
#
# При опросе каждые 200 мс 1000 ждущих запросов делают тысячи команд Redis в
# секунду ради одного события, и результат приходит с задержкой до интервала.
# Вместо этого держатель после снятия лока делает PUBLISH, а в каждом воркере
# ОДНА подписка (pubsub.py) будит всех местных ждущих этого лока через
# asyncio.Event. Ждущие одного N в одном воркере делят одно событие.
# ---------------------------------------------------------------------------


async def publish_lock_released(redis: Redis, name: str) -> None:
    await redis.publish(LOCK_RELEASED_CHANNEL, name)


class LockReleaseNotifier:
    """Реестр ждущих в пределах ОДНОГО воркера: имя лока -> asyncio.Event."""

    def __init__(self) -> None:
        # WeakValueDictionary: событие живёт, пока его кто-то ждёт. Если держатель
        # умер и уведомления не будет, запись исчезнет вместе с последним ждущим,
        # а не останется в памяти навсегда.
        self._events: weakref.WeakValueDictionary[str, asyncio.Event] = (
            weakref.WeakValueDictionary()
        )

    def watch(self, name: str) -> asyncio.Event:
        event = self._events.get(name)
        if event is None or event.is_set():
            event = asyncio.Event()
            self._events[name] = event
        return event

    def notify(self, name: str) -> None:
        # Вызывается из обработчика подписки — синхронно и мгновенно, чтобы не
        # задерживать разбор следующих сообщений.
        event = self._events.pop(name, None)
        if event is not None:
            event.set()
