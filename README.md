# PulseNotify

A Django REST Framework backend for flight price alerts. Users register, log in with JWT, and create price alerts for routes. A Celery Beat schedule checks a mock price feed every 60 seconds and records a `NotificationLog` (then marks the alert `triggered`) when a price drops to or below an alert's threshold. Admins can view an aggregated summary.

## Tech stack

Python 3.12+ · Django 5.2 · Django REST Framework · SimpleJWT · PostgreSQL 17 · Redis 7 · Celery + Celery Beat · Docker Compose

## Prerequisites

- Python 3.12 or newer (tested on 3.14)
- Docker with Docker Compose

## Setup

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # Windows (PowerShell): Copy-Item .env.example .env
```

Edit `.env` and set real values for `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD`. `.env` is gitignored; never commit it.

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Django secret key (required in production) |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts (required in production) |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` | Database connection; also used by Docker Compose |
| `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | Redis URLs for Celery |
| `INTERNAL_API_BASE_URL` | Base URL `check_prices` calls for the mock price feed |
| `PRICE_API_TIMEOUT_SECONDS` | HTTP timeout for that call |
| `JWT_ACCESS_TOKEN_LIFETIME_MINUTES` | Access token lifetime |

Settings live in `config/settings/` (`base.py`, `local.py`, `production.py`). `manage.py` and Celery default to `config.settings.local`; WSGI/ASGI default to `config.settings.production`. Override with the `DJANGO_SETTINGS_MODULE` environment variable.

If port 5432 is already in use on your machine, set `POSTGRES_PORT=5433` (or any free port) in `.env`. Docker Compose and Django both read it.

## Start PostgreSQL and Redis

```bash
docker compose up -d
docker compose ps        # wait until both services are "healthy"
```

Stop them with `docker compose down`. Data persists in the `postgres_data` volume; add `-v` to delete it.

## Migrations

```bash
python manage.py migrate
```

## Create an admin for testing

Every new user gets a `UserProfile` with role `user`. The API never accepts a role from clients. To make an admin, promote a profile from the shell:

```bash
python manage.py createsuperuser --username admin --email admin@example.com
python manage.py shell -c "from django.contrib.auth.models import User; p = User.objects.get(username='admin').profile; p.role = 'admin'; p.save()"
```

The same `shell` command works for any user registered through the API.

## Run the app (three terminals)

Activate the virtual environment in each terminal.

```bash
# Terminal 1: Django
python manage.py runserver

# Terminal 2: Celery worker (on Windows, add --pool=solo)
celery -A config worker --loglevel=info

# Terminal 3: Celery Beat (runs pulse.tasks.check_prices every 60 seconds)
celery -A config beat --loglevel=info
```

The worker calls `INTERNAL_API_BASE_URL/api/flights/price/`, so Django must be running for prices to be checked.

## Run tests

```bash
python manage.py test
```

The tests use the PostgreSQL service (Django creates and drops a `test_*` database). They mock HTTP calls and Celery dispatch, so Redis and the network aren't needed.

## API endpoints

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| POST | `/api/auth/register/` | Public | Register; returns `username`, `access`, `role` (201; 400 if the username exists) |
| POST | `/api/auth/login/` | Public | Returns a fresh `access` token (200; 401 on bad credentials) |
| POST | `/api/alerts/` | JWT | Create an alert: `origin`, `destination`, `threshold_price` |
| GET | `/api/alerts/` | JWT | List only your own alerts |
| DELETE | `/api/alerts/<id>/` | JWT | Soft-delete (status → `inactive`); 404 if not yours |
| GET | `/api/flights/price/?route=DEL-BOM` | Public | Mock price for `DEL-BOM`, `BLR-HYD`, `DEL-BLR`, `BOM-GOA`; 404 otherwise |
| GET | `/api/admin/summary/` | JWT + admin role | Alert/notification counts and top 5 routes (403 for regular users) |

Send JWTs as `Authorization: Bearer <access>`.

## Postman

1. In Postman, choose **Import** and select `postman/PulseNotify.postman_collection.json`.
2. Create an admin as described above. In the collection's **Variables** tab, set the current values of `admin_username` and `admin_password`. Don't save these values into an exported file.
3. With Django running, run the collection in order with the **Collection Runner**. `base_url` defaults to `http://127.0.0.1:8000`.

The collection covers all 13 scenarios: register (201/400), login (200/401), alert create/list/delete, deleting another user's alert (404), public price feed (200/404), admin summary (403/200), and a missing token (401). Test usernames and passwords are generated at run time, and tokens and alert IDs are stored in collection variables.

From the command line: `npx newman run postman/PulseNotify.postman_collection.json --env-var admin_username=admin --env-var admin_password=<password>`.
