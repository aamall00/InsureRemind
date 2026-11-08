from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from notifications.scheduling import on_policy_created
from policies.models import Owner, Policy, Vehicle

DEMO = [
    # name, phone, email, reg, make, model, fuel, insurer, days_to_expiry
    ("Asha Verma", "+919820011111", "asha@example.com", "MH12AB1234", "Maruti", "Swift", "Petrol", "ICICI Lombard", 7),
    ("Rahul Mehta", "+919820022222", "rahul@example.com", "MH14CD5678", "Hyundai", "Creta", "Diesel", "HDFC ERGO", 20),
    ("Sunita Rao", "+919820033333", "sunita@example.com", "KA01EF9012", "Honda", "City", "Petrol", "Bajaj Allianz", 60),
    ("Vikram Singh", "+919820044444", "vikram@example.com", "DL8CAF3456", "Tata", "Nexon", "EV", "New India Assurance", 200),
    ("Meera Iyer", "+919820055555", "meera@example.com", "TN09GH7890", "Toyota", "Innova", "Diesel", "Tata AIG", -12),
]


class Command(BaseCommand):
    help = "Create demo owners, vehicles and policies with varied expiry dates."

    @transaction.atomic
    def handle(self, *args, **opts):
        today = timezone.localdate()
        created = 0
        for name, phone, email, reg, make, model, fuel, insurer, days in DEMO:
            if Vehicle.objects.filter(registration_no=reg).exists():
                continue
            owner = Owner.objects.create(name=name, phone=phone, email=email, whatsapp_opt_in=True)
            vehicle = Vehicle.objects.create(registration_no=reg, owner=owner, make=make, model=model,
                                             fuel_type=fuel, manufacturing_year=2021)
            expiry = today + timedelta(days=days)
            policy = Policy.objects.create(
                vehicle=vehicle, insurer=insurer, policy_number=f"DEMO/{reg}/001",
                policy_type="comprehensive", premium=Decimal("14250.00"),
                start_date=expiry - timedelta(days=365), expiry_date=expiry,
            )
            on_policy_created(policy)
            created += 1
        self.stdout.write(self.style.SUCCESS(f"Seeded {created} demo vehicle(s)."))
