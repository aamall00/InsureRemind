import os

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError

from .models import Owner, Policy, Vehicle
from .utils import normalize_phone, normalize_reg_no

ALLOWED_TYPES = {
    ".pdf": {"application/pdf"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".png": {"image/png"},
    ".webp": {"image/webp"},
}


def _content_matches(ext, head):
    if ext == ".pdf":
        return head.startswith(b"%PDF-")
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".webp":
        return head[0:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


def validate_policy_file(f):
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in ALLOWED_TYPES:
        raise ValidationError("Upload a PDF, JPG, PNG or WEBP file.")
    ctype = (getattr(f, "content_type", "") or "").lower()
    if ctype and ctype not in ALLOWED_TYPES[ext]:
        raise ValidationError(f"File content type {ctype} does not match its extension.")
    if f.size > settings.MAX_UPLOAD_BYTES:
        raise ValidationError("File is too large (max 10 MB).")
    f.seek(0)
    head = f.read(12)
    f.seek(0)
    if not _content_matches(ext, head):
        raise ValidationError("The file content doesn't match its extension.")


class UploadForm(forms.Form):
    vehicle_no = forms.CharField(max_length=30, label="Vehicle registration no.")
    email = forms.EmailField(label="Client email")
    phone = forms.CharField(max_length=30, label="Client phone")
    whatsapp_consent = forms.BooleanField(
        required=False, initial=True, label="Client has consented to receive WhatsApp reminders")
    file = forms.FileField(label="Policy document (PDF/JPG/PNG/WEBP, max 10 MB)", validators=[validate_policy_file])

    def clean_vehicle_no(self):
        v = normalize_reg_no(self.cleaned_data["vehicle_no"])
        if len(v) < 4:
            raise ValidationError("Enter a valid vehicle registration number.")
        return v

    def clean_phone(self):
        return normalize_phone(self.cleaned_data["phone"])



class ReviewForm(forms.Form):
    # Insured & Vehicle Details
    owner_name = forms.CharField(max_length=200, label="Name of the insured")
    make = forms.CharField(max_length=100, required=False)
    model = forms.CharField(max_length=100, required=False)
    variant = forms.CharField(max_length=100, required=False)
    fuel_type = forms.CharField(max_length=50, required=False)
    manufacturing_year = forms.IntegerField(min_value=1950, max_value=2100, required=False)
    rto_location = forms.CharField(max_length=100, required=False, label="RTO location")
    registration_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    vehicle_usage = forms.CharField(max_length=100, required=False)
    chassis_no = forms.CharField(max_length=50, required=False)
    engine_no = forms.CharField(max_length=50, required=False)
    # Current Policy & Premium
    insurer = forms.CharField(max_length=200)
    policy_number = forms.CharField(max_length=100)
    policy_type = forms.ChoiceField(choices=Policy.TYPE_CHOICES)
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    expiry_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    net_premium = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False, label="Net premium (before GST)")
    gst_amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False, label="GST amount")
    premium = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, label="Total premium (incl. GST)")
    ncb_percent = forms.DecimalField(max_digits=5, decimal_places=2, min_value=0, max_value=100, required=False, label="NCB % (current year)")
    # Previous Policy Details
    previous_policy_number = forms.CharField(max_length=100, required=False)
    previous_insurer = forms.CharField(max_length=200, required=False)
    previous_policy_type = forms.CharField(max_length=100, required=False)
    previous_policy_start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    previous_policy_end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    previous_ncb_percent = forms.DecimalField(max_digits=5, decimal_places=2, min_value=0, max_value=100, required=False, label="Previous year NCB %")
    previous_claims_count = forms.IntegerField(min_value=0, max_value=1000, required=False, label="Claims under previous policy")

    SECTIONS = [
        ("Insured & Vehicle Details", ["owner_name", "make", "model", "variant", "fuel_type", "manufacturing_year",
                                       "rto_location", "registration_date", "vehicle_usage", "chassis_no", "engine_no"]),
        ("Current Policy & Premium", ["insurer", "policy_number", "policy_type", "start_date", "expiry_date",
                                      "net_premium", "gst_amount", "premium", "ncb_percent"]),
        ("Previous Policy Details", ["previous_policy_number", "previous_insurer", "previous_policy_type",
                                     "previous_policy_start", "previous_policy_end", "previous_ncb_percent",
                                     "previous_claims_count"]),
    ]

    @property
    def sections(self):
        return [(title, [self[n] for n in names]) for title, names in self.SECTIONS]

    def clean_chassis_no(self):
        return "".join(self.cleaned_data.get("chassis_no", "").split()).upper()

    def clean_engine_no(self):
        return "".join(self.cleaned_data.get("engine_no", "").split()).upper()

    def clean(self):
        data = super().clean()
        s, e = data.get("start_date"), data.get("expiry_date")
        if s and e and e <= s:
            self.add_error("expiry_date", "Expiry date must be after the start date.")
        return data

    def warnings(self):
        """Non-blocking sanity warnings, computed from submitted/initial values."""
        d = self.cleaned_data if self.is_bound and hasattr(self, "cleaned_data") else self.initial
        return policy_warnings(d)


def policy_warnings(d):
    out = []
    s, e, pe = d.get("start_date"), d.get("expiry_date"), d.get("previous_policy_end")
    if s and e and e <= s:
        out.append("Expiry date is not after the start date.")
    if s and pe and pe >= s:
        out.append("The previous policy ends on or after the current policy start date. "
                   "Previous-policy dates may have been mixed up with the current period.")
    n, t = d.get("net_premium"), d.get("premium")
    if n is not None and t is not None and n > t:
        out.append("Net premium is greater than the total premium payable; the values may be swapped.")
    return out


class OwnerForm(forms.ModelForm):
    class Meta:
        model = Owner
        fields = ["name", "phone", "email", "whatsapp_opt_in"]

    def clean_phone(self):
        return normalize_phone(self.cleaned_data["phone"])


class VehicleForm(forms.ModelForm):
    class Meta:
        model = Vehicle
        fields = ["registration_no", "make", "model", "variant", "fuel_type",
                  "manufacturing_year", "rto_location", "registration_date", "vehicle_usage",
                  "chassis_no", "engine_no"]
        widgets = {"registration_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def clean_registration_no(self):
        v = normalize_reg_no(self.cleaned_data["registration_no"])
        qs = Vehicle.objects.filter(registration_no=v).exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("A vehicle with this registration number already exists.")
        return v


class PolicyForm(forms.ModelForm):
    class Meta:
        model = Policy
        fields = ["insurer", "policy_number", "policy_type", "start_date", "expiry_date",
                  "net_premium", "gst_amount", "premium", "ncb_percent",
                  "previous_policy_number", "previous_insurer", "previous_policy_type",
                  "previous_policy_start", "previous_policy_end", "previous_ncb_percent", "previous_claims_count"]
        widgets = {f: forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
                   for f in ("start_date", "expiry_date", "previous_policy_start", "previous_policy_end")}

    def clean(self):
        data = super().clean()
        s, e = data.get("start_date"), data.get("expiry_date")
        if s and e and e <= s:
            self.add_error("expiry_date", "Expiry date must be after the start date.")
        return data
