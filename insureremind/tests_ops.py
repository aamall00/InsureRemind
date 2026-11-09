"""Ops endpoints, storage switch and telemetry setup. Dummy data only; no real Azure/Meta calls."""
import os
import re
from io import StringIO
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import storages
from django.core.management import call_command
from django.test import TestCase, override_settings

from insureremind.settings import build_storages
from insureremind.telemetry import setup_telemetry
from notifications.models import Reminder
from notifications.scheduling import on_policy_created
from policies.tests import make_policy


class HealthzTests(TestCase):
    def test_ok_without_login(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"ok")

    def test_db_error_503(self):
        with mock.patch("insureremind.ops_views.connection") as conn:
            conn.cursor.side_effect = RuntimeError("down")
            r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 503)


@override_settings(CRON_TOKEN="s3cret-token", AGENT_NOTIFY_EMAIL="agent@example.com", AGENT_NOTIFY_PHONE="+919000000009",
                   REMINDER_DAYS=[30, 15, 7, 1])
class CronTests(TestCase):
    url = "/internal/cron/send-reminders/"

    def _due(self):
        p = make_policy(days=7)
        on_policy_created(p)
        return p

    def post(self, token="s3cret-token", query=""):
        headers = {"HTTP_X_CRON_TOKEN": token} if token is not None else {}
        return self.client.post(self.url + query, **headers)

    @override_settings(CRON_TOKEN="")
    def test_404_when_token_unset(self):
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(self.post(token="").status_code, 404)

    def test_403_wrong_or_missing(self):
        self.assertEqual(self.post(token="nope").status_code, 403)
        self.assertEqual(self.post(token=None).status_code, 403)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.url, HTTP_X_CRON_TOKEN="s3cret-token").status_code, 405)

    def test_sends_due_then_nothing_more(self):
        p = self._due()
        with mock.patch("notifications.backends.send_email", return_value=""), \
                mock.patch("notifications.backends.send_whatsapp", return_value="wamid.1"):
            r = self.post()
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual((data["sent"], data["failed"], data["skipped"]), (4, 0, 0))
            self.assertFalse(data["dry_run"])
            self.assertNotIn("example.com", r.content.decode())  # no recipients in the response
            again = self.post().json()
        self.assertEqual(again["sent"], 0)
        self.assertEqual(Reminder.objects.filter(policy=p, status="sent").count(), 4)

    def test_dry_run_changes_nothing(self):
        p = self._due()
        with mock.patch("notifications.backends.send") as s:
            r = self.post(query="?dry_run=1")
        s.assert_not_called()
        data = r.json()
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["due"], 4)
        self.assertEqual(Reminder.objects.filter(policy=p, status="pending", scheduled_for__lte=data["date"]).count(), 4)

    def test_command_output_unchanged(self):
        self._due()
        out = StringIO()
        with mock.patch("notifications.backends.send", return_value=""):
            call_command("send_reminders", stdout=out)
        self.assertEqual(out.getvalue().strip(), "Done: 4 sent, 0 failed, 0 skipped.")

    def test_failure_returns_500_without_details(self):
        with mock.patch("insureremind.ops_views.send_due_reminders", side_effect=RuntimeError("boom")):
            r = self.post()
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json(), {"error": "failed"})


class StorageSwitchTests(TestCase):
    def test_local_default(self):
        s = build_storages({}, testing=False)
        self.assertEqual(s["default"]["BACKEND"], "django.core.files.storage.FileSystemStorage")
        self.assertEqual(s["staticfiles"]["BACKEND"], "whitenoise.storage.CompressedManifestStaticFilesStorage")
        self.assertEqual(build_storages({}, testing=True)["staticfiles"]["BACKEND"],
                         "django.contrib.staticfiles.storage.StaticFilesStorage")

    def test_azure_managed_identity(self):
        env = {"STORAGE_BACKEND": "azure", "AZURE_STORAGE_ACCOUNT_NAME": "stdummy"}
        with mock.patch("azure.identity.DefaultAzureCredential") as cred:
            s = build_storages(env)
        d = s["default"]
        self.assertEqual(d["BACKEND"], "storages.backends.azure_storage.AzureStorage")
        o = d["OPTIONS"]
        self.assertEqual(o["account_name"], "stdummy")
        self.assertEqual(o["azure_container"], "documents")
        self.assertIs(o["overwrite_files"], False)
        self.assertIsNone(o["expiration_secs"])
        self.assertIs(o["token_credential"], cred.return_value)
        self.assertNotIn("connection_string", o)

    def test_azure_connection_string_and_container(self):
        env = {"STORAGE_BACKEND": "azure", "AZURE_STORAGE_ACCOUNT_NAME": "stdummy",
               "AZURE_STORAGE_CONNECTION_STRING": "UseDevelopmentStorage=true", "AZURE_STORAGE_CONTAINER": "docs2"}
        with mock.patch("azure.identity.DefaultAzureCredential") as cred:
            o = build_storages(env)["default"]["OPTIONS"]
        cred.assert_not_called()
        self.assertEqual(o["connection_string"], "UseDevelopmentStorage=true")
        self.assertEqual(o["azure_container"], "docs2")
        self.assertNotIn("token_credential", o)

    def test_invalid_configs(self):
        with self.assertRaises(ImproperlyConfigured):
            build_storages({"STORAGE_BACKEND": "s3"})
        with self.assertRaises(ImproperlyConfigured):
            build_storages({"STORAGE_BACKEND": "azure"})

    def test_azure_backend_is_constructible(self):
        env = {"STORAGE_BACKEND": "azure", "AZURE_STORAGE_ACCOUNT_NAME": "stdummy"}
        with mock.patch("azure.identity.DefaultAzureCredential"):
            cfg = build_storages(env, testing=True)
        with override_settings(STORAGES=cfg):
            self.assertEqual(type(storages["default"]).__name__, "AzureStorage")  # no network on construction


class TelemetryTests(TestCase):
    KEYS = ("OTEL_SERVICE_NAME", "OTEL_PYTHON_DJANGO_EXCLUDED_URLS", "APPLICATIONINSIGHTS_CONNECTION_STRING")

    def _clean_env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in self.KEYS}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_not_configured_without_connection_string(self):
        with self._clean_env(), mock.patch("azure.monitor.opentelemetry.configure_azure_monitor") as cfg:
            self.assertFalse(setup_telemetry())
        cfg.assert_not_called()

    def test_configured_with_connection_string(self):
        with self._clean_env(APPLICATIONINSIGHTS_CONNECTION_STRING="InstrumentationKey=dummy"), \
                mock.patch("azure.monitor.opentelemetry.configure_azure_monitor") as cfg:
            self.assertTrue(setup_telemetry())
        cfg.assert_called_once_with()

    def test_defaults_set_and_reports_excluded(self):
        with self._clean_env():
            setup_telemetry()
            self.assertEqual(os.environ["OTEL_SERVICE_NAME"], "insureremind")
            pats = os.environ["OTEL_PYTHON_DJANGO_EXCLUDED_URLS"].split(",")
        self.assertTrue(any(re.search(p, "http://x/reports/?vehicle=MH12AB1234") for p in pats))
        self.assertTrue(any(re.search(p, "http://x/healthz") for p in pats))
