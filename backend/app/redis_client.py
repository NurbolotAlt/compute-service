"""Один connection pool Redis на процесс-воркер.

Открыть TCP-соединение с Redis — это round-trip'ы и системные вызовы (как и с БД).
Если делать это на каждый запрос, под нагрузкой задержка и число сокетов растут
вместе с RPS. Пул открывает соединения лениво и переиспользует их между всеми
запросами этого воркера.

Пул создаётся в lifespan приложения (main.py), т.е. ПОСЛЕ fork'а gunicorn-воркера:
сокеты, унаследованные через fork, делились бы между процессами и перемешивали
ответы. Хранится в app.state и закрывается при остановке воркера.
"""

from redis.asyncio import BlockingConnectionPool, Redis

from app.config import Settings


def create_pool(settings: Settings) -> BlockingConnectionPool:
    # BlockingConnectionPool, а не обычный ConnectionPool: когда все соединения
    # заняты, обычный пул СРАЗУ бросает MaxConnectionsError — под нагрузкой это
    # были сотни 500-х (нагрузочный тест: 1000 одинаковых запросов -> 493 ошибки).
    # Блокирующий пул ставит запрос в очередь за соединением и ждёт до timeout;
    # только если и тогда не дождались — ConnectionError, который main.py
    # превращает в честный 503.
    return BlockingConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        timeout=settings.redis_pool_timeout_seconds,
        # Без таймаутов сокета зависший Redis (сеть, перегрузка) подвесил бы
        # запросы навсегда: await на чтении ответа никогда бы не вернулся.
        # Подписку Pub/Sub это не ломает: её блокирующее ожидание сообщений
        # socket_timeout не применяет (проверено на redis-py 8).
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        # TCP keepalive: мёртвое соединение (Redis перезапущен, сеть порвалась)
        # обнаруживается ОС, а не висит в пуле до первой ошибки.
        socket_keepalive=True,
        # decode_responses=True: работаем со строками, а не bytes, во всём приложении.
        decode_responses=True,
    )


def create_client(pool: BlockingConnectionPool) -> Redis:
    # Redis-объект — лёгкая обёртка: на время команды берёт соединение из пула и
    # возвращает обратно. Все запросы воркера делят один такой клиент и один пул.
    return Redis(connection_pool=pool)
