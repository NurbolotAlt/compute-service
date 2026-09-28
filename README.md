# compute-service

Распределённый сервис вычислений, который выдерживает **1000 одновременных клиентов**
без единой ошибки: FastAPI за Nginx, несколько backend-реплик, Redis как кэш, rate
limit, distributed lock и шина событий, живая лента результатов по WebSocket.

Вычисление — N-е число Фибоначчи **наивной рекурсией**. Это намеренно искусственная
CPU-нагрузка (fib(38) ≈ 4 с): суть проекта не в математике, а в том, как система
ведёт себя, когда много людей одновременно просят одно и то же дорогое вычисление.

## Результаты нагрузочного теста

1000 клиентов с разными IP одновременно, 2 реплики × 2 воркера × 1 процесс-вычислитель:

| Сценарий | Итог |
|---|---|
| 1000 × одинаковое N=35, **до исправлений** | 493 × 200, **493 × 500, 14 × 502** |
| 1000 × одинаковое N=35, после | **1000 × 200**, вычисление выполнено **1 раз** |
| 1000 × одинаковое N=38 (4,4 с) | 1000 × 200, 1 вычисление, max 5,0 с |
| 1000 × 19 разных N (20..38) | 1000 × 200 с повтором на клиенте, каждое N посчитано ровно 1 раз |

Что было сломано и как исправлено:
- обычный пул Redis при исчерпании сразу бросал ошибку → `BlockingConnectionPool` ждёт свободное соединение;
- 1000 ждущих опрашивали Redis каждые 200 мс → уведомление о снятии лока через Pub/Sub (команд Redis втрое меньше);
- nginx после одной ошибки выключал живую реплику (`max_fails=1`) → `max_fails=3 fail_timeout=5s`.

## Архитектура

```
 браузер ──HTTPS──► nginx ─┬─ статика с диска (backend не нужен)
                           ├─ /api/* ─► upstream backend    (least_conn, keepalive)
                           └─ /ws    ─► upstream backend_ws (отдельные счётчики)
                                          │
              ┌───────────────────────────┴───────────────────────────┐
              ▼                                                       ▼
   backend-реплика: gunicorn                               backend-реплика: gunicorn
     └ uvicorn-воркеры (event loop)                          └ ...
         └ ProcessPoolExecutor (spawn) ── fibonacci
              │                                                       │
              └──────────────────────► Redis ◄────────────────────────┘
        кэш · distributed lock · rate limit · счётчики · Pub/Sub (лента и "лок снят")
```

Снаружи доступен только nginx: порты backend и Redis не проброшены.

## Ключевые решения

- **Защита от cache stampede** — `SET NX EX` + снятие лока только своим токеном
  (Lua), повторная проверка кэша после взятия лока. 1000 одинаковых запросов → 1 вычисление.
  [distributed_lock.py](backend/app/distributed_lock.py), [compute_service.py](backend/app/compute_service.py)
- **ProcessPoolExecutor, а не потоки** — из-за GIL поток с вычислением блокировал бы
  event loop. Процессы создаются через `spawn`, а не `fork`: чистый интерпретатор без
  копий сокетов, event loop и чужих локов. [main.py](backend/app/main.py)
- **Очередь ожидания на сервере** — если процессы заняты, запрос ждёт свой слот
  (FIFO, ограничено по длине и времени), 503 + `Retry-After` только при переполнении.
  Клиент повторяет со случайной добавкой (против thundering herd).
- **Два слоя rate limit** — грубый в nginx (`limit_req`) и точный в Redis
  (`INCR` + `EXPIRE NX` в транзакции). IP клиента — из `X-Real-IP`, который nginx
  перезаписывает сам. [rate_limit.py](backend/app/rate_limit.py)
- **nginx** — `least_conn` с общей `zone`, отдельный upstream для WebSocket,
  `resolve` (новые реплики подхватываются без перезапуска), keepalive к backend.
  [nginx.conf](nginx/nginx.conf)
- **Отказоустойчивость** — Redis недоступен → 503 `redis_unavailable`, а не 500;
  упавший воркер перезапускает gunicorn, упавший контейнер — Docker;
  graceful shutdown дожидается текущего вычисления.
- **Наблюдаемость** — JSON-логи (`event`, `input`, `instance_hostname`, `duration_ms`),
  в логах nginx видно, какая реплика ответила.

Подробный разбор каждого решения, ручные проверки и все замеры —
в [docs/architecture.md](docs/architecture.md).

## Запуск

```bash
docker compose up --build --scale backend=2
```

Открыть https://localhost/ (сертификат self-signed — браузер предупредит).
Настройки (порог N, размер очереди) — в `.env`, см. [.env.example](.env.example).

**Тесты** — интеграционные, с настоящим Redis через Testcontainers (нужен Docker):

```bash
pip install -r backend/requirements.txt -r requirements-dev.txt   # Linux / CI
pip install -r backend/requirements.in  -r requirements-dev.in    # Windows / macOS
ruff check backend && cd backend && python -m pytest
```

`requirements*.txt` — зафиксированные версии с хэшами, собранные `pip-compile`
из `requirements*.in` под Linux (там, где работают образ и CI).

**Нагрузочный тест** — 1000 клиентов с разными IP изнутри Docker-сети:

```bash
docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml up -d --scale backend=2
docker run --rm -i --network computing_service_default computing_service-backend \
  python - --clients 1000 --min-n 35 --max-n 35 < loadtest/loadtest.py
docker compose up -d --scale backend=2   # обязательно вернуть обычный режим nginx
```

## Быстрые проверки

```bash
# распределение по репликам — разные hostname
for i in $(seq 1 6); do curl -sk https://localhost/api/debug/instance-info; echo; done
# порог на N — 422 сразу
curl -sk -X POST https://localhost/api/compute -d '{"input": 50}'
# 5 одинаковых запросов (N, которого ещё нет в кэше) → в логах ровно один compute_started
for i in 1 2 3 4 5; do curl -sk -o /dev/null -X POST https://localhost/api/compute -d '{"input": 33}' & done; wait
docker compose logs --since 30s backend | grep '"input": 33' | grep -c '"compute_started"'
```

## CI/CD и деплой

[GitHub Actions](.github/workflows/ci-cd.yml): ruff + pytest + проверка уязвимостей
(Trivy) → сборка образов под amd64 и arm64 → push в GHCR → деплой по SSH на Oracle
Cloud (ARM) → smoke test. Зависимости обновляет Dependabot. Подготовка сервера —
[deploy/DEPLOY-ORACLE.md](deploy/DEPLOY-ORACLE.md).

## Структура

```
backend/app/     main.py, compute_service.py, distributed_lock.py, cache.py,
                 rate_limit.py, pubsub.py, connection_manager.py, redis_client.py,
                 compute.py, config.py, logging_config.py
backend/tests/   интеграционные тесты (Testcontainers + Redis)
nginx/           nginx.conf, Dockerfile, генерация сертификата
static/          фронтенд без фреймворков
loadtest/        нагрузочный тест
deploy/          production compose, инструкция по деплою
docs/            подробная архитектура
```

## Лицензия

[MIT](LICENSE)
