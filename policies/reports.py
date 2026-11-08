"""Policy report: one shared filter function, an HTML page and Excel / PDF exports."""
import calendar
import io
from datetime import date, datetime
from decimal import Decimal

from django import forms
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View

from .models import Policy
from .utils import normalize_reg_no

PAGE_SIZE = 50
ZERO = Decimal("0.00")


class ReportFilterForm(forms.Form):
    owner = forms.CharField(required=False, label="Owner name", max_length=200)
    vehicle = forms.CharField(required=False, label="Vehicle number", max_length=30)
    month = forms.CharField(
        required=False, label="Expiry month",
        widget=forms.DateInput(attrs={"type": "month"}, format="%Y-%m"))
    latest = forms.BooleanField(required=False, label="Latest policy per vehicle only")

    def clean_month(self):
        raw = (self.cleaned_data.get("month") or "").strip()
        if not raw:
            return None
        try:
            d = datetime.strptime(raw, "%Y-%m")
        except ValueError:
            raise forms.ValidationError("Enter the expiry month as YYYY-MM.")
        return date(d.year, d.month, 1)


def _bind(params):
    """'latest' is on by default: a fresh visit has no 'f' marker; once the form is submitted
    (hidden f=1) an unticked checkbox really means off."""
    data = params.copy() if hasattr(params, "copy") else dict(params)
    if "f" not in data:
        data["latest"] = "on"
    return ReportFilterForm(data)


def filtered_policies(params):
    """Return (queryset, form). The single source of filtering for the page and both exports.

    An invalid form yields an empty queryset (the page shows the errors).
    """
    form = _bind(params)
    qs = Policy.objects.select_related("vehicle__owner")
    if not form.is_valid():
        return qs.none(), form
    cd = form.cleaned_data
    if cd["latest"]:
        qs = qs.latest_per_vehicle()
    if cd["owner"].strip():
        qs = qs.filter(vehicle__owner__name__icontains=cd["owner"].strip())
    reg = normalize_reg_no(cd["vehicle"])
    if reg:
        qs = qs.filter(vehicle__registration_no__contains=reg)
    if cd["month"]:
        first = cd["month"]
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        qs = qs.filter(expiry_date__gte=first, expiry_date__lte=last)
    return qs.order_by("expiry_date", "id"), form


def totals(qs):
    agg = qs.aggregate(net=Sum("net_premium"), gst=Sum("gst_amount"), total=Sum("premium"))
    return {k: v if v is not None else ZERO for k, v in agg.items()}


def describe_filters(form):
    if not form.is_valid():
        return "Invalid filters"
    cd = form.cleaned_data
    parts = []
    if cd["owner"].strip():
        parts.append(f"Owner contains '{cd['owner'].strip()}'")
    if normalize_reg_no(cd["vehicle"]):
        parts.append(f"Vehicle no. contains '{normalize_reg_no(cd['vehicle'])}'")
    if cd["month"]:
        parts.append(f"Expiry month {cd['month']:%b %Y}")
    parts.append("Latest policy per vehicle only" if cd["latest"] else "All policies (full history)")
    return "; ".join(parts)


COLUMNS = ["Vehicle no.", "Owner", "Phone", "Email", "Make / Model", "Insurer", "Policy no.", "Policy type",
           "Start", "Expiry", "Days left", "Status", "Net premium", "GST", "Total premium", "NCB %"]


def row_values(p):
    v, o = p.vehicle, p.vehicle.owner
    return [v.registration_no, o.name, o.phone, o.email, " ".join(x for x in (v.make, v.model) if x),
            p.insurer, p.policy_number, p.get_policy_type_display(), p.start_date, p.expiry_date,
            p.days_left, p.status_label, p.net_premium, p.gst_amount, p.premium, p.ncb_percent]


def _stamp():
    return timezone.localtime()


def _filename(ext):
    return f"insureremind-report-{_stamp():%Y%m%d-%H%M}.{ext}"


# ------------------------------------------------------------------ Excel
def build_xlsx(qs, form):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Policies"
    ncols = len(COLUMNS)
    ws.append(["InsureRemind — Policy report"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"Filters: {describe_filters(form)}. Generated {_stamp():%d-%b-%Y %H:%M}."])
    ws.append([])
    ws.append(COLUMNS)
    header_row = 4
    fill = PatternFill("solid", start_color="1F4E78")
    for c in range(1, ncols + 1):
        cell = ws.cell(row=header_row, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    rows = 0
    for p in qs:
        ws.append(row_values(p))
        rows += 1
        r = ws.max_row
        for col in (9, 10):
            ws.cell(row=r, column=col).number_format = "dd-mmm-yyyy"
        for col in (13, 14, 15, 16):
            ws.cell(row=r, column=col).number_format = "#,##0.00"

    t = totals(qs)
    tr = ws.max_row + 1
    ws.cell(row=tr, column=1, value="Totals")
    ws.cell(row=tr, column=11, value=f"{rows} policies")
    for col, key in ((13, "net"), (14, "gst"), (15, "total")):
        ws.cell(row=tr, column=col, value=t[key]).number_format = "#,##0.00"
    for c in range(1, ncols + 1):
        ws.cell(row=tr, column=c).font = Font(bold=True)

    widths = [14, 24, 15, 28, 22, 22, 24, 15, 12, 12, 9, 14, 14, 12, 14, 8]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(ncols)}{max(ws.max_row - 1, header_row)}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ PDF
