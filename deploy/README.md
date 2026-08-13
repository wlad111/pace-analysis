# Развёртывание Pace Analysis

Два контейнера: `api` (uvicorn) и `web` (Caddy — статика, TLS и прокси на `/api`).
Состояние — один файл SQLite в именованном томе. Порты наружу открывает только Caddy.

Замерено на реальных данных: процесс API занимает **133 МБ** (пик 184 МБ), самый тяжёлый запрос —
сравнение пилотов с бутстрэпом на 983 кругах — **31 мс**, база растёт примерно на **10 МБ в год**.

---

## 1. Что сделать на сервере (Ubuntu 24.04)

### 1.1. Пользователь

Работать под `root` не нужно. Создайте отдельного пользователя и добавьте его в группу `docker`:

```bash
adduser --gecos "" pace
usermod -aG sudo pace          # чтобы мог ставить пакеты
install -d -m 700 -o pace -g pace /home/pace/.ssh
```

> **Осторожно:** членство в группе `docker` равносильно правам root на хосте — демон Docker работает
> от root, и любой, кто может запускать контейнеры, может смонтировать корень файловой системы.
> Это нормальная практика для сервера одного назначения, но иллюзий на счёт «непривилегированного»
> пользователя строить не стоит.

### 1.2. Доступ по SSH

Положите свой публичный ключ в `/home/pace/.ssh/authorized_keys` (`chmod 600`, владелец `pace`).

Дальше отключите вход по паролю — это одна строка, которая убирает 99% автоматических атак:

```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl reload ssh
```

### 1.3. Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker pace
newgrp docker            # или перелогиньтесь
docker compose version   # должно ответить v2.x
```

### 1.4. Файрвол

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
```

Порт 8000 наружу открывать **не нужно**: в compose у сервиса `api` нет `ports`, только `expose`,
поэтому он доступен исключительно Caddy внутри сети Docker.

### 1.5. Домен (не обязательно)

Если есть домен — направьте `A`-запись на IP сервера и впишите его в `SITE_ADDRESS`. Caddy сам
получит и будет продлевать сертификат Let's Encrypt. Без домена сайт работает по HTTP на `:80`.

---

## 2. Первый деплой

```bash
git clone https://github.com/wlad111/pace-analysis.git
cd pace-analysis
cp deploy/.env.example deploy/.env
nano deploy/.env                     # SITE_ADDRESS, при необходимости

docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --build
docker compose -f deploy/compose.yaml ps
curl -s localhost/api/health         # {"status":"ok","sessions":0,"read_only":true}
```

Первая сборка занимает несколько минут: ставится scipy (117 МБ) и `npm ci` (162 МБ). Дальше слои
кешируются, и пересборка после правки кода — секунды.

### Перенос имеющихся сессий

Свежая база пуста. Чтобы поднять стенд с уже разобранными гонками, скопируйте локальную базу:

```bash
# на своей машине
scp data/pace.db pace@СЕРВЕР:/tmp/pace.db

# на сервере
docker compose -f deploy/compose.yaml cp /tmp/pace.db api:/data/pace.db
docker compose -f deploy/compose.yaml restart api
curl -s localhost/api/health         # sessions должно стать 4
```

> **Перед публичным стендом обезличьте пилотов.** В базе одиннадцать реальных имён с фамилиями
> (`ФАТЕЕВА МАРИЯ`, `NIKONOV KIRILL`, `ОВЕРЧЕНКО ИВАН` и другие) — это персональные данные
> посторонних людей, и вместе с ними уедут их времена кругов и позиции. Ники вида `KOLYA11`
> оставлять можно: клуб публикует их сам.

---

## 3. Режим только для чтения

`PACE_READ_ONLY=1` (значение по умолчанию) — роуты записи **не регистрируются вовсе**, поэтому
импорт писем и разметка кругов отвечают `404`. Это не косметика в интерфейсе: API доступен через
`curl` независимо от того, что рисует страница, поэтому отключать запись нужно на сервере.

Caddy дополнительно отклоняет `POST/PUT/PATCH/DELETE` на `/api/*` с кодом `405` — на случай, если
флаг когда-нибудь снимут по невнимательности.

**Когда захотите включить запись:** сначала аутентификация, потом флаг. Порядок именно такой.
Минимальный вариант — basic-авторизация в Caddy:

```
handle /api/* {
	basic_auth {
		wlad $2a$14$...   # docker run --rm caddy:2-alpine caddy hash-password
	}
	reverse_proxy api:8000
}
```

После этого уберите блок `@writes` из `Caddyfile` и поставьте `PACE_READ_ONLY=0` в `deploy/.env`.

---

## 4. Обновление

```bash
cd pace-analysis && git pull
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --build
```

Том с базой переживает пересборку: он именованный и к образам не привязан.

---

## 5. Бэкапы

```bash
sudo install -m 755 deploy/backup.sh /usr/local/bin/pace-backup
sudo crontab -e
# 17 4 * * *  /usr/local/bin/pace-backup >>/var/log/pace-backup.log 2>&1
```

Скрипт снимает снимок через `sqlite3.backup`, а не копирует файл: база работает в режиме WAL, и
обычный `cp` во время записи даёт копию без свежих транзакций. После снятия снимок проверяется
(`PRAGMA integrity_check` плюс счёт сессий и кругов) и сжимается; хранится 30 дней.

Восстановление:

```bash
gunzip -c /var/backups/pace-analysis/pace-20260814-041700.db.gz > /tmp/restore.db
docker compose -f deploy/compose.yaml cp /tmp/restore.db api:/data/pace.db
docker compose -f deploy/compose.yaml restart api
```

---

## 6. Диагностика

```bash
docker compose -f deploy/compose.yaml logs -f api      # трейсбеки FastAPI
docker compose -f deploy/compose.yaml logs -f web      # запросы и выдача сертификата
docker stats --no-stream                               # память: ждём ~130-190 МБ у api
docker compose -f deploy/compose.yaml exec api \
    python -c "import sqlite3;print(sqlite3.connect('/data/pace.db').execute('select count(*) from lap').fetchone())"
```

Если сертификат не выдаётся — почти всегда причина одна: `A`-запись ещё не разошлась или 80-й порт
закрыт файрволом. Caddy повторяет попытку сам, смотрите его логи.
