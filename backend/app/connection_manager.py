"""WebSocket-клиенты ОДНОГО воркера.

Каждый воркер знает только свои сокеты: WebSocket — долгое соединение, которое
nginx привязал к конкретному процессу конкретной реплики. Как сообщение о
результате с ДРУГОЙ реплики доходит сюда — см. pubsub.py.
"""

import asyncio
import logging

from fastapi import WebSocket

log = logging.getLogger(__name__)

# Сколько ждём один сокет при рассылке. Медленный клиент (плохая сеть,
# переполненный буфер) не должен задерживать ленту для всех остальных.
SEND_TIMEOUT_SECONDS = 2.0


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        # Ссылки на запущенные рассылки: asyncio держит на задачи только слабые
        # ссылки, и без этого множества задачу мог бы собрать сборщик мусора.
        self._tasks: set[asyncio.Task] = set()

    @property
    def count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        log.info("ws_connected", extra={"ws_clients": self.count})

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)
        log.info("ws_disconnected", extra={"ws_clients": self.count})

    def broadcast_nowait(self, message: str) -> None:
        """Для обработчика подписки: запустить рассылку и сразу вернуться, не
        задерживая разбор следующих сообщений (в т.ч. уведомлений о локах)."""
        task = asyncio.create_task(self.broadcast(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def broadcast(self, message: str) -> None:
        # Копия множества: пока идут await'ы, connect/disconnect могут его менять.
        targets = list(self._connections)
        if not targets:
            return
        # Параллельно, а не по очереди: иначе время рассылки = сумма задержек всех
        # клиентов, и один зависший сокет тормозит ленту у остальных.
        results = await asyncio.gather(
            *(asyncio.wait_for(ws.send_text(message), SEND_TIMEOUT_SECONDS) for ws in targets),
            return_exceptions=True,
        )
        for ws, result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                # Отвалившийся или слишком медленный клиент — выкидываем, он
                # переподключится сам (это делает фронтенд).
                self.disconnect(ws)
