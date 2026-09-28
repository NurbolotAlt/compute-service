"use strict";

const FEED_LIMIT = 50;

const form = document.getElementById("compute-form");
const resultBox = document.getElementById("result");
const feed = document.getElementById("feed");
const feedEmpty = document.getElementById("feed-empty");
const wsStatus = document.getElementById("ws-status");

// Сообщения ленты приходят от ЛЮБЫХ пользователей, поэтому вставляем их только
// через textContent, никогда через innerHTML: чужие данные не должны становиться
// разметкой (XSS), даже если сейчас там одни числа.
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function badge(fromCache) {
  return fromCache
    ? el("span", "badge cache", "from_cache")
    : el("span", "badge computed", "вычислено");
}

// ---------------------------------------------------------------------------
// Форма
// ---------------------------------------------------------------------------

const MAX_ATTEMPTS = 5;

// Причины отказа, при которых есть смысл повторить автоматически: место в
// очереди или предыдущее вычисление освободится через секунду-другую.
// rate_limit сюда НЕ входит: до конца минутного окна повтор гарантированно
// получит тот же отказ — это просто лишняя нагрузка.
const RETRYABLE = new Set([
  "overloaded", "queue_timeout", "ip_busy", "lock_timeout", "redis_unavailable",
]);

const REASON_TEXT = {
  overloaded: "Сервер занят",
  queue_timeout: "Очередь не успела дойти",
  ip_busy: "Ваше предыдущее вычисление ещё идёт",
  lock_timeout: "Такое же вычисление идёт слишком долго",
  redis_unavailable: "Сервис временно недоступен",
};

const submitButton = form.querySelector('button[type="submit"]');
const cancelButton = document.getElementById("cancel-btn");
let controller = null;

function showPending(text) {
  resultBox.hidden = false;
  resultBox.className = "result pending";
  resultBox.textContent = text;
}

function showResult(data) {
  resultBox.hidden = false;
  resultBox.className = "result";
  resultBox.replaceChildren(
    el("div", null, `fibonacci(${data.input}) = ${data.result}`),
    badge(data.from_cache),
    el("div", "muted",
      `считала ${data.computed_by} за ${data.duration_ms} мс · ответила ${data.served_by}`),
  );
}

function showError(status, body) {
  let detail = `Ошибка ${status}`;
  // Для 422 сервер присылает точную причину ("N не должно превышать 35").
  if (status === 422 && Array.isArray(body?.detail) && body.detail[0]?.msg) {
    detail = body.detail[0].msg;
  } else if (typeof body?.detail === "string") {
    detail = body.detail;
  }
  resultBox.hidden = false;
  resultBox.className = "result error";
  resultBox.textContent = `${status}: ${detail}`;
}

// Пауза с обратным отсчётом, которую можно прервать кнопкой "Отмена".
function countdown(ms, render, signal) {
  return new Promise((resolve, reject) => {
    const end = Date.now() + ms;
    const tick = () => render(Math.max(0, Math.ceil((end - Date.now()) / 1000)));
    tick();
    const timer = setInterval(tick, 250);
    const done = setTimeout(() => { clearInterval(timer); resolve(); }, ms);
    signal.addEventListener("abort", () => {
      clearInterval(timer);
      clearTimeout(done);
      reject(new DOMException("cancelled", "AbortError"));
    }, { once: true });
  });
}

// Минутный лимит исчерпан: кнопка недоступна до конца окна, с отсчётом.
function cooldown(seconds) {
  submitButton.disabled = true;
  const end = Date.now() + seconds * 1000;
  const tick = () => {
    const left = Math.ceil((end - Date.now()) / 1000);
    if (left <= 0) {
      clearInterval(timer);
      submitButton.disabled = false;
      submitButton.textContent = "Посчитать";
      return;
    }
    submitButton.textContent = `Можно снова через ${left} с`;
  };
  const timer = setInterval(tick, 250);
  tick();
}

