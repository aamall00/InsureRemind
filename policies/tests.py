import os
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import access
from notifications import backends
from notifications.models import Reminder
from notifications.scheduling import on_policy_created
from policies import extraction
from policies.models import Owner, Policy, PolicyUpload, Vehicle
from policies.utils import normalize_phone, normalize_reg_no

User = get_user_model()
TMP_MEDIA = tempfile.mkdtemp()


def make_user(username, level):
    u = User.objects.create_user(username, f"{username}@example.com", "pw-Str0ng-123")
    access.set_access_level(u, level)
    return u


def make_policy(reg="MH12AB1234", days=40, phone="+919876543210", email="o@example.com", opt_in=True, **kw):
    owner = Owner.objects.create(name="Test Owner", phone=phone, email=email, whatsapp_opt_in=opt_in)
    vehicle = Vehicle.objects.create(registration_no=reg, owner=owner)
    today = timezone.localdate()
    p = Policy.objects.create(
        vehicle=vehicle, insurer="ACME", policy_number=kw.pop("policy_number", "P1"), premium=Decimal("1000"),
        start_date=today + timedelta(days=days - 365), expiry_date=today + timedelta(days=days), **kw)
    return p


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class Base(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def setUp(self):
        self.ro = make_user("ro", access.READ_ONLY)
        self.rw = make_user("rw", access.READ_WRITE)
        self.admin = make_user("adm", access.ADMIN)


class NormalisationTests(TestCase):
    def test_reg_no(self):
        self.assertEqual(normalize_reg_no("mh 12 ab-1234"), "MH12AB1234")
        self.assertEqual(normalize_reg_no(" mh.12.ab 1234 "), "MH12AB1234")
        self.assertEqual(normalize_reg_no(None), "")

    def test_vehicle_save_normalises(self):
        o = Owner.objects.create(name="x", phone="+919876543210")
        v = Vehicle.objects.create(registration_no="mh 12-ab.1234", owner=o)
        self.assertEqual(v.registration_no, "MH12AB1234")

    def test_phone(self):
        for raw in ["98765 43210", "09876543210", "+91-98765-43210", "919876543210", "0091 98765 43210"]:
            self.assertEqual(normalize_phone(raw), "+919876543210", raw)
        self.assertEqual(normalize_phone("+1 415 555 2671"), "+14155552671")

    def test_phone_invalid(self):
        for raw in ["", "12345", "abc", "+91 12345"]:
            with self.assertRaises(ValidationError):
                normalize_phone(raw)


class AuthAndPermissionTests(Base):
    def test_login_required(self):
        for name in ["dashboard", "vehicle_list", "upload", "reminder_list", "user_list"]:
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("/accounts/login/", r["Location"])

    def test_groups_exist_with_permissions(self):
        ro = Group.objects.get(name=access.READ_ONLY).permissions.values_list("codename", flat=True)
        rw = Group.objects.get(name=access.READ_WRITE).permissions.values_list("codename", flat=True)
        self.assertIn("view_policy", ro)
        self.assertNotIn("add_policy", ro)
        self.assertIn("add_policy", rw)
        self.assertIn("change_vehicle", rw)
        self.assertNotIn("delete_policy", rw)

    def test_read_only_forbidden_on_writes(self):
        p = make_policy()
        self.client.force_login(self.ro)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("vehicle_detail", args=[p.vehicle.pk])).status_code, 200)
        for name, args in [("upload", []), ("vehicle_edit", [p.vehicle.pk]), ("owner_edit", [p.vehicle.owner.pk]),
                           ("policy_edit", [p.pk]), ("user_list", []), ("user_create", [])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403, name)
        self.assertEqual(self.client.post(reverse("upload"), {}).status_code, 403)

    def test_read_only_hides_write_buttons(self):
        p = make_policy()
        self.client.force_login(self.ro)
        html = self.client.get(reverse("vehicle_detail", args=[p.vehicle.pk])).content.decode()
        self.assertNotIn("Upload renewal", html)
        self.assertNotIn("/edit/", html)
        self.client.force_login(self.rw)
        html = self.client.get(reverse("vehicle_detail", args=[p.vehicle.pk])).content.decode()
        self.assertIn("Upload renewal", html)

    def test_non_admin_cannot_manage_users(self):
        self.client.force_login(self.rw)
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 403)
        self.assertEqual(self.client.post(reverse("user_create"), {}).status_code, 403)

    def test_admin_manage_users(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 200)
        r = self.client.post(reverse("user_create"), {
            "username": "newbie", "full_name": "New Bie", "email": "n@example.com", "phone": "98765 43210",
            "access_level": access.READ_WRITE, "password1": "Sup3r-secret-pw", "password2": "Sup3r-secret-pw"})
        self.assertEqual(r.status_code, 302)
        u = User.objects.get(username="newbie")
        self.assertEqual(access.access_level(u), access.READ_WRITE)
        self.assertEqual(u.profile.phone, "+919876543210")
        self.assertTrue(u.has_perm("policies.add_policy"))
        # edit level, reset password, deactivate
        self.client.post(reverse("user_edit", args=[u.pk]), {
            "full_name": "New Bie", "email": "n@example.com", "phone": "", "access_level": access.READ_ONLY})
        u = User.objects.get(pk=u.pk)
        self.assertEqual(access.access_level(u), access.READ_ONLY)
        self.assertFalse(u.has_perm("policies.add_policy"))
        self.client.post(reverse("user_password", args=[u.pk]), {"password1": "An0ther-pass-99", "password2": "An0ther-pass-99"})
        self.assertTrue(User.objects.get(pk=u.pk).check_password("An0ther-pass-99"))
        self.client.post(reverse("user_toggle_active", args=[u.pk]))
        self.assertFalse(User.objects.get(pk=u.pk).is_active)

    def test_admin_cannot_deactivate_self(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("user_toggle_active", args=[self.admin.pk]))
        self.assertTrue(User.objects.get(pk=self.admin.pk).is_active)

    def test_documents_are_auth_protected(self):
        p = make_policy()
        p.document.save("d.pdf", SimpleUploadedFile("d.pdf", b"%PDF-1.4 x"))
        url = reverse("policy_document", args=[p.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.ro)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")


GOOD_EXTRACTION = {
    "owner_name": "Asha Verma", "registration_no": "MH 12 AB 1234", "make": "Maruti", "model": "Swift",
    "variant": "VXI", "fuel_type": "Petrol", "manufacturing_year": 2020, "chassis_no": "CH123",
    "engine_no": "EN123", "insurer": "ICICI Lombard", "policy_number": "POL-001", "policy_type": "Package policy",
    "premium_total": 15234.5, "start_date": "2026-11-20", "expiry_date": "2027-11-19",
}


def upload_post(client, **over):
    data = {"vehicle_no": "mh 12 ab 1234", "email": "asha@example.com", "phone": "98765 43210",
            "whatsapp_consent": "on",
            "file": SimpleUploadedFile("pol.pdf", b"%PDF-1.4 fake", content_type="application/pdf")}
    data.update(over)
    return client.post(reverse("upload"), data)


def review_data(**over):
    d = {"owner_name": "Asha Verma", "make": "Maruti", "model": "Swift", "variant": "VXI", "fuel_type": "Petrol",
         "manufacturing_year": "2020", "chassis_no": "CH123", "engine_no": "EN123", "insurer": "ICICI Lombard",
         "policy_number": "POL-001", "policy_type": "comprehensive", "premium": "15234.50",
         "start_date": "2026-11-20", "expiry_date": "2027-11-19"}
    d.update(over)
    return d


class UploadFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.rw)

    @mock.patch("policies.views.extraction.extract_policy_data", return_value=GOOD_EXTRACTION)
    def test_full_flow_new_vehicle(self, m):
        r = upload_post(self.client)
        up = PolicyUpload.objects.get()
        self.assertRedirects(r, reverse("upload_review", args=[up.pk]))
        self.assertEqual(up.vehicle_no, "MH12AB1234")
        self.assertEqual(up.phone, "+919876543210")
        self.assertTrue(up.whatsapp_consent)
        page = self.client.get(reverse("upload_review", args=[up.pk]))
        self.assertContains(page, "Asha Verma")
        self.assertContains(page, "POL-001")
        self.assertNotContains(page, "Registration number mismatch")
        fr = self.client.get(reverse("upload_file", args=[up.pk]))
        self.assertEqual(fr.status_code, 200)
        fr.close()  # release the file handle (Windows can't delete open files)

        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse("upload_review", args=[up.pk]), review_data())
        v = Vehicle.objects.get(registration_no="MH12AB1234")
        self.assertRedirects(r, reverse("vehicle_detail", args=[v.pk]))
        up.refresh_from_db()
        self.assertEqual(up.status, PolicyUpload.CONFIRMED)
        p = v.policies.get()
        self.assertEqual(p.premium, Decimal("15234.50"))
        self.assertTrue(p.document.name.startswith("policies/"))
        self.assertEqual(p.extracted_data["policy_number"], "POL-001")
        self.assertEqual(v.owner.phone, "+919876543210")
        self.assertTrue(v.owner.whatsapp_opt_in)
        self.assertTrue(Reminder.objects.filter(policy=p).exists())
        # a second confirm of the same upload is a no-op redirect, not a duplicate
        self.client.post(reverse("upload_review", args=[up.pk]), review_data())
        self.assertEqual(Policy.objects.count(), 1)

    @mock.patch("policies.views.extraction.extract_policy_data", return_value={**GOOD_EXTRACTION, "registration_no": "KA01XY9999"})
    def test_mismatch_warning(self, m):
        upload_post(self.client)
        up = PolicyUpload.objects.get()
        self.assertContains(self.client.get(reverse("upload_review", args=[up.pk])), "Registration number mismatch")

    @mock.patch("policies.views.extraction.extract_policy_data", side_effect=extraction.ExtractionError("Boom failure"))
    def test_extraction_failure_manual_entry(self, m):
        upload_post(self.client)
        up = PolicyUpload.objects.get()
        self.assertEqual(up.extraction_error, "Boom failure")
        page = self.client.get(reverse("upload_review", args=[up.pk]))
        self.assertContains(page, "Boom failure")
        self.client.post(reverse("upload_review", args=[up.pk]), review_data(policy_number="MANUAL-1"))
        self.assertEqual(Policy.objects.get().policy_number, "MANUAL-1")

    def test_no_api_key_gives_banner(self):
        with override_settings(OPENROUTER_API_KEY=""):
            upload_post(self.client)
        up = PolicyUpload.objects.get()
        self.assertIn("OPENROUTER_API_KEY", up.extraction_error)
        self.assertContains(self.client.get(reverse("upload_review", args=[up.pk])), "Automatic reading failed")

    @mock.patch("policies.views.extraction.extract_policy_data", side_effect=RuntimeError("unexpected"))
    def test_unexpected_extraction_exception_does_not_crash(self, m):
        r = upload_post(self.client)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(PolicyUpload.objects.get().extraction_error)

    def test_upload_validation(self):
        bad = SimpleUploadedFile("x.exe", b"MZ", content_type="application/octet-stream")
        r = upload_post(self.client, file=bad)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PolicyUpload.objects.count(), 0)
        wrong = SimpleUploadedFile("x.pdf", b"x", content_type="image/png")
        self.assertEqual(upload_post(self.client, file=wrong).status_code, 200)
        r = upload_post(self.client, phone="123")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PolicyUpload.objects.count(), 0)

    @mock.patch("policies.views.extraction.extract_policy_data", return_value=GOOD_EXTRACTION)
    def test_renewal_skips_old_reminders(self, m):
        old = make_policy(reg="MH12AB1234", days=10, policy_number="OLD")
        on_policy_created(old)
        pending_before = Reminder.objects.filter(policy=old, status=Reminder.PENDING).count()
        self.assertGreater(pending_before, 0)
        upload_post(self.client)
        up = PolicyUpload.objects.get()
        page = self.client.get(reverse("upload_review", args=[up.pk]))
        self.assertContains(page, "Existing vehicle")
        self.client.post(reverse("upload_review", args=[up.pk]), review_data(owner_name="New Name"))
        old.refresh_from_db()
        self.assertEqual(Reminder.objects.filter(policy=old, status=Reminder.PENDING).count(), 0)
        self.assertEqual(Reminder.objects.filter(policy=old, status=Reminder.SKIPPED).count(), pending_before)
        v = Vehicle.objects.get()
        self.assertEqual(v.policies.count(), 2)
        self.assertEqual(Owner.objects.count(), 1)  # existing owner updated, not duplicated
        self.assertEqual(v.owner.name, "New Name")
        new = v.latest_policy
        self.assertEqual(new.policy_number, "POL-001")
        self.assertTrue(new.is_latest)
        self.assertFalse(old.is_latest)
        self.assertTrue(Reminder.objects.filter(policy=new, status=Reminder.PENDING).exists())

    def test_list_search_and_filter(self):
        make_policy(reg="MH12AB1234", days=5)
        make_policy(reg="KA01ZZ0001", days=200, phone="+919000000001")
        r = self.client.get(reverse("vehicle_list"), {"q": "mh 12-ab 1234"})
        self.assertContains(r, "MH12AB1234")
        self.assertNotContains(r, "KA01ZZ0001")
        r = self.client.get(reverse("vehicle_list"), {"status": "active"})
        self.assertContains(r, "KA01ZZ0001")
        self.assertNotContains(r, "MH12AB1234")
        r = self.client.get(reverse("vehicle_list"), {"q": "90000 00001"})
        self.assertContains(r, "KA01ZZ0001")


