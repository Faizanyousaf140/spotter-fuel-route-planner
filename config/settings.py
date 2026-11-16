"""
Django settings for the fuel route planner.

All environment-specific values come from environment variables (optionally
loaded from a local ``.env`` file). See ``.env.example`` for the full list.
"""

from pathlib import Path
import os

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return default if value in (None, "") else float(value)


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return default if value in (None, "") else int(value)


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "insecure-local-development-key-do-not-use-in-production"
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false. "
            "Copy .env.example to .env for local development."
        )

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "route_api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
        # Concurrent requests write RoutePlan rows: WAL lets reads continue during a write,
        # IMMEDIATE transactions avoid "database is locked" upgrade deadlocks.
        "OPTIONS": {
            "timeout": 20,
            "transaction_mode": "IMMEDIATE",
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}

# Cache for route geometry (keyed by start/finish). Database-backed so it is
# shared between worker processes and survives restarts. Computed plans are
# stored in the RoutePlan table (not the cache) so /route/<id>/map/ never 404s
# because of cache eviction.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "route_api_cache",
        "TIMEOUT": env_int("ROUTE_CACHE_SECONDS", 60 * 60 * 24 * 7),
        "OPTIONS": {"MAX_ENTRIES": 5000},
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
    "EXCEPTION_HANDLER": "route_api.exceptions.api_exception_handler",
    # Identify clients by REMOTE_ADDR only; a spoofed X-Forwarded-For cannot bypass the rate limit.
    "NUM_PROXIES": 0,
}

SECURE_CONTENT_TYPE_NOSNIFF = True
# OpenStreetMap's tile policy requires a Referer on tile requests from web pages; Django's
# default "same-origin" strips it (tiles answer 403 "Access blocked"). This sends only the
# origin (e.g. "http://127.0.0.1:8000/") to other sites, never full URLs.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
# HTTPS hardening: enable when served over HTTPS (off by default for local HTTP use).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 0)
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", False)

# --------------------------------------------------------------------------
# Domain configuration (fuel route planner)
# --------------------------------------------------------------------------
FUEL_PLANNER = {
    # Vehicle model required by the assignment.
    "VEHICLE_RANGE_MILES": env_float("VEHICLE_RANGE_MILES", 500.0),
    "VEHICLE_MPG": env_float("VEHICLE_MPG", 10.0),
    # Spacing of the resampled route polyline used for station matching.
    "ROUTE_SAMPLE_SPACING_MILES": env_float("ROUTE_SAMPLE_SPACING_MILES", 2.0),
    # Stations whose geocoded point is farther than this from the route
    # polyline (straight-line miles) are not considered.
    "MAX_OFF_ROUTE_MILES": env_float("MAX_OFF_ROUTE_MILES", 15.0),
    # Routing provider: "osrm" (no key) or "openrouteservice" (needs key).
    "ROUTING_PROVIDER": os.environ.get("ROUTING_PROVIDER", "osrm").strip().lower(),
    "OSRM_BASE_URL": os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/"),
    "ORS_BASE_URL": os.environ.get("ORS_BASE_URL", "https://api.heigit.org/openrouteservice").rstrip("/"),
    "ORS_API_KEY": os.environ.get("ORS_API_KEY", ""),
    "ROUTING_CONNECT_TIMEOUT": env_float("ROUTING_CONNECT_TIMEOUT", 5.0),
    "ROUTING_READ_TIMEOUT": env_float("ROUTING_READ_TIMEOUT", 15.0),
    # Hard limit on a whole provider call (connect + full body). requests' own timeouts
    # do not bound a server that trickles its response.
    "ROUTING_TOTAL_TIMEOUT": env_float("ROUTING_TOTAL_TIMEOUT", 30.0),
    # Minimum seconds between routing calls per process (OSRM demo policy: 1 req/s).
    "ROUTING_MIN_INTERVAL_SECONDS": env_float("ROUTING_MIN_INTERVAL_SECONDS", 1.0),
    # Douglas-Peucker tolerance (degrees) for the geometry returned to clients.
    "RESPONSE_GEOMETRY_TOLERANCE_DEG": env_float("RESPONSE_GEOMETRY_TOLERANCE_DEG", 0.0005),
    # Free-text geocoding fallback (only used when the offline Census
    # gazetteer cannot resolve the input).
    "GEOCODER_FALLBACK_ENABLED": env_bool("GEOCODER_FALLBACK_ENABLED", True),
    "NOMINATIM_BASE_URL": os.environ.get(
        "NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org"
    ).rstrip("/"),
    "GEOCODER_TIMEOUT": env_float("GEOCODER_TIMEOUT", 8.0),
    "HTTP_USER_AGENT": os.environ.get(
        "HTTP_USER_AGENT", "spotter-fuel-route-planner/1.0 (Django coding assessment)"
    ),
    "GEOCODE_CACHE_SECONDS": env_int("GEOCODE_CACHE_SECONDS", 60 * 60 * 24 * 30),
    # Per-client limit on POST /api/v1/route/ (protects the free routing/geocoding services).
    # DRF rate syntax, e.g. "60/min"; empty disables.
    "ROUTE_RATE_LIMIT": os.environ.get("ROUTE_RATE_LIMIT", "60/min"),
    "PLACES_REFERENCE_PATH": BASE_DIR / "data" / "reference" / "us_places.csv",
}

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "route_api": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
