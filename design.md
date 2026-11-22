# PulseNotify: Design and Project Structure

This document explains how the PulseNotify codebase is organized and how its parts work together. For setup and run commands, see [README.md](README.md).

## 1. What the system does

PulseNotify is a backend-only REST API for **flight price alerts**:

1. A user registers or logs in and gets a **JWT access token**.
2. The user creates **price alerts**, for example "tell me when DEL→BOM is ₹5000 or less."
3. Every 60 seconds, a **background job** checks current prices from a mock price feed.
4. When a price is at or below an alert's threshold, the system writes a **NotificationLog** row and marks the alert `triggered`.
5. Admins can call a **summary endpoint** for counts and the most popular routes.

There is no frontend and no real notification delivery (email, push). A "notification" is a database record.

## 2. Architecture at a glance

```
                ┌──────────────────────────────┐
  HTTP client   │  Django + DRF (runserver)    │
 (Postman, app) │  config/  +  pulse/          │
 ─────────────► │  /api/auth, /api/alerts,     │
   JWT Bearer   │  /api/admin, /api/flights    │
                └──────┬───────────────▲───────┘
                       │ ORM           │ HTTP GET /api/flights/price/
                       ▼               │ (worker calls the API itself)
                ┌─────────────┐  ┌─────┴──────────────┐
                │ PostgreSQL  │◄─┤  Celery worker     │
                │ (Docker)    │  │  pulse/tasks.py    │
                └─────────────┘  └─────▲──────────────┘
                                       │ tasks via broker
                ┌─────────────┐  ┌─────┴──────────────┐
                │ Redis       │◄─┤  Celery Beat       │
                │ (Docker)    │  │  every 60 s:       │
                │ broker +    │  │  check_prices      │
                │ results     │  └────────────────────┘
                └─────────────┘
```

Five processes run at the same time:

| Process | Started with | Role |
| --- | --- | --- |
| PostgreSQL 17 | `docker compose up -d` | Main data store |
| Redis 7 | `docker compose up -d` | Celery message broker (db 0) and result backend (db 1) |
| Django | `python manage.py runserver` | Serves the REST API, including the mock price feed |
| Celery worker | `celery -A config worker` | Runs `check_prices` and `send_notification` |
| Celery Beat | `celery -A config beat` | Schedules `check_prices` every 60 seconds |

**Note:** the worker gets prices over HTTP from Django's own `/api/flights/price/` endpoint (`INTERNAL_API_BASE_URL`). If Django isn't running, price checks fail and are logged as warnings. In a real deployment you would replace this endpoint with an external price provider.

## 3. Directory layout

```
pulsenotify/
├── manage.py                  # Django CLI; defaults to config.settings.local
├── requirements.txt           # Pinned dependencies
├── docker-compose.yml         # PostgreSQL + Redis (with healthchecks)
├── .env.example               # Template for environment variables (.env is gitignored)
├── README.md                  # Setup, run and API reference
├── design.md                  # This file
│
├── config/                    # Project-level wiring (no business logic)
│   ├── __init__.py            # Imports the Celery app so it loads with Django
│   ├── celery.py              # Celery app: reads CELERY_* settings, autodiscovers tasks
│   ├── urls.py                # Root URLs: mounts pulse.urls under /api/
│   ├── wsgi.py / asgi.py      # Server entry points; default to settings.production
│   └── settings/
│       ├── base.py            # Shared settings: DB, DRF, JWT, Celery, beat schedule
│       ├── local.py           # DEBUG=True, localhost only, dev fallback secret key
│       └── production.py      # Requires secrets; HTTPS/secure-cookie hardening
│
├── pulse/                     # The one Django app; all business logic lives here
│   ├── apps.py                # AppConfig; ready() registers signals
│   ├── models.py              # UserProfile, PriceAlert, NotificationLog
│   ├── signals.py             # Creates a UserProfile when a User is created
│   ├── serializers.py         # Input validation and output shapes
│   ├── permissions.py         # IsAdminUser (checks profile.role)
│   ├── views.py               # API endpoints + mock price table
│   ├── urls.py                # Routes for the app
│   ├── tasks.py               # Celery tasks: check_prices, send_notification
│   ├── tests.py               # Unit and API tests (27 tests)
│   └── migrations/0001_initial.py
│
└── postman/
    └── PulseNotify.postman_collection.json   # 13 end-to-end API scenarios
```

**Rule of thumb:** `config/` decides *how the project runs*. `pulse/` decides *what it does*.

## 4. Data model

