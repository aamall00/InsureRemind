"""Reports page / exports and document downloads. Dummy data only."""
import io
import shutil
import tempfile
import zipfile
from datetime import date, timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from policies import tests as base
from policies.models import Owner, Policy, Vehicle
from policies.reports import filtered_policies

MEDIA = tempfile.mkdtemp()


def mk(reg, owner="Dummy Owner", expiry=date(2030, 5, 15), start=None, number=None, net="1000", gst="180",
       total="1180", vehicle=None, phone="+919000000001"):
    if vehicle is None:
        o = Owner.objects.create(name=owner, phone=phone, email="d@example.com")
        vehicle = Vehicle.objects.create(registration_no=reg, owner=o, make="Maker", model="Model")
    return Policy.objects.create(
        vehicle=vehicle, insurer="Dummy Ins", policy_number=number or f"PN/{reg}/{expiry:%m%d}",
        premium=Decimal(total), net_premium=Decimal(net), gst_amount=Decimal(gst), ncb_percent=Decimal("20"),
        start_date=start or expiry - timedelta(days=364), expiry_date=expiry)


def numbers(qs):
    return sorted(qs.values_list("policy_number", flat=True))


@override_settings(MEDIA_ROOT=MEDIA)
class ReportBase(base.Base):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)


class FilterTests(ReportBase):
    def setUp(self):
        super().setUp()
        self.a = mk("MH12AB1234", owner="Alice Anand", expiry=date(2030, 5, 1), number="A")
        self.b = mk("MH12CD9999", owner="Bob Brown", expiry=date(2030, 5, 31), number="B")
        self.c = mk("KA01EF0001", owner="alice Kumar", expiry=date(2030, 6, 1), number="C")
        self.d = mk("KA01EF0002", owner="Dan", expiry=date(2030, 4, 30), number="D")

    def f(self, **p):
        p.setdefault("f", "1")
        return numbers(filtered_policies(p)[0])

    def test_owner_contains_case_insensitive(self):
        self.assertEqual(self.f(owner="ALICE"), ["A", "C"])

    def test_vehicle_partial_normalised(self):
        self.assertEqual(self.f(vehicle="mh 12"), ["A", "B"])
        self.assertEqual(self.f(vehicle="ka-01.ef0002"), ["D"])

    def test_month_boundaries(self):
        self.assertEqual(self.f(month="2030-05"), ["A", "B"])  # first and last day in; Apr 30 and Jun 1 out
        self.assertEqual(self.f(month="2030-02"), [])

    def test_combined(self):
        self.assertEqual(self.f(owner="alice", vehicle="mh", month="2030-05"), ["A"])

    def test_latest_toggle(self):
        v = self.a.vehicle
        mk(None, vehicle=v, expiry=date(2031, 5, 1), number="A2")
        self.assertEqual(self.f(vehicle="MH12AB1234", latest="on"), ["A2"])
        self.assertEqual(self.f(vehicle="MH12AB1234"), ["A", "A2"])  # f=1 present, box unticked
        self.assertEqual(numbers(filtered_policies({"vehicle": "MH12AB1234"})[0]), ["A2"])  # default on

    def test_order_by_expiry(self):
        qs = filtered_policies({"f": "1"})[0]
        self.assertEqual([p.policy_number for p in qs], ["D", "A", "B", "C"])

    def test_invalid_month_is_form_error(self):
        self.client.force_login(self.ro)
        r = self.client.get(reverse("report"), {"f": "1", "month": "garbage"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "YYYY-MM")
        for name in ("report_xlsx", "report_pdf"):
            self.assertEqual(self.client.get(reverse(name), {"month": "13-2030"}).status_code, 200)


class PageTests(ReportBase):
    def test_access(self):
        self.assertEqual(self.client.get(reverse("report")).status_code, 302)
        for name in ("report_xlsx", "report_pdf"):
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 302)
            self.assertIn("/accounts/login/", r["Location"])
        self.client.force_login(self.ro)
        self.assertEqual(self.client.get(reverse("report")).status_code, 200)
        self.assertEqual(self.client.get(reverse("report_xlsx")).status_code, 200)
        self.assertEqual(self.client.get(reverse("report_pdf")).status_code, 200)
        self.assertContains(self.client.get(reverse("dashboard")), "Reports")

    def test_page_totals_all_rows_and_empty_state(self):
        for i in range(55):
            mk(f"TN01AA{i:04d}", expiry=date(2030, 1, 1) + timedelta(days=i), number=f"N{i}")
        self.client.force_login(self.ro)
        r = self.client.get(reverse("report"))
        self.assertEqual(len(r.context["page"]), 50)
        self.assertEqual(r.context["count"], 55)
        self.assertEqual(r.context["totals"]["total"], Decimal("1180") * 55)
        self.assertEqual(r.context["totals"]["net"], Decimal("1000") * 55)
        self.assertContains(r, "Export Excel")
        self.assertContains(r, "/reports/export.xlsx?")
        r = self.client.get(reverse("report"), {"f": "1", "owner": "nobody-here"})
        self.assertContains(r, "No policies match")

    def test_query_count_is_constant(self):
        for i in range(10):
            mk(f"TN02AA{i:04d}", number=f"Q{i}")
        self.client.force_login(self.ro)
        with self.assertNumQueries(8):  # session, user, perms (x2), access-level badge, count, totals, page (no N+1)
            self.client.get(reverse("report"))


