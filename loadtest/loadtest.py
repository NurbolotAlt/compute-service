"""Нагрузочный тест: N "клиентов" с разными IP одновременно бьют в POST /api/compute.

Только стандартная библиотека — запускается в образе backend без установки пакетов:
    docker run --rm -i --network computing_service_default computing_service-backend \
        python - --clients 100 --min-n 20 --max-n 35 < loadtest/loadtest.py

Потоки, а не asyncio: здесь они только ждут сеть (GIL при этом отпускается), а
threading.Barrier позволяет отпустить все запросы в один момент.
"""

import argparse
import json
import random
import ssl
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

# Сертификат self-signed — проверку отключаем, это тест внутри своей же сети.
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


# Та же логика, что во фронтенде (static/app.js): повторять только то, что имеет
# смысл повторять, через Retry-After + растущую случайную добавку.
RETRYABLE = {"overloaded", "queue_timeout", "ip_busy", "lock_timeout", "redis_unavailable"}


def send(request: urllib.request.Request) -> tuple:
    try:
        with urllib.request.urlopen(request, context=CTX, timeout=120) as response:
            return response.status, json.loads(response.read()), None
    except urllib.error.HTTPError as exc:
        # Ошибки, которые отдаёт сам nginx (его limit_req, 502/504), — HTML, не JSON.
        try:
            body = json.loads(exc.read())
        except ValueError:
            body = {"reason": "nginx"}
        return exc.code, body, exc.headers.get("Retry-After")
    except OSError as exc:
        return type(exc).__name__, None, None


def client(i: int, n: int, url: str, barrier: threading.Barrier, retries: int) -> dict:
    ip = f"10.77.{i // 250}.{i % 250 + 1}"
    request = urllib.request.Request(
        url,
        data=json.dumps({"input": n}).encode(),
        headers={"Content-Type": "application/json", "X-Forwarded-For": ip},
    )
    barrier.wait()  # все потоки стартуют одновременно
    started = time.perf_counter()
    for attempt in range(1, retries + 2):
        status, body, retry_after = send(request)
        reason = body.get("reason") if isinstance(body, dict) else None
        if status == 200 or reason not in RETRYABLE or attempt > retries:
            break
        time.sleep(float(retry_after or 2) + random.random() * attempt)
    if status != 200:
        status = f"{status} {reason}"
    return {
        "status": status,
        "seconds": time.perf_counter() - started,
        "n": n,
        "body": body if status == 200 else None,
        "attempts": attempt,
    }


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://nginx/api/compute")
    parser.add_argument("--clients", type=int, default=100)
    parser.add_argument("--min-n", type=int, default=20)
    parser.add_argument("--max-n", type=int, default=35)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--retries", type=int, default=0, help="повторов на 503/ip_busy")
    args = parser.parse_args()

    random.seed(args.seed)
    inputs = [random.randint(args.min_n, args.max_n) for _ in range(args.clients)]
    barrier = threading.Barrier(args.clients)

    wall = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.clients) as pool:
        results = list(
            pool.map(
                lambda i: client(i, inputs[i], args.url, barrier, args.retries),
                range(args.clients),
            )
        )
    wall = time.perf_counter() - wall

    print(f"{args.clients} клиентов, N из {args.min_n}..{args.max_n} "
          f"({len(set(inputs))} разных значений), всё заняло {wall:.2f} с\n")

    print("Коды ответов:")
    for status, count in sorted(Counter(r["status"] for r in results).items(), key=str):
        times = [r["seconds"] for r in results if r["status"] == status]
        print(f"  {status}: {count:3d}   время p50={statistics.median(times):.2f}с "
              f"p95={percentile(times, 0.95):.2f}с max={max(times):.2f}с")

    if args.retries:
        attempts = Counter(r["attempts"] for r in results)
        print("Попыток на клиента:", dict(sorted(attempts.items())))

    ok = [r["body"] for r in results if r["status"] == 200]
    if ok:
        computed = [b for b in ok if not b["from_cache"]]
        print(f"\nУспешных: {len(ok)}, из них реально посчитано: {len(computed)} "
              f"(разных N среди успешных: {len({b['input'] for b in ok})}), "
              f"из кэша/после ожидания лока: {len(ok) - len(computed)}")
        print("Кто отвечал (served_by):  ",
              dict(Counter(b["served_by"] for b in ok)))
        print("Кто считал (computed_by): ",
              dict(Counter(b["computed_by"] for b in computed)))


if __name__ == "__main__":
    main()
