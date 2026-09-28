# Деплой на Oracle Cloud Always Free (ARM) — ручная подготовка

Пайплайн (`ci-cd.yml`) делает всё автоматически, но ему нужна подготовленная
машина и секреты. Это делается один раз руками.

## 0. Что важно знать заранее

Условия бесплатного тарифа меняются — **перепроверь их в консоли Oracle**. На
сентябрь 2026 Always Free — это 2 ARM-ядра (Ampere A1) и 12 ГБ RAM суммарно
(до 15 июня 2026 было 4 ядра и 24 ГБ), плюс до двух AMD micro-VM и 200 ГБ диска.

- **Карта при регистрации нужна** (верификация). Если карта не проходит, дальше
  Oracle не пойти — тогда смотри дешёвый VPS.
- **Домашний регион выбирается при регистрации и потом не меняется.** Бесплатные
  ресурсы создаются в нём. В популярных регионах бывает ошибка
  `Out of host capacity` — тогда повтори позже или попробуй другой
  availability domain.
- **Простаивающие инстансы Oracle может забрать** (несколько дней подряд ниже ~20%
  по CPU, сети и памяти). Демо без трафика — как раз такой случай. Если заберут,
  весь стек восстанавливается за 10–15 минут по этой инструкции + повторный
  запуск пайплайна — это ещё один довод за автоматизацию.
- Архитектура **ARM (aarch64)**: образы должны быть собраны под `linux/arm64`
  (пайплайн это делает). Официальные образы `nginx`, `redis`, `python` — multi-arch.

## 1. Создание VM

Compute → Instances → Create instance:

- Image: **Ubuntu** (ARM / aarch64)
- Shape: **VM.Standard.A1.Flex**, 2 OCPU, 12 ГБ
- Networking: назначить публичный IPv4
- SSH keys: загрузить свой публичный ключ (для входа руками)

Запиши публичный IP — это будущий `SSH_HOST`. Пользователь по умолчанию у
Ubuntu-образов — `ubuntu` (это `SSH_USER`).

## 2. Открыть порты — в ДВУХ местах

Самая частая причина "всё запущено, а снаружи не открывается" — открыт только один
из двух уровней.

**Уровень 1: сеть Oracle.** Networking → Virtual Cloud Networks → твоя VCN →
Security Lists → Ingress Rules → добавить TCP **80** и **443** с источника
`0.0.0.0/0`. Порт 22 там уже есть (при желании сузь до своего IP).

**Уровень 2: файрвол самой VM.** На Ubuntu-образах Oracle часто настроены
ограничивающие правила iptables:

```bash
sudo DEBIAN_FRONTEND=noninteractive apt install -y iptables-persistent
sudo iptables -L INPUT -n --line-numbers      # найди номер правила REJECT
# вставь правила ПЕРЕД ним (подставь номер вместо <N>):
sudo iptables -I INPUT <N> -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT <N> -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Если в выводе нет правила REJECT, а политика `ACCEPT`, этот шаг можно пропустить.

## 3. Установить Docker

```bash
curl -fsSL https://get.docker.com | sudo sh    # скрипт из официальной инструкции Docker
sudo usermod -aG docker $USER                  # затем выйди и зайди по SSH заново
docker version
docker compose version
```

(Установка через скрипт — нормально для учебной VM. Для серьёзного продакшена
ставят из apt-репозитория по официальной документации.)

## 4. Отдельный ключ для деплоя

Для GitHub Actions заведи **отдельный** ключ — не свой личный. На своём компьютере
(Windows PowerShell; пустую passphrase в PowerShell задают как `'""'`):

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\compute_deploy" -C "github-actions-deploy" -N '""'
```

Добавь публичный ключ на сервер:

```powershell
type "$env:USERPROFILE\.ssh\compute_deploy.pub" | ssh ubuntu@<IP> "cat >> ~/.ssh/authorized_keys"
```

Отпечаток сервера для `SSH_KNOWN_HOSTS`:

```powershell
ssh-keyscan -H <IP>
```

## 5. Секреты в GitHub

Репозиторий → Settings → Secrets and variables → Actions → New repository secret
(либо в Environment `production`, если создашь его):

| Секрет | Значение |
|---|---|
| `SSH_HOST` | публичный IP VM |
| `SSH_USER` | `ubuntu` |
| `SSH_PRIVATE_KEY` | всё содержимое файла `compute_deploy` (приватный ключ, включая строки BEGIN/END) |
| `SSH_KNOWN_HOSTS` | вывод `ssh-keyscan -H <IP>` |

Приватный ключ нигде, кроме этого секрета, лежать не должен и в репозиторий
не попадает никогда.

Затем там же, на вкладке **Variables**, создай переменную `DEPLOY_ENABLED` со
значением `true`. Без неё пайплайн только тестирует и собирает образы, а шаг
деплоя пропускает — так репозиторий можно публиковать до подготовки сервера.

