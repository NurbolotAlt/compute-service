from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Все параметры читаются из переменных окружения (регистр не важен):
    один и тот же образ работает локально и на сервере, отличаются только env."""

    redis_url: str = "redis://localhost:6379/0"
    # Верхняя граница соединений в пуле ОДНОГО воркера. Держим её конечной, чтобы
    # всплеск запросов не открыл тысячи соединений и не упёрся в maxclients Redis.
    redis_max_connections: int = 50
    # Сколько запрос ждёт свободного соединения из пула, прежде чем получить 503.
    redis_pool_timeout_seconds: float = 5.0
    # Сколько ждать ответа Redis на команду и установки соединения. Redis отвечает
    # за доли миллисекунды; секунды без ответа — это уже авария, а не нагрузка.
    redis_socket_timeout_seconds: float = 5.0
    redis_connect_timeout_seconds: float = 2.0

    # Порог на N: наивная рекурсия растёт как φ^N, поэтому потолок подбирается
    # замером duration_ms на целевой машине (худший случай — несколько секунд).
    max_fibonacci_n: int = 35

    # Процессов в ProcessPoolExecutor на ОДИН gunicorn-воркер. Общее число
    # считающих процессов = реплики × WEB_CONCURRENCY × COMPUTE_POOL_SIZE ≈ ядра.
    compute_pool_size: int = 1

    # Результат детерминирован, так что TTL нужен не ради "свежести", а чтобы
    # кэш не рос бесконечно и Redis не упёрся в память.
    cache_ttl_seconds: int = 3600

    rate_limit_requests: int = 5
    rate_limit_window_seconds: int = 60

    # TTL лока — в РАЗЫ больше худшего вычисления при max_fibonacci_n (секунды):
    # если лок истечёт посреди работы, второй запрос начнёт считать то же самое.
    # Но и не бесконечный: если держатель умрёт, лок сам освободится.
    lock_ttl_seconds: int = 120
    # Сколько ждущий запрос готов ждать чужой лок, прежде чем сдаться с 504.
    lock_wait_timeout_seconds: float = 60.0
    # Ждущий просыпается по уведомлению о снятии лока (Pub/Sub). Эта периодическая
    # перепроверка — только страховка: Pub/Sub не гарантирует доставку, а держатель,
    # убитый SIGKILL, не успеет ничего опубликовать (его лок истечёт по TTL).
    lock_recheck_interval_seconds: float = 1.0

    # Не больше стольких вычислений одного IP одновременно (на все реплики).
    max_concurrent_per_ip: int = 1
    # TTL счётчика — страховка от процесса, умершего до DECR. Больше худшего вычисления.
    concurrency_ttl_seconds: int = 120

    # Сколько задач "считаются + ждут слот" на воркер, в разах от размера пула.
    # Дальше — сразу 503. Подбирается от времени: ждущему достанется слот
    # примерно через (factor - 1) × худшее вычисление / пул; при пороге N=35
    # (~1 с локально) и factor=8 это ~7 с — укладывается в compute_queue_wait_seconds.
    # На медленной машине (ARM) худшее вычисление дольше — factor стоит уменьшить.
    backpressure_factor: int = 8
    # Сколько запрос может ждать свободного слота в очереди воркера, прежде чем
    # получить 503. Страховка на случай, если оценка выше не сработала.
    compute_queue_wait_seconds: float = 10.0
    # Через сколько секунд предлагать повтор при 503 (заголовок Retry-After). Раз
    # вычисление укладывается в секунду-две, место освобождается быстро.
    overload_retry_after_seconds: int = 2
    # То же для 429 "с этого IP уже идёт вычисление": предыдущее скоро закончится.
    ip_busy_retry_after_seconds: int = 2

    log_level: str = "INFO"

    @property
    def max_pending_computations(self) -> int:
        return self.backpressure_factor * self.compute_pool_size


@lru_cache
def get_settings() -> Settings:
    # Кэшируем, чтобы env разбирался один раз на процесс, а не на каждый запрос.
    return Settings()