```
 django.contrib.auth User
        │ 1
        ├──────────── 1  UserProfile        (role: admin | user)
        │
        │ 1
        └──────────── *  PriceAlert         (origin, destination, threshold_price, status)
                              │ 1
                              └──── *  NotificationLog  (triggered_price, message, notified_at)
```

| Model | Key fields | Notes |
| --- | --- | --- |
| `User` | Django built-in | Username and password auth |
| `UserProfile` | `user` (1:1), `role`, `created_at` | Created automatically by a `post_save` signal. `role` defaults to `user`. The API never lets clients set it; admins are promoted from the Django shell. |
| `PriceAlert` | `user` (FK), `origin`, `destination`, `threshold_price` (Decimal 10,2), `status`, `created_at` | `route` is a computed property: `"ORIGIN-DEST"` |
| `NotificationLog` | `alert` (FK), `triggered_price`, `message`, `notified_at` | One row per fired alert |

All foreign keys use `on_delete=CASCADE`: deleting a user removes their profile, alerts and notification logs.

### Alert lifecycle

```
            create                 price ≤ threshold
  (none) ─────────► ACTIVE ───────────────────────────► TRIGGERED
                      │          (send_notification)        │
                      │ DELETE /api/alerts/<id>/            │ DELETE
                      ▼                                     ▼
                   INACTIVE ◄────────────────────────────────
```

- Only `ACTIVE` alerts are checked.
- An alert fires **once**. After that it stays `TRIGGERED`, and nothing re-arms it.
- "Delete" is a **soft delete**: it sets `status = inactive` and keeps the row.

## 5. Request flow (API layer)

Every request goes through:

```
config/urls.py  →  pulse/urls.py  →  view in pulse/views.py
                                        ├─ authentication: SimpleJWT (Bearer token)
                                        ├─ permission:     IsAuthenticated by default
                                        ├─ serializer:     validate input / shape output
                                        └─ ORM queries     → PostgreSQL
```

DRF defaults are set in `config/settings/base.py`: JWT authentication, `IsAuthenticated`, and JSON-only responses. Public endpoints opt out with `authentication_classes = []` and `AllowAny`.

| Endpoint | View | Auth | Behavior |
| --- | --- | --- | --- |
| `POST /api/auth/register/` | `RegisterView` | Public | `RegisterSerializer` creates the user (password ≥ 8 chars, hashed). The signal creates the profile. Returns `username`, `access`, `role`. |
| `POST /api/auth/login/` | `LoginView` | Public | `authenticate()`; returns a new access token, or 401 |
| `GET /api/alerts/` | `AlertListCreateView` | JWT | Only the caller's alerts, newest first |
| `POST /api/alerts/` | `AlertListCreateView` | JWT | Owner comes from `request.user`, never from the request body |
| `DELETE /api/alerts/<id>/` | `AlertDeactivateView` | JWT | Soft delete. The queryset is limited to the caller, so other users' alerts return **404** (their existence isn't revealed). |
| `GET /api/flights/price/?route=X-Y` | `FlightPriceView` | Public | Random price from the range in `MOCK_PRICES`; 404 for unknown routes |
| `GET /api/admin/summary/` | `AdminSummaryView` | JWT + `IsAdminUser` | Alert counts by status, total notifications, top 5 routes |

### Validation (`PriceAlertSerializer`)
- `origin` and `destination` are trimmed and upper-cased (`" del "` becomes `"DEL"`).
- `origin` must differ from `destination`.
- `threshold_price` must be greater than 0.
- `id`, `status` and `created_at` are read-only, so clients can't set `status`.

### Authorization model
- **Authentication:** stateless JWT access tokens (SimpleJWT), lifetime from `JWT_ACCESS_TOKEN_LIFETIME_MINUTES`. There is no refresh-token endpoint: clients log in again when the token expires.
- **Ownership:** enforced by filtering querysets on `user=request.user`.
- **Roles:** `pulse/permissions.py::IsAdminUser` checks `user.profile.role == "admin"`. A user without a profile is denied. This is separate from Django's `is_staff` / `is_superuser`.

## 6. Background processing (Celery)

Defined in `pulse/tasks.py` and scheduled in `CELERY_BEAT_SCHEDULE` in `config/settings/base.py`.