@override_settings(AGENT_NOTIFY_EMAIL="agent@example.com", AGENT_NOTIFY_PHONE="+919000000009",
                   REMINDER_DAYS=[30, 15, 7, 1])
class ReminderTests(TestCase):
    def test_schedule_creation(self):
        p = make_policy(days=20)
        on_policy_created(p)
        days = sorted(set(Reminder.objects.filter(policy=p).values_list("days_before", flat=True)))
        self.assertEqual(days, [1, 7, 15])  # 30-day mark is in the past -> skipped
        self.assertEqual(Reminder.objects.filter(policy=p).count(), 3 * 4)  # owner+agent x email+whatsapp
        r = Reminder.objects.get(policy=p, days_before=7, channel="email", recipient_type="owner")
        self.assertEqual(r.scheduled_for, p.expiry_date - timedelta(days=7))
        self.assertEqual(r.recipient, "o@example.com")
        on_policy_created(p)  # idempotent
        self.assertEqual(Reminder.objects.filter(policy=p).count(), 12)

    def test_no_opt_in_skips_owner_whatsapp(self):
        p = make_policy(days=40, opt_in=False)
        on_policy_created(p)
        wa = Reminder.objects.filter(policy=p, channel="whatsapp", recipient_type="owner")
        self.assertEqual(wa.count(), 4)
        self.assertTrue(all(r.status == Reminder.SKIPPED for r in wa))
        agent_wa = Reminder.objects.filter(policy=p, channel="whatsapp", recipient_type="agent")
        self.assertTrue(all(r.status == Reminder.PENDING for r in agent_wa))

    def test_unique_constraint(self):
        from django.db import IntegrityError, transaction
        p = make_policy(days=40)
        on_policy_created(p)
        r = Reminder.objects.first()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Reminder.objects.create(policy=p, channel=r.channel, recipient_type=r.recipient_type,
                                    days_before=r.days_before, scheduled_for=r.scheduled_for)

    def _due_policy(self):
        p = make_policy(days=7)
        on_policy_created(p)
        return p

    def test_send_reminders_idempotent(self):
        p = self._due_policy()
        due = Reminder.objects.filter(policy=p, scheduled_for__lte=timezone.localdate(), status="pending")
        n = due.count()
        self.assertEqual(n, 4)  # 7d mark is today: owner+agent x email+whatsapp
        with mock.patch("notifications.backends.send_email", return_value="") as em, \
                mock.patch("notifications.backends.send_whatsapp", return_value="wamid.1") as wa:
            call_command("send_reminders", stdout=StringIO())
            call_command("send_reminders", stdout=StringIO())
        self.assertEqual(em.call_count, 2)
        self.assertEqual(wa.call_count, 2)
        self.assertEqual(Reminder.objects.filter(policy=p, status="sent").count(), n)
        self.assertEqual(Reminder.objects.filter(policy=p, status="sent", channel="whatsapp").first().provider_message_id, "wamid.1")

    def test_dry_run_changes_nothing(self):
        p = self._due_policy()
        out = StringIO()
        with mock.patch("notifications.backends.send") as s:
            call_command("send_reminders", "--dry-run", stdout=out)
        s.assert_not_called()
        self.assertFalse(Reminder.objects.filter(policy=p, status="sent").exists())
        self.assertIn("dry-run", out.getvalue())

    def test_date_option(self):
        p = self._due_policy()
        with mock.patch("notifications.backends.send", return_value="") as s:
            call_command("send_reminders", "--date", str(timezone.localdate() + timedelta(days=6)), stdout=StringIO())
        self.assertEqual(s.call_count, 8)  # 7d and 1d marks (1d is due: expiry-1 = today+6)

    def test_failed_retried_on_later_runs_up_to_3(self):
        p = self._due_policy()
        Reminder.objects.filter(policy=p).exclude(channel="email", recipient_type="owner").update(status="skipped")
        today = timezone.localdate()
        with mock.patch("notifications.backends.send", side_effect=backends.SendError("nope")) as s:
            call_command("send_reminders", stdout=StringIO(), stderr=StringIO())
            call_command("send_reminders", stdout=StringIO(), stderr=StringIO())  # same day: no retry
            self.assertEqual(s.call_count, 1)
            for i in (1, 2, 3):  # later days
                call_command("send_reminders", "--date", str(today + timedelta(days=i)), stdout=StringIO(), stderr=StringIO())
        r = Reminder.objects.get(policy=p, channel="email", recipient_type="owner", days_before=7)
        self.assertEqual(r.attempts, 3)
        self.assertEqual(r.status, "failed")
        self.assertIn("nope", r.error)

    def test_superseded_not_sent(self):
        old = self._due_policy()
        newer = Policy.objects.create(vehicle=old.vehicle, insurer="N", policy_number="NEW", premium=1,
                                      start_date=old.expiry_date, expiry_date=old.expiry_date + timedelta(days=365))
        # simulate a missed skip (no on_policy_created call)
        with mock.patch("notifications.backends.send") as s:
            call_command("send_reminders", stdout=StringIO())
        s.assert_not_called()
        self.assertFalse(Reminder.objects.filter(policy=old, status="sent").exists())
        self.assertFalse(Reminder.objects.filter(policy=old, days_before=7, status="pending").exists())
        self.assertTrue(Reminder.objects.filter(policy=old, days_before=7, status="skipped").exists())

    def test_expired_policy_not_sent(self):
        p = make_policy(days=-3)
        Reminder.objects.create(policy=p, channel="email", recipient_type="owner", recipient="o@example.com",
                                days_before=1, scheduled_for=timezone.localdate() - timedelta(days=4))
        with mock.patch("notifications.backends.send") as s:
            call_command("send_reminders", stdout=StringIO())
        s.assert_not_called()

    def test_email_backend_content(self):
        p = make_policy(days=7, policy_number="X1")
        r = Reminder.objects.create(policy=p, channel="email", recipient_type="owner", recipient="o@example.com",
                                    days_before=7, scheduled_for=timezone.localdate())
        backends.send_email(r)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn("Insurance renewal due in 7 days", msg.subject)
        self.assertIn("MH12AB1234", msg.subject)
        self.assertEqual(msg.to, ["o@example.com"])
        self.assertTrue(msg.alternatives)

    @override_settings(WHATSAPP_BACKEND="meta", WHATSAPP_PHONE_NUMBER_ID="12345", WHATSAPP_ACCESS_TOKEN="tok",
                       WHATSAPP_TEMPLATE_NAME="tmpl", WHATSAPP_TEMPLATE_LANG="en", WHATSAPP_API_VERSION="v21.0")
    def test_meta_payload_shape(self):
        p = make_policy(days=7)
        r = Reminder.objects.create(policy=p, channel="whatsapp", recipient_type="owner", recipient="+919876543210",
                                    days_before=7, scheduled_for=timezone.localdate())
        resp = mock.Mock(status_code=200, text="ok")
        resp.json.return_value = {"messages": [{"id": "wamid.ABC"}]}
        with mock.patch("notifications.backends.requests.post", return_value=resp) as post:
            mid = backends.send(r)
        self.assertEqual(mid, "wamid.ABC")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://graph.facebook.com/v21.0/12345/messages")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(kwargs["timeout"], 15)
        body = kwargs["json"]
        self.assertEqual(body["messaging_product"], "whatsapp")
        self.assertEqual(body["to"], "919876543210")
        self.assertEqual(body["type"], "template")
        self.assertEqual(body["template"]["name"], "tmpl")
        self.assertEqual(body["template"]["language"], {"code": "en"})
        comp = body["template"]["components"][0]
        self.assertEqual(comp["type"], "body")
        texts = [x["text"] for x in comp["parameters"]]
        self.assertEqual(texts, ["Test Owner", "MH12AB1234", p.expiry_date.strftime("%d-%b-%Y"), "7", "ACME"])
        self.assertTrue(all(x["type"] == "text" for x in comp["parameters"]))

    @override_settings(WHATSAPP_BACKEND="meta", WHATSAPP_PHONE_NUMBER_ID="1", WHATSAPP_ACCESS_TOKEN="t")
    def test_meta_error_marks_failed(self):
        p = self._due_policy()
        Reminder.objects.filter(policy=p).exclude(channel="whatsapp", recipient_type="owner").update(status="skipped")
        resp = mock.Mock(status_code=400, text='{"error":"bad template"}')
        with mock.patch("notifications.backends.requests.post", return_value=resp):
            call_command("send_reminders", stdout=StringIO(), stderr=StringIO())
        r = Reminder.objects.get(policy=p, channel="whatsapp", recipient_type="owner", days_before=7)
        self.assertEqual(r.status, "failed")
        self.assertIn("bad template", r.error)

    def test_console_whatsapp(self):
        p = make_policy(days=7)
        r = Reminder.objects.create(policy=p, channel="whatsapp", recipient_type="owner", recipient="+919876543210",
                                    days_before=7, scheduled_for=timezone.localdate())
        with self.assertLogs("notifications.backends", level="INFO"):
            self.assertEqual(backends.send(r), "console")


