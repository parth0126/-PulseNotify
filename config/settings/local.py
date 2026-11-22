from .base import *  # noqa: F401,F403
from .base import SECRET_KEY

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# Development-only fallback so the project runs before a secret is configured.
SECRET_KEY = SECRET_KEY or "django-insecure-local-development-only"
