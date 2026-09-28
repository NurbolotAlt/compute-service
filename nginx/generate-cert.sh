#!/bin/sh
# Генерирует self-signed TLS-сертификат при СТАРТЕ контейнера (официальный образ
# nginx запускает скрипты из /docker-entrypoint.d/ перед запуском nginx).
# Не при сборке: сгенерированный в RUN закрытый ключ остался бы в слое образа, и
# любой, кто скачал образ (например, публичный пакет в GHCR), получил бы его.
#
# Для локальной разработки/демо — браузер будет ругаться на "не доверенный
# сертификат" (это нормально, curl нужен флаг -k, браузер — "продолжить
# всё равно"). Для реального продакшена вместо этого используют
# сертификат от Let's Encrypt или другого центра сертификации.
set -e

# Уже есть (рестарт того же контейнера) — не перевыпускаем.
if [ -f /etc/nginx/certs/selfsigned.crt ] && [ -f /etc/nginx/certs/selfsigned.key ]; then
  exit 0
fi

mkdir -p /etc/nginx/certs

openssl req -x509 -nodes -days 365 \
  -newkey rsa:2048 \
  -keyout /etc/nginx/certs/selfsigned.key \
  -out /etc/nginx/certs/selfsigned.crt \
  -subj "/C=KG/ST=Bishkek/L=Bishkek/O=ComputeService/CN=localhost" 2>/dev/null

chmod 600 /etc/nginx/certs/selfsigned.key
echo "Сертификат сгенерирован: /etc/nginx/certs/selfsigned.crt"
