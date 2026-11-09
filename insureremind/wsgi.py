import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "insureremind.settings")

from insureremind.telemetry import setup_telemetry  # noqa: E402

setup_telemetry()
application = get_wsgi_application()
