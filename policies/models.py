from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .utils import normalize_reg_no

EXPIRING_SOON_DAYS = 30


def today():
    return timezone.localdate()


class Owner(models.Model):
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, help_text="E.164, e.g. +919876543210")
    email = models.EmailField(blank=True)
    whatsapp_opt_in = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.phone})"


class Vehicle(models.Model):
    registration_no = models.CharField(max_length=20, unique=True)
    make = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    variant = models.CharField(max_length=100, blank=True)
    fuel_type = models.CharField(max_length=50, blank=True)
    manufacturing_year = models.PositiveSmallIntegerField(null=True, blank=True)
    chassis_no = models.CharField(max_length=50, blank=True)
    engine_no = models.CharField(max_length=50, blank=True)
    rto_location = models.CharField("RTO location", max_length=100, blank=True)
    registration_date = models.DateField(null=True, blank=True)
    vehicle_usage = models.CharField(max_length=100, blank=True)
    owner = models.ForeignKey(Owner, on_delete=models.PROTECT, related_name="vehicles")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["registration_no"]

    def save(self, *args, **kwargs):
        self.registration_no = normalize_reg_no(self.registration_no)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.registration_no

    def get_absolute_url(self):
        return reverse("vehicle_detail", args=[self.pk])

    @property
    def latest_policy(self):
        return self.policies.order_by("-expiry_date", "-id").first()

    @property
    def description(self):
        return " ".join(x for x in (self.make, self.model, self.variant) if x)


class PolicyQuerySet(models.QuerySet):
    def latest_per_vehicle(self):
        """Only the policy with the max expiry_date per vehicle (ties: highest id)."""
        newer = Policy.objects.filter(vehicle=models.OuterRef("vehicle")).filter(
            Q(expiry_date__gt=models.OuterRef("expiry_date"))
            | Q(expiry_date=models.OuterRef("expiry_date"), id__gt=models.OuterRef("id"))
        )
        return self.filter(~models.Exists(newer))


class Policy(models.Model):
    ACTIVE, EXPIRING, EXPIRED = "active", "expiring", "expired"
    TYPE_CHOICES = [
        ("comprehensive", "Comprehensive"),
        ("third_party", "Third-party"),
        ("own_damage", "Own-damage"),
        ("other", "Other"),
    ]

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="policies")
    insurer = models.CharField(max_length=200)
    policy_number = models.CharField(max_length=100)
    policy_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="comprehensive")
    premium = models.DecimalField("Total premium (incl. GST)", max_digits=12, decimal_places=2)
    net_premium = models.DecimalField("Net premium (before GST)", max_digits=12, decimal_places=2, null=True, blank=True)
    gst_amount = models.DecimalField("GST amount", max_digits=12, decimal_places=2, null=True, blank=True)
    ncb_percent = models.DecimalField("NCB % (current year)", max_digits=5, decimal_places=2, null=True, blank=True)
    previous_policy_number = models.CharField(max_length=100, blank=True)
    previous_insurer = models.CharField(max_length=200, blank=True)
    previous_policy_type = models.CharField(max_length=100, blank=True)
    previous_policy_start = models.DateField(null=True, blank=True)
    previous_policy_end = models.DateField(null=True, blank=True)
    previous_ncb_percent = models.DecimalField("Previous year NCB %", max_digits=5, decimal_places=2, null=True, blank=True)
    previous_claims_count = models.PositiveSmallIntegerField("Claims under previous policy", null=True, blank=True)
    start_date = models.DateField()
    expiry_date = models.DateField()
    document = models.FileField(upload_to="policies/", blank=True)
    extracted_data = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PolicyQuerySet.as_manager()

    class Meta:
        ordering = ["-expiry_date", "-id"]
        verbose_name_plural = "policies"

    def __str__(self):
        return f"{self.policy_number} ({self.vehicle})"

    @property
    def days_left(self):
        return (self.expiry_date - today()).days

    @property
    def status(self):
        d = self.days_left
        if d < 0:
            return self.EXPIRED
        if d <= EXPIRING_SOON_DAYS:
            return self.EXPIRING
        return self.ACTIVE

    @property
    def status_label(self):
        return {self.ACTIVE: "Active", self.EXPIRING: "Expiring soon", self.EXPIRED: "Expired"}[self.status]

    @property
    def is_latest(self):
        latest = self.vehicle.latest_policy
        return latest is not None and latest.pk == self.pk

    @staticmethod
    def status_range(status):
        """Return a Q filter on expiry_date for a status keyword."""
        t = today()
        limit = t + timedelta(days=EXPIRING_SOON_DAYS)
        return {
            Policy.ACTIVE: Q(expiry_date__gt=limit),
            Policy.EXPIRING: Q(expiry_date__gte=t, expiry_date__lte=limit),
            Policy.EXPIRED: Q(expiry_date__lt=t),
        }.get(status)


class PolicyUpload(models.Model):
    PENDING, CONFIRMED, DISCARDED = "pending", "confirmed", "discarded"
    STATUS_CHOICES = [(PENDING, "Pending"), (CONFIRMED, "Confirmed"), (DISCARDED, "Discarded")]

    file = models.FileField(upload_to="uploads/")
    vehicle_no = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20)
    whatsapp_consent = models.BooleanField(default=False)
    extracted_data = models.JSONField(null=True, blank=True)
    extraction_error = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Upload {self.pk} ({self.vehicle_no})"

    @property
    def is_pdf(self):
        return self.file.name.lower().endswith(".pdf")
