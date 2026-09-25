# Deploy / rollback runbook

Источник истины по деплою инстансов сервиса: топология, процедура релиза, откат, CI/CD.

## Цель деплоя

- **Общий Linux-сервер** с уже работающим **внешним Traefik** (`/opt/edge`), который держит
  порты 80/443, терминирует TLS и выпускает Let's Encrypt-сертификаты. Мы **не** запускаем
  reverse-proxy и **не** управляем TLS.
- Каждый инстанс — каталог `/opt/<instance>` со своим `.env`, своей БД и своим доменом.
- **Образ собирается на сервере** (`docker compose build`), не тянется из registry. Immutable
  registry-тега в этой схеме нет → откат = `git checkout <prev-commit>` + пересборка.

Активные артефакты:

| Файл | Роль |
|---|---|
| `docker-compose.prod.yml` | прод-стек: `api` (`expose: 8000`, сети `web`+`default`, Traefik labels) + `postgres` 16 + `redis` 7 (только `default`, без портов) + one-shot `migrate` |
| `.env.prod.example` | шаблон прод-env (только плейсхолдеры; реальный `.env` живёт на сервере и в git не попадает) |
| `.github/workflows/ci.yml` | CI-гейт + **gated** авто-деплой на push в `main` |
| `.github/workflows/deploy.yml` | ручной деплой (`workflow_dispatch`) |
| `docker-compose.prod.observability.yml` + `infra/observability/` | опциональный Prometheus (только loopback, только сеть `default`, никогда не на `web`) |

## Провижининг нового инстанса

```bash
mkdir -p /opt/<instance>/.secrets && cd /opt/<instance>
git clone <repo> .
cp .env.prod.example .env      # заполнить: SERVICE_NAME, SERVICE_DOMAIN, COMPOSE_PROJECT_NAME, секреты
openssl genrsa -out .secrets/jwt_private.pem 2048
openssl rsa -in .secrets/jwt_private.pem -pubout -out .secrets/jwt_public.pem
chmod 600 .secrets/jwt_private.pem

# DNS: A-запись <SERVICE_DOMAIN> -> IP сервера — ДО первого up (нужна для ACME-challenge Traefik).
# Внешняя сеть создаётся на сервере однократно (если её ещё нет):
docker network create web

docker compose -p <instance> -f docker-compose.prod.yml --env-file .env build api migrate
docker compose -p <instance> -f docker-compose.prod.yml --env-file .env run --rm migrate
docker compose -p <instance> -f docker-compose.prod.yml --env-file .env up -d --no-build
curl -fsS https://<SERVICE_DOMAIN>/healthz
```


Перед приёмом реальных пользователей пройти **prod-readiness checklist**:
`TRUSTED_PROXY_IPS`, `PRODUCTS`, Apple root CA, `DOCS_ENABLED=false`, бэкап PostgreSQL,
алерты по платежам.

## Контракт релиза

1. `git pull --ff-only` в `/opt/<instance>`.
2. `docker compose build api migrate` — реальная ошибка сборки валит инстанс.
3. `docker compose run --rm migrate` (`alembic upgrade head`) — **до** старта нового `api`.
   Миграции обязаны быть **expand-only**: старый `api` продолжает обслуживать трафик во время накатки.
4. `docker compose up -d --no-build` — rc **не считается** признаком успеха.
5. **Readiness-gate** — источник истины: контейнер `<proj>-api-1` обязан стать `healthy`
   (compose healthcheck = `GET /ready`: PostgreSQL + Redis). Не стал за ~60с → инстанс упал.
6. Публичный smoke `GET https://<SERVICE_DOMAIN>/healthz` — **non-fatal** (на первом деплое DNS/ACME
   могут ещё «устаканиваться»).

> **Почему шаги 2–4 разделены и почему rc `up` не проверяется.** Совмещённая команда
> `up -d --build` фьюзит в один exit code сборку BuildKit, one-shot `migrate` (`restart: "no"`) и
> старт `api` — и умеет возвращать транзиентный non-zero при полностью здоровом `api`. Это ложно
> краснило job и обрывало loop по инстансам. Реальные ошибки ловятся явными rc-проверками на
> build/migrate и readiness-gate'ом. Не «упрощать» обратно.

## CI/CD

- `ci.yml` (push в `main` и PR): `quality` (ruff format/check + mypy) → `test` (pytest +
  coverage-гейты) → `alerts` (promtool) → `build-image` (валидация Dockerfile, без push).
- `deploy.yml`: запускается `workflow_run` **после зелёного `ci`** по push в `main` (красная сборка
  не выкатывается и не гоняется параллельно с CI), плюс ручной запуск (`workflow_dispatch`).
  SSH на сервер → `/opt/gelnora`: checkout ровно протестированного коммита (более старый коммит не
  выкатывается) → `build api migrate` → `run --rm migrate` → `up -d --no-build` → readiness-gate
  (`gelnora-api-1` healthy, `gelnora-worker-1` running) → non-fatal smoke
  `https://gelnora.shop/healthz`.