PDF_PAD = 3
PDF_TYPE_LABELS = {"Comprehensive": "Comp.", "Third-party": "TP", "Own-damage": "OD", "Other": "Other"}
# (header, index into COLUMNS, width in points, right aligned, may_wrap). Usable width on landscape A4 = 794pt.
# Email and Make/Model are omitted from the PDF. Non-wrapping columns are sized to their content
# (checked by a test); Owner, Insurer and Status wrap at word boundaries.
PDF_SPEC = [("Vehicle no.", 0, 60, 0, False), ("Owner", 1, 62, 0, True), ("Phone", 2, 72, 0, False),
            ("Insurer", 5, 72, 0, True), ("Policy no.", 6, 105, 0, False), ("Type", 7, 38, 0, False),
            ("Start", 8, 58, 0, False), ("Expiry", 9, 58, 0, False), ("Days", 10, 30, 1, False),
            ("Status", 11, 46, 0, True), ("Net (Rs.)", 12, 54, 1, False), ("GST (Rs.)", 13, 48, 1, False),
            ("Total (Rs.)", 14, 56, 1, False), ("NCB %", 15, 34, 1, False)]
def build_pdf(qs, form):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    # Built-in Helvetica has no rupee glyph, so amount columns are labelled "(Rs.)" instead.
    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self.setFont("Helvetica", 8)
                self.drawRightString(landscape(A4)[0] - 24, 14, f"Page {self._pageNumber} of {total}")
                super().showPage()
            super().save()

    small = ParagraphStyle("small", fontName="Helvetica", fontSize=7, leading=8.5)
    small_r = ParagraphStyle("small_r", parent=small, alignment=2)
    head = ParagraphStyle("head", parent=small, fontName="Helvetica-Bold", textColor=colors.white)
    bold = ParagraphStyle("bold", parent=small, fontName="Helvetica-Bold")
    bold_r = ParagraphStyle("bold_r", parent=bold, alignment=2)
    styles = getSampleStyleSheet()

    spec = PDF_SPEC

    def fmt(v, idx):
        if v is None or v == "":
            return ""
        if isinstance(v, date):
            return f"{v:%d-%b-%Y}"
        if idx in (12, 13, 14):
            return _inr_plain(v)
        if idx == 15:
            return f"{v:.0f}" if v == v.to_integral() else f"{v:.2f}"
        if idx == 7:
            return PDF_TYPE_LABELS.get(v, v)
        return str(v)

    data = [[Paragraph(h, head) for h, *_ in spec]]
    count = 0
    for p in qs:
        vals = row_values(p)
        data.append([Paragraph(_esc(fmt(vals[i], i)), small_r if r else small) for _, i, _, r, _w in spec])
        count += 1
    t = totals(qs)
    tot = [Paragraph("", bold) for _ in spec]
    tot[0] = Paragraph("Totals", bold)
    tot[8] = Paragraph(f"{count}", bold_r)
    for key, idx in (("net", 12), ("gst", 13), ("total", 14)):
        pos = [c[1] for c in spec].index(idx)
        tot[pos] = Paragraph(_inr_plain(t[key]), bold_r)
    data.append(tot)

    table = Table(data, colWidths=[c[2] for c in spec], repeatRows=1)
    ts = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#BBBBBB")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#DDE6F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), PDF_PAD), ("RIGHTPADDING", (0, 0), (-1, -1), PDF_PAD),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#F2F6FA")]),
    ]
    table.setStyle(TableStyle(ts))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24,
                            bottomMargin=30, title="InsureRemind - Policy report")
    story = [
        Paragraph("InsureRemind - Policy report", styles["Title"]),
        Paragraph(_esc(f"Filters: {describe_filters(form)}"), styles["Normal"]),
        Paragraph(f"Generated {_stamp():%d-%b-%Y %H:%M} - {count} policies", styles["Normal"]),
        Spacer(1, 8), table,
    ]
    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()


def _inr_plain(v):
    from .templatetags.policy_tags import inr_format
    return inr_format(v).replace("₹", "")


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ views
class ReportPermission(PermissionRequiredMixin):
    permission_required = "policies.view_policy"
    raise_exception = True


class ReportView(ReportPermission, View):
    def get(self, request):
        qs, form = filtered_policies(request.GET)
        page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
        qd = request.GET.copy()
        qd.pop("page", None)
        qd["f"] = "1"
        if "latest" not in request.GET and "f" not in request.GET:
            qd["latest"] = "on"
        return render(request, "policies/report.html", {
            "form": form, "page": page, "count": page.paginator.count,
            "totals": totals(qs) if form.is_valid() else None,
            "querystring": qd.urlencode(),
        })


class ReportXlsxView(ReportPermission, View):
    def get(self, request):
        qs, form = filtered_policies(request.GET)
        resp = HttpResponse(build_xlsx(qs, form),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = f'attachment; filename="{_filename("xlsx")}"'
        return resp


class ReportPdfView(ReportPermission, View):
    def get(self, request):
        qs, form = filtered_policies(request.GET)
        resp = HttpResponse(build_pdf(qs, form), content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename="{_filename("pdf")}"'
        return resp
