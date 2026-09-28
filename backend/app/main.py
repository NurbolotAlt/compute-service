"""HTTP/WebSocket-слой: валидация, лимиты, маршрутизация ошибок в статусы.

Запуск — через gunicorn с UvicornWorker (см. backend/Dockerfile). Всё, что создаётся
в lifespan (пул Redis, пул процессов, подписка Pub/Sub), создаётся в КАЖДОМ
gunicorn-воркере отдельно — у каждого свой event loop и свои ресурсы.
"""

import asyncio
import logging
import multiprocessing
import os
import socket
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, create_model
from redis.exceptions import RedisError

from app.compute import fibonacci
from app.compute_service import (
    ComputeService,
    LockWaitTimeout,
    Overloaded,
    Runner,
    TooManyConcurrent,
)
from app.config import Settings, get_settings
from app.connection_manager import ConnectionManager
from app.distributed_lock import LOCK_RELEASED_CHANNEL, LockReleaseNotifier
from app.logging_config import setup_logging
from app.pubsub import RESULTS_CHANNEL, RedisSubscriber, publish_result
from app.rate_limit import check_rate_limit
from app.redis_client import create_client, create_pool

log = logging.getLogger(__name__)

HOSTNAME = socket.gethostname()


def client_ip(request: Request) -> str:
    """IP реального клиента — для rate limit и лимита параллельных вычислений.

    За nginx request.client.host — это адрес контейнера nginx, одинаковый для
    всех: без поправки все пользователи делили бы один лимит на всех. Поэтому
    берём X-Real-IP, который nginx выставляет из $remote_addr.

    Доверять заголовку МОЖНО только потому, что backend недоступен снаружи в обход
    nginx (порт не проброшен, есть только внутренняя Docker-сеть), а nginx
    ПЕРЕЗАПИСЫВАЕТ X-Real-IP, а не пробрасывает клиентский. Будь backend открыт
    наружу, любой подставил бы себе случайный X-Real-IP и обошёл лимиты.
    """
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def error(status: int, detail: str, reason: str, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(
        status_code=status, content={"detail": detail, "reason": reason}, headers=headers
    )


def make_process_runner(executor: ProcessPoolExecutor) -> Runner:
    async def run(n: int) -> int:
        # ProcessPoolExecutor, а НЕ ThreadPoolExecutor: fibonacci — чистый CPU на
        # Python-байткоде, а байткод в одном процессе в каждый момент исполняет
        # только один поток (GIL). Поток с фибоначчи отбирал бы GIL у потока event
        # loop, и воркер "подвисал" бы на всё время вычисления, а два вычисления в
        # потоках шли бы не параллельно, а по очереди. У отдельного процесса свой
        # интерпретатор и свой GIL: считает на другом ядре, а event loop свободен
        # и продолжает обслуживать /api/health, WebSocket и ответы из кэша.
        # Цена — pickle аргумента и результата между процессами; для int это копейки.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, fibonacci, n)

    return run


def build_request_model(max_n: int) -> type[BaseModel]:
    # Порог на N — ПЕРВАЯ проверка, до любых обращений к Redis: она бесплатная.
    # Почему он обязателен: наивная рекурсия делает ~φ^N вызовов (φ ≈ 1.618), т.е.
    # +1 к N — примерно ×1.6 времени, +5 — уже ×11. N=50 занял бы ядро на часы.
    # А задачу, уже запущенную в ProcessPoolExecutor, не отменить: Future.cancel()
    # работает только для ещё не начатых задач. Поэтому защищаемся ограничением
    # входа, а не таймаутом постфактум.
    # Модель строится из настроек (а не классом с константой), чтобы порог менялся
    # переменной MAX_FIBONACCI_N без правки кода. strict=True: "35", 35.5 и true
    # не превращаются молча в число.
    return create_model(
        "ComputeRequest",
        input=(Annotated[int, Field(ge=0, le=max_n, strict=True)], ...),
    )


def create_app(settings: Settings | None = None, runner: Runner | None = None) -> FastAPI:
    """Фабрика: тесты подставляют свои настройки и runner, прод — env и пул процессов."""
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    request_model = build_request_model(settings.max_fibonacci_n)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = create_pool(settings)
        redis = create_client(pool)

        executor: ProcessPoolExecutor | None = None
        compute_runner = runner
        if compute_runner is None:
            # spawn: дочерний процесс стартует с чистого интерпретатора. fork
            # скопировал бы работающий event loop, сокеты Redis и состояние
            # потоков родителя — источник редких зависаний. Цена spawn — ~100 мс
            # на старт процесса, один раз.
            executor = ProcessPoolExecutor(
                max_workers=settings.compute_pool_size,
                mp_context=multiprocessing.get_context("spawn"),
            )
            compute_runner = make_process_runner(executor)
            # Прогрев: процесс пула создаётся при первой задаче. Пусть это случится
            # при старте, а не на первом пользовательском запросе.
            await compute_runner(0)

        manager = ConnectionManager()
        notifier = LockReleaseNotifier()
        # Одна подписка воркера на оба канала: лента -> WebSocket-клиентам,
        # "лок снят" -> ждущим этого лока в этом воркере.
        subscriber = RedisSubscriber(
            redis,
            {
                RESULTS_CHANNEL: manager.broadcast_nowait,
                LOCK_RELEASED_CHANNEL: notifier.notify,
            },
        )
        await subscriber.start()

        app.state.redis = redis
        app.state.service = ComputeService(redis, settings, compute_runner, notifier)
        app.state.manager = manager
        log.info(
            "worker_started",
            extra={
                "pid": os.getpid(),
                "compute_pool_size": settings.compute_pool_size,
                "max_fibonacci_n": settings.max_fibonacci_n,
            },
        )
        try:
            yield
        finally:
            # Сюда попадаем при SIGTERM ПОСЛЕ того, как uvicorn дождался текущих
            # запросов (в пределах gunicorn --graceful-timeout). Незавершённых
            # вычислений к этому моменту нет, так что shutdown(wait=True) быстрый;
            # cancel_futures снимает то, что не успело начаться.
            await subscriber.stop()
            if executor is not None:
                await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
            await pool.disconnect()
            log.info("worker_stopped", extra={"pid": os.getpid()})

    app = FastAPI(title="compute-service", lifespan=lifespan)

    @app.exception_handler(RedisError)
    async def redis_unavailable(request: Request, exc: RedisError) -> JSONResponse:
        # Redis недоступен, не отвечает или пул исчерпан дольше redis_pool_timeout.
        # Без этого обработчика клиент получил бы 500 "что-то сломалось" — а это
        # временная недоступность зависимости: 503 + Retry-After, повтор уместен.
        log.error("redis_unavailable", extra={"error": repr(exc)})
        return error(503, "Хранилище временно недоступно", "redis_unavailable", 2)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = []
        for err in exc.errors():
            if err.get("type") == "less_than_equal" and err.get("loc", ())[-1:] == ("input",):
                err = {**err, "msg": f"N не должно превышать {settings.max_fibonacci_n}"}
            errors.append({k: v for k, v in err.items() if k in ("loc", "msg", "type")})
        return JSONResponse(status_code=422, content={"detail": errors})

    # Тело разбираем сами, а не параметром-моделью: FastAPI парсит JSON только при
    # Content-Type: application/json, а `curl -d '{"input": 35}'` без -H шлёт
    # x-www-form-urlencoded. Нам нужен JSON независимо от заголовка.
    # openapi_extra возвращает схему тела в /docs.
    @app.post(
        "/api/compute",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": request_model.model_json_schema()}},
            }
        },
    )
    async def compute_endpoint(request: Request) -> JSONResponse:
        try:
            body = request_model.model_validate_json(await request.body())
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        n: int = body.input
        ip = client_ip(request)
        redis = request.app.state.redis
        service: ComputeService = request.app.state.service

        limit = await check_rate_limit(
            redis, ip, settings.rate_limit_requests, settings.rate_limit_window_seconds
        )
        # reason — машиночитаемая причина отказа. По одному коду клиент не поймёт,
        # что делать: 429 "лимит в минуту" — ждать до конца окна, а 429 "твоё
        # вычисление ещё идёт" — повторить через пару секунд.
        if not limit.allowed:
            log.warning("rate_limited", extra={"input": n, "client_ip": ip})
            return error(
                429,
                f"Не больше {limit.limit} вычислений в минуту",
                "rate_limit",
                retry_after=limit.retry_after,
            )

        try:
            result = await service.get_or_compute(n, ip)
        except TooManyConcurrent as exc:
            return error(429, str(exc), "ip_busy", settings.ip_busy_retry_after_seconds)
        except Overloaded as exc:
            return error(503, str(exc), exc.reason, settings.overload_retry_after_seconds)
        except LockWaitTimeout as exc:
            return error(504, str(exc), "lock_timeout")

        payload = {**result.to_dict(), "served_by": HOSTNAME}
        try:
            await publish_result(redis, payload)
        except RedisError:
            # Лента — побочный эффект: результат уже посчитан, отдаём его клиенту,
            # даже если публикация не удалась.
            log.warning("publish_failed", extra={"input": n}, exc_info=True)
        return JSONResponse(content=payload)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        manager: ConnectionManager = websocket.app.state.manager
        await manager.connect(websocket)
        try:
            # Клиент ничего не шлёт, но receive нужен, чтобы узнать о закрытии
            # соединения и убрать сокет из рассылки.
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.disconnect(websocket)

    @app.get("/api/limits")
    async def limits() -> dict:
        # Фронтенд берёт порог отсюда, а не из захардкоженного max в HTML: иначе
        # при смене MAX_FIBONACCI_N форма либо не пускала бы допустимое N, либо
        # обещала бы недопустимое. Источник истины один — настройки сервера.
        return {
            "max_fibonacci_n": settings.max_fibonacci_n,
            "rate_limit_per_minute": settings.rate_limit_requests,
        }

    @app.get("/api/debug/instance-info")
    async def instance_info(request: Request) -> dict:
        # hostname контейнера = ID реплики (Docker ставит его равным ID контейнера).
        # pid дополнительно показывает, какой gunicorn-воркер внутри реплики ответил.
        return {
            "hostname": HOSTNAME,
            "pid": os.getpid(),
            "pending_computations": request.app.state.service.pending,
            "running": request.app.state.service.running,
            "waiting": request.app.state.service.waiting,
            "ws_clients": request.app.state.manager.count,
        }

    @app.get("/api/health")
    async def health(request: Request) -> JSONResponse:
        try:
            await request.app.state.redis.ping()
        except RedisError:
            # 503, а не 200 с "redis: down": HEALTHCHECK в Docker и smoke test
            # в CI смотрят на код ответа (curl -f).
            return JSONResponse(status_code=503, content={"status": "error", "redis": "down"})
        return JSONResponse(content={"status": "ok", "redis": "ok", "hostname": HOSTNAME})

    return app


# Точка входа для `gunicorn app.main:app`. Соединений здесь не открывается — всё
# тяжёлое происходит в lifespan, уже внутри каждого воркера.
app = create_app()