SAMPLE_RAW = {
    "owner_name": "A K SHARMA", "registration_no": "GJ01AB1234", "make": "M & M", "model": "BOLERO B6",
    "variant": None, "fuel_type": None, "manufacturing_year": None,
    "chassis_no": "MA1TEST0000 K12345", "engine_no": "ENG0012345",
    "insurer": "ICICI Lombard", "policy_number": "3004/100000001/00/000", "policy_type": "Comprehensive Package",
    "premium_total": 18971.0, "net_premium": 16077.0, "gst_amount": 2894.0,
    "start_date": "2026-10-17", "expiry_date": "2027-10-16",
    "rto_location": "GUJARAT-AHMEDABAD", "registration_date": "2025-11-04",
    "vehicle_usage": "STAFF TRANSPORT", "ncb_percent": 20,
    "previous_policy_number": "213044/00/00/000001", "previous_insurer": "SGIC",
    "previous_policy_type": "Comprehensive Package", "previous_policy_start": "2025-10-17",
    "previous_policy_end": "2026-10-16", "previous_ncb_percent": 0, "previous_claims_count": 0,
    "_model": "anthropic/claude-opus-5.5",
}


def ok_response(content, model="anthropic/claude-opus-5.5"):
    import json as _json
    r = mock.Mock(status_code=200, text="ok")
    r.json.return_value = {"model": model, "choices": [{"message": {"content": content if isinstance(content, str) else _json.dumps(content)}}]}
    return r