```
Celery Beat (every 60 s)
   │
   ▼
check_prices()
   ├─ load ACTIVE alerts
   ├─ for each distinct (origin, destination):
   │     fetch_price(route)  ── HTTP GET → /api/flights/price/?route=...
   │       └─ returns None on network/parse errors (logged, route skipped)
   │     for each active alert on that route:
   │        if price_meets_threshold(price, alert.threshold_price):
   │            send_notification.delay(alert.id, str(price))
   └─ returns number of notifications dispatched

send_notification(alert_id, price)          (separate task, runs on a worker)
   └─ transaction.atomic():
        ├─ SELECT ... FOR UPDATE on the alert (row lock)
        ├─ skip if alert is gone or no longer ACTIVE
        ├─ create NotificationLog
        └─ set alert.status = TRIGGERED
```

Design choices:
- **One price fetch per route, not per alert.** Many alerts on the same route share one HTTP call.
- **Fan-out:** `check_prices` only decides. Writing to the database happens in `send_notification`, so each alert is processed independently.
- **Idempotency and concurrency safety:** the row lock plus the "still ACTIVE?" check mean an alert can't be notified twice, even if two `send_notification` tasks run at once or a user deactivates the alert at the same moment.
- **Exact decimals:** prices pass between tasks as strings, so JSON serialization keeps the exact `Decimal` value.
- **Resilience:** a failing price call skips that route for this cycle and the rest continue.

## 7. Configuration

All configuration comes from environment variables, loaded from `.env` by `python-dotenv` in `base.py`.

| Settings module | Used by default by | Behavior |
| --- | --- | --- |
| `config.settings.local` | `manage.py`, Celery | `DEBUG=True`, localhost only, insecure fallback `SECRET_KEY` if none is set |
| `config.settings.production` | `wsgi.py`, `asgi.py` | Fails at startup if `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD` or `DJANGO_ALLOWED_HOSTS` is missing. Enables SSL redirect, secure cookies, proxy SSL header, `nosniff`. |

Override either default with `DJANGO_SETTINGS_MODULE`. The full variable list is in `.env.example` and the README.

The installed apps are kept small: `contenttypes`, `auth`, `rest_framework`, `pulse`. There is no Django admin site, sessions or static files. The project is a pure JSON API.

## 8. Testing

`pulse/tests.py` uses Django's `TestCase` against a temporary PostgreSQL test database. HTTP calls (`requests.get`) and Celery dispatch (`.delay`) are mocked, so Redis and the network aren't needed.

| Test class | Covers |
| --- | --- |
| `PriceThresholdTests` | Threshold comparison (<, =, >), `check_prices` dispatch, surviving request failures |
| `NotificationLogTests` | Notification creation, marking `TRIGGERED`, no duplicates, inactive or deleted alerts skipped |
| `AlertScopingTests` | Users only see and delete their own alerts, owner set from request, validation, auth required |
| `AuthTests` | Register (default role, duplicate username), login token works, bad credentials return 401 |
| `FlightPriceTests` | Known route price within range, unknown route returns 404 |
| `AdminSummaryTests` | 401/403 cases, user without profile, aggregated numbers |

The Postman collection in `postman/` covers the same API end to end against a running server (13 scenarios).

## 9. Where to make common changes

| I want to… | Edit |
| --- | --- |
| Add a new endpoint | `pulse/views.py` + `pulse/urls.py` (+ a serializer in `pulse/serializers.py`) |
| Add or change a database field | `pulse/models.py`, then `python manage.py makemigrations && python manage.py migrate` |
| Change alert validation rules | `PriceAlertSerializer` in `pulse/serializers.py` |
| Change how often prices are checked | `CELERY_BEAT_SCHEDULE` in `config/settings/base.py` |
| Use a real price provider | `fetch_price()` in `pulse/tasks.py` (and `INTERNAL_API_BASE_URL`) |
| Add mock routes | `MOCK_PRICES` in `pulse/views.py` |
| Actually send email or push notifications | `send_notification()` in `pulse/tasks.py`, after the `NotificationLog` is created |
| Change who counts as an admin | `pulse/permissions.py` |
| Change token lifetime | `JWT_ACCESS_TOKEN_LIFETIME_MINUTES` in `.env` |

## 10. Known limitations

These are current behaviors to keep in mind, not bugs:

- Alerts are **one-shot**. A triggered alert never fires again and can't be reactivated through the API.
- **No refresh tokens.** Login returns only an access token.
- **The price feed is mocked** and random. Each call returns a new random price in the route's range.
- **Notifications are only logged** in the database. Nothing is sent to the user, and there is no endpoint for users to read their own notifications.
- `DELETE /api/alerts/<id>/` returns **200** with `{"status": "inactive"}` rather than 204, because the row is kept.
- `check_prices` runs per route in sequence, which is fine for a few routes. With many routes you might fetch prices in parallel or in batches.
