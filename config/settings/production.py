import os

from .base import *  # noqa: F401,F403
from .base import ALLOWED_HOSTS, DATABASES

DEBUG = False

# Fail fast if required secrets are missing.
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DATABASES["default"]["PASSWORD"] = os.environ["POSTGRES_PASSWORD"]
if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be set in production.")

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "True") == "True"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