## 6. Доступ сервера к образам в GHCR

После первого прогона пайплайна образы появятся на `ghcr.io/<владелец в нижнем
регистре>/compute-backend` и `compute-nginx`. Серверу нужно уметь их скачивать:

- **Проще для публичного демо:** в настройках пакета (Packages → пакет → Package
  settings) поставить видимость **Public**. Проверь, какая видимость получилась
  по умолчанию — она может быть приватной.
- **Если пакеты приватные:** один раз на сервере выполнить `docker login ghcr.io -u
  <логин>` с personal access token, у которого есть право `read:packages`.

## 7. Первый деплой и проверка

Запушь в `main` и следи за вкладкой Actions. После зелёного прогона:

```bash
# на сервере
cd ~/compute-service
docker compose ps                    # все сервисы Up (healthy)
docker compose logs -f nginx         # в логах виден upstream=<адрес реплики>
```

В браузере `https://<IP>/` — браузер предупредит о self-signed сертификате, это
ожидаемо. Для нормального HTTPS нужно доменное имя + Let's Encrypt (раздел 11).

**Замерь вычисления на этой VM** — пороги подбирались на x86, ARM медленнее:

```bash
curl -sk -X POST https://localhost/api/compute -d '{"input": 35}'
docker compose logs backend | grep compute_finished | tail -1   # duration_ms
```

- худшее вычисление при `MAX_FIBONACCI_N` должно занимать несколько секунд;
- `(BACKPRESSURE_FACTOR − 1) × duration < COMPUTE_QUEUE_WAIT_SECONDS` (10 с) —
  иначе последние в очереди будут получать 503 `queue_timeout`.

Поменять — переменными в `~/compute-service/.env` на сервере (compose читает его
сам), затем `docker compose up -d --scale backend=2`.

## 8. Откат

Образы тегируются git SHA, поэтому откат — это запуск со старым SHA:

```bash
cd ~/compute-service
OWNER_LC=<владелец> IMAGE_TAG=<старый sha> docker compose up -d --scale backend=2
```

SHA берёшь из истории коммитов или Actions. Старые образы остаются на диске
(`docker image prune -f` удаляет только "висячие"); если места не хватает —
`docker image prune -a --filter "until=168h"`.

## 9. Чеклист безопасности публичного деплоя

- [ ] `MAX_FIBONACCI_N` подобран замером на этой VM (худший случай — несколько секунд)
- [ ] Наружу открыты только 22, 80, 443; порты Redis и backend не проброшены
- [ ] Вход по SSH только по ключу, для деплоя — отдельный ключ
- [ ] Rate limit работает в двух слоях (nginx + Redis), IP клиента берётся из `X-Real-IP`
- [ ] Логи контейнеров ограничены (`max-size`), диск не забьётся
- [ ] Self-signed сертификат — временное решение; следующий шаг — домен + Let's Encrypt

## 10. Частые проблемы

| Симптом | Вероятная причина |
|---|---|
| Снаружи не открывается, а `curl` с самой VM работает | Открыт только один из двух уровней файрвола (шаг 2) |
| `exec format error` при запуске контейнера | Образ собран не под arm64 — проверь `platforms` в пайплайне |
| `denied` / `unauthorized` при `docker compose pull` | Пакет в GHCR приватный (шаг 6) |
| 502 от nginx в первые секунды после пересоздания backend | nginx перечитывает адреса реплик раз в 5 с (`resolver ... valid=5s`) — подожди; если держится дольше, проверь `docker compose ps` (backend healthy?) |
| `Out of host capacity` при создании VM | В регионе нет свободных ресурсов — повторить позже / другой availability domain |
| SSH из пайплайна: `Host key verification failed` | `SSH_KNOWN_HOSTS` пустой или от другого IP |
| Много 503 `queue_timeout` в логах | Вычисления на ARM дольше, чем рассчитано: уменьши `BACKPRESSURE_FACTOR` или `MAX_FIBONACCI_N` (шаг 7) |

## 11. Следующий шаг: HTTPS без предупреждения браузера

Self-signed сертификат шифрует трафик, но браузер ему не доверяет. Доверенный
сертификат выдаёт Let's Encrypt (бесплатно), и обычно ему нужно **доменное имя**:

1. Получить домен. Бесплатно — поддомен на [duckdns.org](https://www.duckdns.org)
   (вход через GitHub, указать IP VM).
2. Выпустить сертификат через certbot (HTTP-01 проверка идёт на порт 80 — он у нас
   открыт) и смонтировать его в контейнер nginx вместо self-signed:
   `generate-cert.sh` не перезаписывает уже существующие файлы в `/etc/nginx/certs`.
3. Настроить автопродление (certbot renew по таймеру + `nginx -s reload`) и
   включить HSTS в nginx.conf — только ПОСЛЕ того, как доверенный сертификат работает.