class ExportTests(ReportBase):
    def test_xlsx(self):
        from openpyxl import load_workbook
        for i in range(60):  # more than one page
            mk(f"GJ05AA{i:04d}", expiry=date(2030, 1, 1) + timedelta(days=i), number=f"X{i}")
        mk("GJ06ZZ0001", number="OTHER", expiry=date(2031, 1, 1))
        self.client.force_login(self.ro)
        r = self.client.get(reverse("report_xlsx"), {"f": "1", "vehicle": "GJ05", "latest": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r["Content-Disposition"], r'insureremind-report-\d{8}-\d{4}\.xlsx')
        ws = load_workbook(io.BytesIO(r.content)).active
        self.assertEqual(ws.title, "Policies")
        self.assertIn("Policy report", ws["A1"].value)
        self.assertIn("GJ05", ws["A2"].value)
        headers = [c.value for c in ws[4]]
        self.assertEqual(headers[:3], ["Vehicle no.", "Owner", "Phone"])
        self.assertIn("Total premium", headers)
        data_rows = [row for row in ws.iter_rows(min_row=5, values_only=True) if row[0] and row[0] != "Totals"]
        self.assertEqual(len(data_rows), 60)  # export ignores the 50-row page size
        self.assertIsInstance(data_rows[0][9], (date,))
        self.assertEqual(ws.cell(row=5, column=10).number_format, "dd-mmm-yyyy")
        self.assertEqual(ws.cell(row=5, column=15).number_format, "#,##0.00")
        self.assertIsInstance(ws.cell(row=5, column=15).value, (int, float))
        tot = [row for row in ws.iter_rows(min_row=5, values_only=True) if row[0] == "Totals"][0]
        self.assertEqual((tot[12], tot[13], tot[14]), (60000, 10800, 70800))
        self.assertEqual(ws.freeze_panes, "A5")
        self.assertTrue(ws.auto_filter.ref.startswith("A4:"))
        self.assertTrue(ws["A4"].font.bold)

    def test_pdf(self):
        for i in range(60):
            mk(f"GJ07AA{i:04d}", number=f"P{i}", owner="Owner & <Co>")
        self.client.force_login(self.ro)
        r = self.client.get(reverse("report_pdf"), {"f": "1"})
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertRegex(r["Content-Disposition"], r'insureremind-report-\d{8}-\d{4}\.pdf')
        empty = self.client.get(reverse("report_pdf"), {"f": "1", "owner": "zzz"})
        self.assertTrue(empty.content.startswith(b"%PDF"))


def attach(policy, content=b"%PDF-1.4 dummy", name="orig.pdf"):
    policy.document.save(name, SimpleUploadedFile(name, content), save=True)
    return policy


class DownloadTests(ReportBase):
    def setUp(self):
        super().setUp()
        self.old = mk("GJ01AB1234", expiry=date(2029, 10, 16), start=date(2028, 10, 17), number="3004/100/00/000")
        self.new = mk(None, vehicle=self.old.vehicle, expiry=date(2030, 10, 16), start=date(2029, 10, 17),
                      number="3004/200/00/000")
        attach(self.old, b"%PDF-old")
        attach(self.new, b"%PDF-new")
        other = mk("GJ99ZZ9999", number="OTHER/1")
        attach(other, b"%PDF-other")
        self.other = other
        self.url = reverse("policy_document", args=[self.new.pk])

    def test_download_vs_inline(self):
        self.client.force_login(self.ro)
        r = self.client.get(self.url + "?download=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Disposition"], 'attachment; filename="GJ01AB1234_3004-200-00-000_2029-10-17.pdf"')
        r.close()
        r = self.client.get(self.url)
        self.assertTrue(r["Content-Disposition"].startswith("inline"))
        self.assertEqual(r["Content-Type"], "application/pdf")
        r.close()

    def test_original_extension_kept(self):
        attach(self.new, b"\xff\xd8jpg", name="scan.JPG")
        self.client.force_login(self.ro)
        r = self.client.get(self.url + "?download=1")
        self.assertIn("_2029-10-17.jpg", r["Content-Disposition"])
        r.close()

    def test_auth_required(self):
        for u in (self.url, self.url + "?download=1", reverse("vehicle_documents_zip", args=[self.old.vehicle.pk])):
            r = self.client.get(u)
            self.assertEqual(r.status_code, 302)
            self.assertIn("/accounts/login/", r["Location"])

    def test_zip(self):
        self.client.force_login(self.ro)
        r = self.client.get(reverse("vehicle_documents_zip", args=[self.old.vehicle.pk]))
        self.assertEqual(r["Content-Type"], "application/zip")
        self.assertIn("GJ01AB1234_policies.zip", r["Content-Disposition"])
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertEqual(sorted(zf.namelist()), ["GJ01AB1234_3004-100-00-000_2028-10-17.pdf",
                                                 "GJ01AB1234_3004-200-00-000_2029-10-17.pdf"])
        self.assertEqual(zf.read("GJ01AB1234_3004-200-00-000_2029-10-17.pdf"), b"%PDF-new")

    def test_missing_file_404_and_zip_skips(self):
        import os
        os.remove(self.old.document.path)
        self.client.force_login(self.ro)
        self.assertEqual(self.client.get(reverse("policy_document", args=[self.old.pk]) + "?download=1").status_code, 404)
        r = self.client.get(reverse("vehicle_documents_zip", args=[self.old.vehicle.pk]))
        self.assertEqual(zipfile.ZipFile(io.BytesIO(r.content)).namelist(), ["GJ01AB1234_3004-200-00-000_2029-10-17.pdf"])
        page = self.client.get(reverse("vehicle_detail", args=[self.old.vehicle.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "No document")
        self.assertNotContains(page, "Download all policies")  # only one file left

    def test_detail_page_buttons_and_labels(self):
        self.client.force_login(self.ro)
        page = self.client.get(reverse("vehicle_detail", args=[self.old.vehicle.pk]))
        self.assertContains(page, "Current policy")
        self.assertContains(page, "Previous policy")
        self.assertContains(page, "Download all policies (ZIP)")
        self.assertContains(page, "?download=1", count=2)
        self.assertContains(page, "Latest")

    def test_policy_without_document(self):
        nodoc = mk("GJ02XX0001", number="ND")
        self.client.force_login(self.ro)
        page = self.client.get(reverse("vehicle_detail", args=[nodoc.vehicle.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "No document")
        self.assertNotContains(page, "Download all policies")
        self.assertEqual(self.client.get(reverse("policy_document", args=[nodoc.pk]) + "?download=1").status_code, 404)


class InrFilterTests(TestCase):
    def test_inr(self):
        from policies.templatetags.policy_tags import inr
        self.assertEqual(inr(0), "₹0.00")
        self.assertEqual(inr(999), "₹999.00")
        self.assertEqual(inr(1000), "₹1,000.00")
        self.assertEqual(inr(100000), "₹1,00,000.00")
        self.assertEqual(inr(1234567.5), "₹12,34,567.50")
        self.assertEqual(inr(Decimal("16077.00")), "₹16,077.00")
        self.assertEqual(inr(Decimal("-1234.5")), "-₹1,234.50")
        self.assertEqual(inr(None), "–")

    def test_pages_use_inr(self):
        p = mk("GJ03AA0001", net="1234567.50", gst="222222.15", total="1456789.65", number="INR1")
        u = base.make_user("inruser", "Read only")
        self.client.force_login(u)
        html = self.client.get(reverse("report"), {"f": "1"}).content.decode()
        self.assertIn("₹12,34,567.50", html)
        self.assertIn("₹14,56,789.65", html)
        html = self.client.get(reverse("vehicle_detail", args=[p.vehicle.pk])).content.decode()
        self.assertIn("₹12,34,567.50", html)


class PdfLayoutTests(TestCase):
    def test_nowrap_columns_fit_on_one_line(self):
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from policies.reports import PDF_PAD, PDF_SPEC, PDF_TYPE_LABELS
        samples = {0: "MH12AB1234", 2: "+919876543210", 6: "3004/100000001/00/000", 7: max(PDF_TYPE_LABELS.values(), key=len),
                   8: "27-Oct-2025", 9: "27-Sep-2027", 10: "-365", 12: "12,34,567.89", 13: "2,22,222.22",
                   14: "14,56,789.65", 15: "100"}
        for header, idx, width, _right, may_wrap in PDF_SPEC:
            if may_wrap:
                continue
            avail = width - 2 * PDF_PAD
            self.assertLessEqual(stringWidth(samples[idx], "Helvetica", 7), avail, header)
            self.assertLessEqual(stringWidth(header, "Helvetica-Bold", 7), avail, f"header {header}")
        self.assertLessEqual(sum(c[2] for c in PDF_SPEC), 794)

    def test_long_values_pdf_text_unbroken(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest("pypdf not installed")
        o = Owner.objects.create(name="Dummy Person Name", phone="+919876543210", email="d@example.com")
        v = Vehicle.objects.create(registration_no="MH12AB1234", owner=o)
        Policy.objects.create(vehicle=v, insurer="ICICI Lombard General Insurance Company Limited",
                              policy_number="3004/100000001/00/000", premium=Decimal("1456789.65"),
                              net_premium=Decimal("1234567.50"), gst_amount=Decimal("222222.15"),
                              start_date=date(2025, 10, 27), expiry_date=date(2026, 10, 26), policy_type="comprehensive")
        from policies.reports import build_pdf, filtered_policies
        qs, form = filtered_policies({"f": "1"})
        text = PdfReader(io.BytesIO(build_pdf(qs, form))).pages[0].extract_text()
        for token in ("+919876543210", "27-Oct-2025", "26-Oct-2026", "3004/100000001/00/000", "12,34,567.50", "Comp."):
            self.assertIn(token, text)
