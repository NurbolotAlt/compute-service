FROM nginx:alpine

# openssl нужен для генерации self-signed сертификата
RUN apk add --no-cache openssl

# Сертификат генерируется при СТАРТЕ контейнера, а не при сборке — иначе закрытый
# ключ был бы зашит в слой образа (подробнее — в самом скрипте). sed убирает CRLF
# на случай, если файл сохранён на Windows: с \r шебанг не сработает.
COPY nginx/generate-cert.sh /docker-entrypoint.d/05-generate-cert.sh
RUN sed -i 's/\r$//' /docker-entrypoint.d/05-generate-cert.sh \
    && chmod +x /docker-entrypoint.d/05-generate-cert.sh

COPY nginx/nginx.conf /etc/nginx/conf.d/default.conf

# Все пути в COPY — от КОРНЯ проекта (build context = корень: context: .,
# dockerfile: nginx/Dockerfile — так его собирают и compose, и пайплайн).
# Статика фронтенда — HTML/CSS/JS дашборда.
# ВАЖНО: этот COPY предполагает, что build context в docker-compose.yml
# указывает на КОРЕНЬ проекта (context: .), а не на папку nginx/ —
# Docker не разрешает COPY файлов из-за пределов build-контекста
# (../static сюда бы не сработал). Смотри сервис nginx в docker-compose.yml.
COPY static /usr/share/nginx/html

EXPOSE 80 443

CMD ["nginx", "-g", "daemon off;"]