def err_response(status, body="boom"):
    r = mock.Mock(status_code=status, text=body)
    r.json.return_value = {"error": {"message": body}}
    return r


LLM_TEST = {"base_url": "https://openrouter.ai/api/v1", "model": "m/primary", "fallback_models": ["m/fb1", "m/fb2"],
            "pdf_engine": "mistral-ocr", "timeout_seconds": 33, "max_tokens": 1234, "temperature": 0,
            "app_title": "InsureRemind", "app_url": "https://example.com"}


@override_settings(OPENROUTER_API_KEY="sk-or-test", LLM=LLM_TEST)
class ExtractionTests(TestCase):
    @override_settings(OPENROUTER_API_KEY="")
    def test_unavailable(self):
        with mock.patch("policies.extraction.requests.post") as post:
            with self.assertRaises(extraction.ExtractionUnavailable):
                extraction.extract_policy_data("a.pdf", b"x")
        post.assert_not_called()

    def test_pdf_payload(self):
        with mock.patch("policies.extraction.requests.post", return_value=ok_response(GOOD_EXTRACTION)) as post:
            data = extraction.extract_policy_data("pol.pdf", b"%PDF-1.4")
        args, kw = post.call_args
        self.assertEqual(args[0], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer sk-or-test")
        self.assertEqual(kw["headers"]["Content-Type"], "application/json")
        self.assertEqual(kw["headers"]["X-Title"], "InsureRemind")
        self.assertEqual(kw["headers"]["HTTP-Referer"], "https://example.com")
        self.assertEqual(kw["timeout"], 33)
        body = kw["json"]
        self.assertEqual(body["model"], "m/primary")
        self.assertEqual(body["models"], ["m/primary", "m/fb1", "m/fb2"])
        self.assertEqual(body["max_tokens"], 1234)
        self.assertEqual(body["temperature"], 0)
        part, text = body["messages"][0]["content"]
        self.assertEqual(part["type"], "file")
        self.assertEqual(part["file"]["filename"], "pol.pdf")
        self.assertTrue(part["file"]["file_data"].startswith("data:application/pdf;base64,"))
        self.assertEqual(text["type"], "text")
        self.assertEqual(text["text"], extraction.PROMPT)
        self.assertEqual(body["plugins"], [{"id": "file-parser", "pdf": {"engine": "mistral-ocr"}}])
        rf = body["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertEqual(rf["json_schema"]["name"], "policy_extraction")
        self.assertTrue(rf["json_schema"]["strict"])
        self.assertIs(rf["json_schema"]["schema"], extraction.SCHEMA)
        self.assertEqual(data["policy_number"], "POL-001")
        self.assertEqual(data["_model"], "anthropic/claude-opus-5.5")

    def test_image_payload_has_no_plugin(self):
        with mock.patch("policies.extraction.requests.post", return_value=ok_response(GOOD_EXTRACTION)) as post:
            extraction.extract_policy_data("scan.PNG", b"img")
        body = post.call_args.kwargs["json"]
        part = body["messages"][0]["content"][0]
        self.assertEqual(part["type"], "image_url")
        self.assertTrue(part["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertNotIn("plugins", body)

    def test_no_fallbacks_omits_models(self):
        cfg = {**LLM_TEST, "fallback_models": [], "app_url": ""}
        with override_settings(LLM=cfg), \
                mock.patch("policies.extraction.requests.post", return_value=ok_response(GOOD_EXTRACTION)) as post:
            extraction.extract_policy_data("a.jpg", b"i")
        self.assertNotIn("models", post.call_args.kwargs["json"])
        self.assertNotIn("HTTP-Referer", post.call_args.kwargs["headers"])

    def test_key_comes_from_settings(self):
        with override_settings(OPENROUTER_API_KEY="other-key"), \
                mock.patch("policies.extraction.requests.post", return_value=ok_response(GOOD_EXTRACTION)) as post:
            extraction.extract_policy_data("a.pdf", b"x")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer other-key")

    def test_code_fence_parsed(self):
        import json as _json
        fenced = "```json\n" + _json.dumps(GOOD_EXTRACTION) + "\n```"
        with mock.patch("policies.extraction.requests.post", return_value=ok_response(fenced)):
            self.assertEqual(extraction.extract_policy_data("a.pdf", b"x")["policy_number"], "POL-001")

    def test_error_statuses(self):
        cases = {401: "API key", 403: "API key", 402: "credits", 429: "rate-limited", 500: "HTTP 500", 400: "HTTP 400"}
        for status, snippet in cases.items():
            with mock.patch("policies.extraction.requests.post", return_value=err_response(status)):
                with self.assertRaises(extraction.ExtractionError) as cm:
                    extraction.extract_policy_data("a.pdf", b"x")
            self.assertIn(snippet, str(cm.exception), status)

    def test_network_errors(self):
        import requests
        for exc in (requests.Timeout("t"), requests.ConnectionError("c")):
            with mock.patch("policies.extraction.requests.post", side_effect=exc):
                with self.assertRaisesMessage(extraction.ExtractionError, "Could not reach"):
                    extraction.extract_policy_data("a.pdf", b"x")

    def test_bad_bodies(self):
        bad_bodies = [{"id": "x"}, {"error": {"message": "upstream"}}, {"choices": []}]
        for body in bad_bodies:
            r = mock.Mock(status_code=200, text=str(body))
            r.json.return_value = body
            with mock.patch("policies.extraction.requests.post", return_value=r):
                with self.assertRaisesMessage(extraction.ExtractionError, "unusable"):
                    extraction.extract_policy_data("a.pdf", b"x")
        with mock.patch("policies.extraction.requests.post", return_value=ok_response("not json at all")):
            with self.assertRaises(extraction.ExtractionError):
                extraction.extract_policy_data("a.pdf", b"x")

    def test_error_body_logged_truncated(self):
        with mock.patch("policies.extraction.requests.post", return_value=err_response(500, "x" * 2000)):
            with self.assertLogs("policies.extraction", level="WARNING") as lg:
                with self.assertRaises(extraction.ExtractionError):
                    extraction.extract_policy_data("a.pdf", b"x")
        self.assertIn("x" * 500, lg.output[0])
        self.assertNotIn("x" * 501, lg.output[0])

    def test_prompt_and_schema(self):
        self.assertIn("Previous Policy", extraction.PROMPT)
        self.assertIn("CURRENT policy", extraction.PROMPT)
        self.assertIn("Total Premium Payable", extraction.PROMPT)
        self.assertEqual(list(extraction.SCHEMA["required"]), list(extraction.SCHEMA["properties"].keys()))
        self.assertFalse(extraction.SCHEMA["additionalProperties"])
        props = extraction.SCHEMA["properties"]
        # Anthropic structured outputs reject schemas with more than 16 union-typed params.
        unions = [k for k, v in props.items() if isinstance(v.get("type"), list) or "anyOf" in v]
        self.assertLessEqual(len(unions), 16, unions)
        for k in ("net_premium", "gst_amount", "ncb_percent", "previous_ncb_percent"):
            self.assertEqual(props[k]["type"], ["number", "null"])
        self.assertEqual(props["previous_claims_count"]["type"], ["integer", "null"])
        for k in ("rto_location", "registration_date", "vehicle_usage", "previous_policy_start", "previous_policy_end"):
            self.assertIn(k, props)

    def test_clean_sample(self):
        c = extraction.clean_extracted(SAMPLE_RAW)
        self.assertEqual(c["owner_name"], "A K SHARMA")
        self.assertEqual(c["registration_no"], "GJ01AB1234")
        self.assertEqual(c["chassis_no"], "MA1TEST0000K12345")
        self.assertEqual(c["engine_no"], "ENG0012345")
        self.assertEqual((c["make"], c["model"]), ("M & M", "BOLERO B6"))
        self.assertEqual(c["net_premium"], Decimal("16077.00"))
        self.assertEqual(c["premium"], Decimal("18971.00"))
        self.assertEqual(c["gst_amount"], Decimal("2894.00"))
        self.assertEqual(c["ncb_percent"], Decimal("20.00"))
        self.assertEqual(str(c["start_date"]), "2026-10-17")
        self.assertEqual(str(c["expiry_date"]), "2027-10-16")
        self.assertEqual(str(c["registration_date"]), "2025-11-04")
        self.assertEqual(str(c["previous_policy_end"]), "2026-10-16")
        self.assertEqual(c["previous_ncb_percent"], Decimal("0.00"))
        self.assertEqual(c["previous_claims_count"], 0)
        self.assertEqual(c["previous_insurer"], "SGIC")
        self.assertEqual(c["vehicle_usage"], "STAFF TRANSPORT")
        self.assertEqual(c["warnings"], [])
        self.assertNotIn("_model", c)

    def test_clean_splits_combined_make_model_and_warns_on_swap(self):
        c = extraction.clean_extracted({"make": "M & M / BOLERO B6", "net_premium": 20000, "premium_total": 18971})
        self.assertEqual((c["make"], c["model"]), ("M & M", "BOLERO B6"))
        self.assertEqual(len(c["warnings"]), 1)
        self.assertEqual(c["net_premium"], Decimal("20000.00"))  # values kept

    def test_clean_defensive(self):
        c = extraction.clean_extracted({"start_date": "garbage", "expiry_date": "2027-02-30",
                                        "premium_total": "abc", "manufacturing_year": "1800",
                                        "registration_no": "mh 12 ab 1234", "policy_type": "Liability Only"})
        self.assertIsNone(c["start_date"])
        self.assertIsNone(c["expiry_date"])
        self.assertIsNone(c["premium"])
        self.assertIsNone(c["manufacturing_year"])
        self.assertEqual(c["registration_no"], "MH12AB1234")
        self.assertEqual(c["policy_type"], "third_party")
        self.assertEqual(extraction.clean_extracted(None)["owner_name"], "")


class LLMConfigTests(TestCase):
    def test_settings_load_toml(self):
        from django.conf import settings
        self.assertEqual(settings.LLM["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(settings.LLM["pdf_engine"], "native")
        self.assertIsInstance(settings.LLM["fallback_models"], list)
        self.assertFalse(hasattr(settings, "ANTHROPIC_API_KEY"))

    def test_model_env_override_and_path(self):
        from insureremind.llmconfig import load_llm_config
        self.assertEqual(load_llm_config({"OPENROUTER_MODEL": "x/y"})["model"], "x/y")
        self.assertTrue(load_llm_config({})["model"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = os.path.join(tmp.name, "alt.toml")
        with open(p, "w") as f:
            f.write('[openrouter]\nmodel = "alt/model"\nfallback_models = []\n')
        cfg = load_llm_config({"LLM_CONFIG_PATH": p})
        self.assertEqual(cfg["model"], "alt/model")
        self.assertEqual(cfg["timeout_seconds"], 120)  # default filled in
        self.assertEqual(load_llm_config({"LLM_CONFIG_PATH": p, "OPENROUTER_MODEL": "z/z"})["model"], "z/z")


class NewFieldsFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.rw)

    def test_upload_review_confirm_stores_new_fields(self):
        with mock.patch("policies.views.extraction.extract_policy_data", return_value=SAMPLE_RAW):
            upload_post(self.client, vehicle_no="GJ01AB1234")
        up = PolicyUpload.objects.get()
        page = self.client.get(reverse("upload_review", args=[up.pk]))
        for h in ("Insured &amp; Vehicle Details", "Current Policy &amp; Premium", "Previous Policy Details"):
            self.assertContains(page, h)
        self.assertContains(page, "MA1TEST0000K12345")
        self.assertContains(page, "Total premium (incl. GST)")
        self.assertNotContains(page, "Registration number mismatch")
        data = review_data(
            owner_name="A K SHARMA", make="M & M", model="BOLERO B6", chassis_no="MA1TEST0000 K12345",
            engine_no="ENG0012345", rto_location="GUJARAT-AHMEDABAD", registration_date="2025-11-04",
            vehicle_usage="STAFF TRANSPORT", policy_number="3004/100000001/00/000", start_date="2026-10-17",
            expiry_date="2027-10-16", net_premium="16077.00", gst_amount="2894.00", premium="18971.00",
            ncb_percent="20", previous_policy_number="213044/00/00/000001", previous_insurer="SGIC",
            previous_policy_type="Comprehensive Package", previous_policy_start="2025-10-17",
            previous_policy_end="2026-10-16", previous_ncb_percent="0", previous_claims_count="0")
        self.client.post(reverse("upload_review", args=[up.pk]), data)
        v = Vehicle.objects.get(registration_no="GJ01AB1234")
        self.assertEqual(v.rto_location, "GUJARAT-AHMEDABAD")
        self.assertEqual(str(v.registration_date), "2025-11-04")
        self.assertEqual(v.vehicle_usage, "STAFF TRANSPORT")
        self.assertEqual(v.chassis_no, "MA1TEST0000K12345")
        p = v.policies.get()
        self.assertEqual(p.net_premium, Decimal("16077.00"))
        self.assertEqual(p.gst_amount, Decimal("2894.00"))
        self.assertEqual(p.premium, Decimal("18971.00"))
        self.assertEqual(p.ncb_percent, Decimal("20"))
        self.assertEqual(p.previous_policy_number, "213044/00/00/000001")
        self.assertEqual(p.previous_insurer, "SGIC")
        self.assertEqual(str(p.previous_policy_end), "2026-10-16")
        self.assertEqual(p.previous_ncb_percent, Decimal("0"))
        self.assertEqual(p.previous_claims_count, 0)
        html = self.client.get(reverse("vehicle_detail", args=[v.pk])).content.decode()
        for needle in ("STAFF TRANSPORT", "₹16,077.00", "₹2,894.00", "₹18,971.00", "SGIC", "213044/00/00/000001"):
            self.assertIn(needle, html)

    def test_review_warning_previous_end_after_start(self):
        raw = {**SAMPLE_RAW, "previous_policy_end": "2026-12-01"}
        with mock.patch("policies.views.extraction.extract_policy_data", return_value=raw):
            upload_post(self.client, vehicle_no="GJ01AB1234")
        up = PolicyUpload.objects.get()
        self.assertContains(self.client.get(reverse("upload_review", args=[up.pk])), "previous policy ends on or after")

    def test_edit_forms_save_new_fields(self):
        p = make_policy()
        r = self.client.post(reverse("vehicle_edit", args=[p.vehicle.pk]), {
            "registration_no": "mh 12 ab 1234", "rto_location": "MH-12", "registration_date": "2020-01-02",
            "vehicle_usage": "Private", "make": "", "model": "", "variant": "", "fuel_type": "",
            "chassis_no": "", "engine_no": ""})
        self.assertEqual(r.status_code, 302)
        v = Vehicle.objects.get(pk=p.vehicle.pk)
        self.assertEqual((v.rto_location, v.vehicle_usage, str(v.registration_date)), ("MH-12", "Private", "2020-01-02"))
        start, exp = p.start_date, p.expiry_date
        r = self.client.post(reverse("policy_edit", args=[p.pk]), {
            "insurer": "ACME", "policy_number": "P1", "policy_type": "comprehensive", "start_date": start,
            "expiry_date": exp, "net_premium": "900", "gst_amount": "162", "premium": "1062", "ncb_percent": "25",
            "previous_policy_number": "OLD1", "previous_insurer": "SGIC", "previous_policy_type": "TP",
            "previous_policy_start": "2024-01-01", "previous_policy_end": "2024-12-31",
            "previous_ncb_percent": "20", "previous_claims_count": "1"})
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        p.refresh_from_db()
        self.assertEqual((p.net_premium, p.gst_amount, p.ncb_percent), (Decimal("900"), Decimal("162"), Decimal("25")))
        self.assertEqual((p.previous_policy_number, p.previous_claims_count), ("OLD1", 1))


class SeedTests(TestCase):
    def test_seed_demo(self):
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(Vehicle.objects.count(), 5)
        call_command("seed_demo", stdout=StringIO())  # idempotent
        self.assertEqual(Vehicle.objects.count(), 5)
        call_command("send_reminders", "--dry-run", stdout=StringIO())
