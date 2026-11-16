import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()

# Load the place gazetteer and station index once per process at startup
# (also used by `runserver`). Disable with WARMUP_ON_START=false.
if os.environ.get("WARMUP_ON_START", "true").strip().lower() in {"1", "true", "yes", "on"}:
    from route_api.warmup import warm_up

    warm_up()