- `appleboy/ssh-action` запинен по **commit SHA**, не по тегу: этому action передаётся прод-SSH-ключ.

**Инстанс:** каталог `/opt/gelnora`, compose-проект `gelnora`, домен `gelnora.shop`
(A-запись → IP сервера). Секреты приложения — только в `/opt/gelnora/.env` и `/opt/gelnora/.secrets`.

**Coverage-гейты:** 80% глобально + **95% на каждый** критический
пакет (`policy`, `wallet`, `auth`, `generation`, `billing*`, `subscription`, `token_purchase`).
Гейт **per-package**, а не по агрегату: единый `--cov-fail-under` на объединение пакетов пропустил
бы `wallet` на 85% за счёт `policy` на 99% — то есть был бы слабее всего там, где ошибка стоит
денег. Реализация: suite прогоняется **один раз** (`pytest --cov=src`), затем каждый пакет
проверяется отдельно поверх тех же данных (`coverage report --include=... --fail-under=95`).

**GitHub Secrets:** `SSH_HOST`, `SSH_USER`, `SSH_PRIVATE_KEY`.

## Наблюдаемость (опциональный overlay)

Перед первым подъёмом overlay'я положить токен скрейпа в файл (он gitignored, в свежем клоне его
нет) и продублировать то же значение в `.env` (`METRICS_SCRAPE_TOKEN`):

```bash
cd /opt/<instance>
openssl rand -base64 32 > infra/observability/secrets/scrape_token
docker compose -p <instance> -f docker-compose.prod.yml -f docker-compose.prod.observability.yml \
  --env-file .env up -d --no-build
```

Монтируется **каталог** `infra/observability/secrets`, а не сам файл: bind-mount отсутствующего
файла заставил бы Docker создать на его месте root-owned **каталог**, после чего Prometheus не
прочитал бы токен, а починить путь можно было бы только через `rm -rf`. Каталог в репо есть
(`.gitkeep`), поэтому монтирование всегда резолвится, а отсутствующий токен даёт понятную ошибку
Prometheus. Prometheus слушает только loopback (`127.0.0.1:9090`) и никогда не попадает в сеть `web`.

## Логи

Все контейнеры прод-стека ограничены (`json-file`, `max-size: 10m`, `max-file: 3` → не больше
~30 МБ на контейнер). Это не косметика: `gunicorn` пишет строку на каждый запрос, а на общем
сервере несколько инстансов делят один диск с Traefik — неограниченный лог одного инстанса
переполнил бы диск и уронил **все** инстансы и edge-прокси разом. Нужна долгая ретенция —
отгружать логи с хоста, а не поднимать лимит.

## Откат

Immutable-тега нет, поэтому откат — по git:

```bash
cd /opt/<instance>
git log --oneline -n 5                # найти предыдущий хороший коммит
git checkout <prev-commit>
docker compose -p <instance> -f docker-compose.prod.yml --env-file .env build api migrate
docker compose -p <instance> -f docker-compose.prod.yml --env-file .env run --rm migrate   # если нужно
docker compose -p <instance> -f docker-compose.prod.yml --env-file .env up -d --no-build
curl -fsS https://<SERVICE_DOMAIN>/healthz
# вернуться на ветку, когда фикс готов: git checkout main && git pull
```

**Схема НЕ откатывается.** Expand-only миграции сохраняют совместимость со старым кодом — это и
делает откат кода безопасным без `downgrade`.

## Секреты

Все секреты (`POSTGRES_PASSWORD`, JWT-ключи, `ADMIN_API_SECRET` (+ `ADMIN_API_SECRET_PREV` на
время ротации), `ADAPTY_WEBHOOK_SECRET`, `CLOUDPAYMENTS_API_TOKEN`, `METRICS_SCRAPE_TOKEN`,
`*_TEST_SECRET` — только вне прода) берутся из secret manager сервера: никогда из
закоммиченного файла и никогда не запекаются в образ. В проде они лежат в `.env` в
`/opt/<instance>` (gitignored) и подаются в контейнеры через `env_file`.

**Не секреты:** `SERVICE_NAME`, `SERVICE_DOMAIN`, `TRAEFIK_CERTRESOLVER`, `PRODUCTS` — это конфиг.
`PRODUCTS` обязана совпадать с продуктами, реально настроенными в App Store Connect / Adapty /
у агрегатора: продукт вне карты → `rejected`, 0 кредитов (fail-closed).

`.env.example` и `.env.prod.example` — только плейсхолдеры, никогда реальные значения.

### Чек-лист перед коммитом (обязателен)

```bash
# Каждый путь ДОЛЖЕН быть отмечен как ignored (непустой вывод + exit 0).
git check-ignore -v .env .secrets/jwt_private.pem infra/observability/secrets/scrape_token
```

Если хоть один путь не игнорируется — СТОП, не коммитить. Никогда не делать `git add -f` на
`.env*` (кроме `*.example`), `.secrets/`, `*.pem`, `*.key`, `*.cer`.