async function compute(n, signal) {
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    // Пока запрос висит, сервер может держать его в своей очереди ожидания —
    // показываем, что идёт время, а не "зависло".
    const sentAt = Date.now();
    const waitingTimer = setInterval(() => {
      const s = Math.floor((Date.now() - sentAt) / 1000);
      showPending(`Считаем… ${s} с (если сервер занят — ждём своей очереди)`);
    }, 500);
    showPending("Считаем…");

    let response, body;
    try {
      response = await fetch("/api/compute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: n }),
        signal,
      });
      body = await response.json().catch(() => null);
    } finally {
      clearInterval(waitingTimer);
    }

    if (response.ok) return showResult(body);

    const reason = body?.reason;
    if (reason === "rate_limit") {
      cooldown(Number(response.headers.get("Retry-After")) || 60);
      return showError(response.status, body);
    }
    if (!RETRYABLE.has(reason) || attempt === MAX_ATTEMPTS) {
      return showError(response.status, body);
    }

    // Повтор через Retry-After ПЛЮС случайная добавка, растущая с каждой
    // попыткой. Без случайности все клиенты, получившие отказ одновременно,
    // одновременно же и повторят — и снова упрутся в перегрузку (thundering herd).
    const base = (Number(response.headers.get("Retry-After")) || 2) * 1000;
    const delay = base + Math.random() * 1000 * attempt;
    await countdown(delay, (left) => showPending(
      `${REASON_TEXT[reason]}. Повторим через ${left} с (попытка ${attempt + 1} из ${MAX_ATTEMPTS})`,
    ), signal);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const n = Number(form.elements.input.value);
  controller = new AbortController();
  submitButton.disabled = true;
  cancelButton.hidden = false;
  try {
    await compute(n, controller.signal);
  } catch (err) {
    if (err.name === "AbortError") {
      showError("отменено", { detail: "запрос отменён" });
    } else {
      showError("сеть", { detail: "сервер недоступен" });
    }
  } finally {
    cancelButton.hidden = true;
    // Если включился отсчёт минутного лимита, кнопку разблокирует он сам.
    if (!submitButton.textContent.startsWith("Можно снова")) submitButton.disabled = false;
  }
});

cancelButton.addEventListener("click", () => controller?.abort());

// Порог N — с сервера. max в HTML — только подсказка браузеру, проверяет сервер.
fetch("/api/limits")
  .then((r) => r.json())
  .then(({ max_fibonacci_n: max }) => {
    const input = form.elements.input;
    input.max = String(max);
    input.title = `0…${max}`;
  })
  .catch(() => { /* без лимитов форма работает с max из HTML */ });

// ---------------------------------------------------------------------------
// Какая реплика отвечает — каждое нажатие может попасть на другую
// ---------------------------------------------------------------------------

document.getElementById("instance-btn").addEventListener("click", async () => {
  const out = document.getElementById("instance-info");
  try {
    const response = await fetch("/api/debug/instance-info");
    const info = await response.json();
    out.textContent = `hostname=${info.hostname} pid=${info.pid} ` +
      `считается=${info.running} ждёт=${info.waiting} ws=${info.ws_clients}`;
  } catch {
    out.textContent = "недоступно";
  }
});

// ---------------------------------------------------------------------------
// Живая лента по WebSocket
// ---------------------------------------------------------------------------

function addToFeed(data) {
  const item = el("li");
  item.append(
    el("span", "n", `F(${data.input})`),
    el("span", "value", String(data.result)),
    badge(data.from_cache),
    el("span", "meta",
      `${new Date().toLocaleTimeString()} · считала ${data.computed_by}` +
      ` (${data.duration_ms} мс) · ответила ${data.served_by}`),
  );
  feed.prepend(item);
  while (feed.children.length > FEED_LIMIT) feed.lastElementChild.remove();
  feedEmpty.hidden = true;
}

function setWsState(state, text) {
  wsStatus.dataset.state = state;
  wsStatus.textContent = text;
}

let reconnectDelay = 1000;

function connect() {
  // wss на https-странице: браузер не даст открыть незашифрованный ws:// со
  // страницы, загруженной по HTTPS (mixed content).
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  setWsState("connecting", "подключение…");

  ws.addEventListener("open", () => {
    reconnectDelay = 1000;
    setWsState("open", "лента онлайн");
  });

  ws.addEventListener("message", (event) => {
    try {
      addToFeed(JSON.parse(event.data));
    } catch {
      // Битое сообщение не должно ломать ленту.
    }
  });

  ws.addEventListener("close", () => {
    // Реплика перезапустилась или сеть моргнула — переподключаемся сами.
    // Экспоненциальная задержка: при лежащем сервере сотни вкладок не должны
    // долбить его переподключениями каждую секунду.
    setWsState("closed", "нет связи, переподключение…");
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
  });
}

connect();
