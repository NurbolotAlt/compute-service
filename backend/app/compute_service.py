"""Сценарий одного вычисления: кэш -> distributed lock -> вычисление или ожидание.

Отделено от main.py, чтобы логику лока можно было тестировать без HTTP и без
пула процессов: способ вычисления (runner) передаётся снаружи.
"""

import asyncio
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.cache import ResultCache, input_hash
from app.config import Settings
from app.distributed_lock import LockReleaseNotifier, RedisLock, publish_lock_released
from app.rate_limit import ConcurrencySlot

log = logging.getLogger(__name__)

# Асинхронная функция "посчитай n". В проде — run_in_executor с ProcessPoolExecutor,
# в тестах — счётчик вызовов.
Runner = Callable[[int], Awaitable[int]]

HOSTNAME = socket.gethostname()


class LockWaitTimeout(Exception):
    """Чужой лок держится дольше lock_wait_timeout_seconds -> 504."""


class TooManyConcurrent(Exception):
    """С этого IP уже идёт вычисление -> 429."""


class Overloaded(Exception):
    """Нет места в очереди ожидания или не дождались слота -> 503 + Retry-After."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason  # "overloaded" | "queue_timeout" — фронтенду для текста


@dataclass(frozen=True)
class ComputeResult:
    input: int
    result: int
    from_cache: bool
    duration_ms: float  # время САМОГО вычисления (из кэша — сколько считал тот, кто считал)
    computed_by: str  # hostname реплики, которая реально посчитала

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "result": self.result,
            "from_cache": self.from_cache,
            "duration_ms": self.duration_ms,
            "computed_by": self.computed_by,
        }


class ComputeService:
    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        runner: Runner,
        notifier: LockReleaseNotifier | None = None,
    ) -> None:
        self._redis = redis
        self._settings = settings
        self._runner = runner
        # Без подписки (notifier никто не кормит) ждущие просыпаются только по
        # периодической перепроверке — медленнее, но корректно.
        self._notifier = notifier or LockReleaseNotifier()
        self._cache = ResultCache(redis, settings.cache_ttl_seconds)
        # Слоты пула: в ProcessPoolExecutor одновременно отдаётся не больше задач,
        # чем в нём процессов. Остальные ждут на семафоре — это и есть очередь
        # ожидания. asyncio.Semaphore будит ждущих строго по порядку прихода (FIFO),
        # поэтому очередь честная. А внутренняя очередь пула остаётся пустой: в неё
        # нельзя заглянуть, её нельзя ограничить и из неё нельзя уйти по таймауту.
        self._slots = asyncio.Semaphore(settings.compute_pool_size)
        # Задачи "ждут слот + считаются" в ЭТОМ воркере. Обычные int без блокировок:
        # все корутины воркера живут в одном потоке event loop, а между проверкой и
        # инкрементом нет await — переключиться посередине нельзя.
        self._pending = 0
        self._running = 0

    @property
    def pending(self) -> int:
        return self._pending

    @property
    def running(self) -> int:
        return self._running

    @property
    def waiting(self) -> int:
        return self._pending - self._running

    async def get_or_compute(self, n: int, client_ip: str = "unknown") -> ComputeResult:
        ctx = {"input": n, "client_ip": client_ip}
        cached = await self._from_cache(n)
        if cached is not None:
            log.info("cache_hit", extra=ctx)
            return cached

        deadline = time.monotonic() + self._settings.lock_wait_timeout_seconds
        # Цикл, а не одна попытка: если держатель лока исчез, не положив результат
        # (упал, получил 503 от backpressure), ждущий сам становится вычисляющим,
        # а не досиживает до таймаута.
        name = input_hash(n)
        while True:
            # Подписываемся на снятие лока ДО попытки его взять: если держатель
            # снимет лок между нашим "занято" и началом ожидания, уведомление всё
            # равно не потеряется — событие уже зарегистрировано.
            released = self._notifier.watch(name)
            lock = RedisLock(self._redis, name, self._settings.lock_ttl_seconds)
            if await lock.acquire():
                log.info("lock_acquired", extra=ctx)
                try:
                    return await self._compute_locked(n, client_ip)
                finally:
                    # finally: даже если вычисление упало или получило отказ
                    # (429/503), лок снимаем сразу и будим ждущих, чтобы следующий
                    # запрос с тем же N не ждал истечения TTL.
                    if await lock.release():
                        await self._announce_release(name, ctx)
                    log.info("lock_released", extra=ctx)

            log.info("lock_wait", extra=ctx)
            result = await self._wait_for_result(n, lock, name, released, deadline)
            if result is not None:
                return result
            # Лок пропал без результата — идём на новую попытку его взять.

    async def _compute_locked(self, n: int, client_ip: str) -> ComputeResult:
        ctx = {"input": n, "client_ip": client_ip}
        # Повторная проверка кэша ПОСЛЕ взятия лока (double-checked locking).
        # Гонка без неё: A считает, B промахнулся мимо кэша; A кладёт результат и
        # отпускает лок; B берёт свободный лок — и считал бы всё заново.
        cached = await self._from_cache(n)
        if cached is not None:
            log.info("cache_hit_after_lock", extra=ctx)
            return cached

        # Лимиты нагрузки проверяются только здесь — когда ясно, что считать
        # действительно придётся. Ответ из кэша или ожидание чужого лока почти
        # ничего не стоят, резать их незачем.
        slot = ConcurrencySlot(
            self._redis,
            client_ip,
            self._settings.max_concurrent_per_ip,
            self._settings.concurrency_ttl_seconds,
        )
        if not await slot.acquire():
            log.warning("per_ip_concurrency_rejected", extra=ctx)
            raise TooManyConcurrent("С этого IP уже выполняется вычисление")
        try:
            value, duration_ms = await self._run_with_backpressure(n, ctx)
        finally:
            await slot.release()

        result = ComputeResult(
            input=n, result=value, from_cache=False, duration_ms=duration_ms, computed_by=HOSTNAME
        )
        # Кладём в кэш ДО снятия лока (снятие — в finally вызывающего): ждущие
        # проверяют кэш, и к моменту исчезновения лока результат уже должен там быть.
        await self._cache.set(n, result.to_dict())
        return result

    async def _run_with_backpressure(self, n: int, ctx: dict) -> tuple[int, float]:
        # Сервер ждёт свободного слота ЗА клиента: при коротких вычислениях (порог
        # на N держит худший случай около секунды) место освобождается быстро, и
        # подождать пару секунд на сервере лучше, чем отказать и заставить клиента
        # гадать, когда повторить. Точное время клиенту назвать нельзя без
        # резервирования места — а резерв по порядку и есть эта очередь.
        #
        # Но очередь ОГРАНИЧЕНА по длине и по времени ожидания: неограниченная
        # очередь — это растущая без предела задержка и память, а в итоге клиенты
        # отваливаются по таймауту, когда CPU на их задачи уже потрачен. Поэтому
        # переполнение или долгое ожидание -> честный 503 + Retry-After.
        limit = self._settings.max_pending_computations
        if self._pending >= limit:
            log.warning("backpressure_rejected", extra={**ctx, "pending": self._pending})
            raise Overloaded("Сервер перегружен, повторите позже", reason="overloaded")

        self._pending += 1
        try:
            queued_at = time.perf_counter()
            if self._slots.locked():
                log.info("compute_queued", extra={**ctx, "waiting": self.waiting + 1})
            try:
                async with asyncio.timeout(self._settings.compute_queue_wait_seconds):
                    await self._slots.acquire()
            except TimeoutError:
                log.warning("queue_wait_timeout", extra=ctx)
                raise Overloaded(
                    "Не дождались свободного места, повторите позже", reason="queue_timeout"
                ) from None
            queue_ms = round((time.perf_counter() - queued_at) * 1000, 2)

            self._running += 1
            log.info("compute_started", extra={**ctx, "queue_ms": queue_ms})
            started = time.perf_counter()
            try:
                value = await self._runner(n)
            finally:
                self._running -= 1
                self._slots.release()
        finally:
            self._pending -= 1
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        # duration_ms по каждому вычислению — основа для подбора MAX_FIBONACCI_N
        # замером на целевой машине.
        log.info("compute_finished", extra={**ctx, "duration_ms": duration_ms})
        return value, duration_ms

    async def _wait_for_result(
        self, n: int, lock: RedisLock, name: str, released: asyncio.Event, deadline: float
    ) -> ComputeResult | None:
        """Ждёт, пока держатель посчитает. None — лок исчез без результата."""
        while True:
            if released.is_set():
                # Прошлое уведомление уже использовано (лок снимали, но его снова
                # взял другой). Регистрируемся на следующее ДО проверок ниже, чтобы
                # снятие между проверкой и ожиданием не потерялось.
                released = self._notifier.watch(name)
            # Спим до уведомления о снятии лока, но не дольше интервала
            # перепроверки: уведомление может не прийти (Pub/Sub без гарантий
            # доставки, держатель убит SIGKILL и лок истекает по TTL молча).
            remaining = deadline - time.monotonic()
            try:
                async with asyncio.timeout(
                    max(0.0, min(self._settings.lock_recheck_interval_seconds, remaining))
                ):
                    await released.wait()
            except TimeoutError:
                pass

            cached = await self._from_cache(n)
            if cached is not None:
                log.info("lock_wait_cache_hit", extra={"input": n})
                return cached

            if not await lock.is_locked():
                return None

            # Ограничиваем ожидание: иначе при зависшем держателе запрос висел бы
            # до истечения TTL лока, держа соединение и воркер.
            if time.monotonic() >= deadline:
                log.warning("lock_wait_timeout", extra={"input": n})
                raise LockWaitTimeout(f"Результат для N={n} не готов за отведённое время")

    async def _announce_release(self, name: str, ctx: dict) -> None:
        try:
            await publish_lock_released(self._redis, name)
        except RedisError:
            # Не страшно: ждущие проснутся по периодической перепроверке.
            log.warning("lock_release_publish_failed", extra=ctx, exc_info=True)

    async def _from_cache(self, n: int) -> ComputeResult | None:
        payload = await self._cache.get(n)
        if payload is None:
            return None
        return ComputeResult(**{**payload, "from_cache": True})
