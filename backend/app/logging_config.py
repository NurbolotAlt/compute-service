"""Структурированное JSON-логирование.

Одна строка = один JSON-объект с фиксированными полями (timestamp, level, event,
input, instance_hostname, ...). Такой лог можно фильтровать и агрегировать
(jq, Loki, ELK): "все lock_wait_timeout по input=35 на реплике X за час" — это
запрос, а не grep по свободному тексту. С несколькими репликами без поля
instance_hostname вообще непонятно, чья это строка.

Использование: log.info("cache_hit", extra={"input": n}) — сообщение и есть event,
а всё из extra попадает в JSON отдельными полями.
"""

import json
import logging
import socket
import sys
from datetime import UTC, datetime

HOSTNAME = socket.gethostname()

# Атрибуты, которые есть у любого LogRecord. Всё, что сверх них, пришло через
# extra=... и должно попасть в JSON как отдельные поля.
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
            "instance_hostname": HOSTNAME,
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        # default=str: случайный несериализуемый extra не должен ронять логирование.
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # Access-лог uvicorn дублирует лог nginx (где к тому же виден upstream) и
    # удваивает объём логов. Оставляем только предупреждения и ошибки.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # Ошибки uvicorn/gunicorn-воркера тоже идут в JSON через root.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
