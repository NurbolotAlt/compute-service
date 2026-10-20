# syntax=docker/dockerfile:1

# ============================================================
# Стадия 1: builder — ставит зависимости в отдельный venv.
# Всё, что pip тянет при сборке (кэш, временные файлы, при необходимости
# компиляторы), остаётся в этой стадии и в финальный образ не попадает.
# ============================================================
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# requirements отдельно от кода: слой с зависимостями пересобирается только
# при изменении requirements.txt, а не при каждой правке кода.
COPY requirements.txt .
RUN pip install -r requirements.txt

# ============================================================
# Стадия 2: runtime — только интерпретатор, готовый venv и код.
# ============================================================
FROM python:3.14-slim

# curl — только для HEALTHCHECK ниже.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Непривилегированный пользователь. Принцип минимальных привилегий: если через
# уязвимость в приложении или зависимости кто-то выполнит код в контейнере, он
# получит права пользователя app, а не root — не сможет ставить пакеты, менять
# систему образа и заметно сложнее выберется за пределы контейнера.
RUN useradd --system --no-create-home --uid 10001 app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
# Код принадлежит root и только читается пользователем app: скомпрометированный
# процесс не сможет подменить собственный код.
COPY app ./app

# PYTHONUNBUFFERED: JSON-логи уходят в stdout сразу, а не копятся в буфере.
# WEB_CONCURRENCY / COMPUTE_POOL_SIZE — дефолты; docker-compose переопределяет их
# под машину (правило ёмкости: реплики × WEB_CONCURRENCY × COMPUTE_POOL_SIZE ≈ ядра).
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEB_CONCURRENCY=2 \
    COMPUTE_POOL_SIZE=1

USER app

EXPOSE 8000

# Без HEALTHCHECK Docker знает только "процесс жив / не жив". Gunicorn-мастер
# может жить, а приложение — не отвечать или не видеть Redis. С проверкой
# контейнер получает статус healthy/unhealthy, и на него опирается
# depends_on: condition: service_healthy (nginx ждёт готовый backend).
# start-period: время на старт воркеров и прогрев пула процессов.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

# Gunicorn — мастер-процесс: слушает порт, запускает воркеры, перезапускает
# упавшие, координирует graceful reload/shutdown. UvicornWorker даёт каждому
# воркеру собственный asyncio event loop — на нём работают async def и await,
# в том числе ожидание результата из ProcessPoolExecutor.
#
# Число воркеров НЕ задаётся флагом --workers: gunicorn читает WEB_CONCURRENCY
# из окружения сам, поэтому один образ работает и на 4 ядрах, и на 2.
#
# --timeout 90: воркер, не подающий признаков жизни 90 с, перезапускается.
#   Вычисление идёт в отдельном процессе, event loop не блокируется, поэтому
#   воркер отвечает мастеру и во время долгого фибоначчи.
# --graceful-timeout 30: на SIGTERM (вежливая просьба, её можно обработать)
#   воркеры перестают брать новые запросы и 30 с дорабатывают текущие; потом
#   мастер их убивает. SIGKILL обработать нельзя вообще — поэтому в compose
#   stop_grace_period больше 30 с, иначе Docker пришлёт SIGKILL раньше.
#   Порог на N гарантирует, что любое вычисление укладывается в это окно.
# --worker-tmp-dir /dev/shm: файл heartbeat воркера — в RAM, а не на overlay-
#   файловой системе контейнера, где запись бывает медленной и даёт ложные таймауты.
# --no-control-socket: с gunicorn 25.1 мастер по умолчанию открывает управляющий
#   unix-сокет в $HOME (для утилиты gunicornc). Нам он не нужен, а у пользователя
#   app нет домашней папки — был бы ERROR в логах. Лишний канал управления
#   процессом нам ни к чему: минимальные привилегии.
# --keep-alive 75: сколько держать простаивающее соединение от nginx (по
#   умолчанию 2 с). Должно быть БОЛЬШЕ keepalive_timeout в upstream nginx (60 с):
#   закрывать простаивающее соединение должен nginx, иначе он иногда отправит
#   запрос в соединение, которое backend в этот момент закрывает, — и 502.
# exec-форма (JSON-массив): gunicorn — PID 1 и сам получает SIGTERM от Docker.
CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn_worker.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "90", \
     "--graceful-timeout", "30", \
     "--keep-alive", "75", \
     "--worker-tmp-dir", "/dev/shm", \
     "--no-control-socket"]
