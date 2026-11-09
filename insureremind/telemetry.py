import os


def setup_telemetry():
    """Start Application Insights (OpenTelemetry) when a connection string is configured.

    Called from wsgi.py only, so tests and management commands never start it.
    """
    os.environ.setdefault("OTEL_SERVICE_NAME", "insureremind")
    # Comma-separated regexes, re.search()ed against the request URL. Report filters put registration
    # numbers in the query string, so /reports/ requests are not traced at all.
    os.environ.setdefault("OTEL_PYTHON_DJANGO_EXCLUDED_URLS", "healthz,/reports/")
    if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        from azure.monitor.opentelemetry import configure_azure_monitor
        configure_azure_monitor()
        return True
    return False
