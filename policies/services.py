import os

from django.core.files.base import ContentFile
from django.db import transaction

from notifications.scheduling import on_policy_created

from .models import Owner, Policy, PolicyUpload, Vehicle

VEHICLE_FIELDS = ["make", "model", "variant", "fuel_type", "manufacturing_year", "chassis_no", "engine_no",
                  "rto_location", "registration_date", "vehicle_usage"]
POLICY_EXTRA_FIELDS = ["net_premium", "gst_amount", "ncb_percent", "previous_policy_number", "previous_insurer",
                       "previous_policy_type", "previous_policy_start", "previous_policy_end",
                       "previous_ncb_percent", "previous_claims_count"]

CHAR_EXTRA = ["previous_policy_number", "previous_insurer", "previous_policy_type"]


@transaction.atomic
def confirm_upload(upload, cleaned, user):
    """Create/update Owner, Vehicle, Policy from a reviewed upload. Returns the Policy."""
    upload = PolicyUpload.objects.select_for_update().get(pk=upload.pk)
    if upload.status != PolicyUpload.PENDING:
        raise ValueError("Upload already processed")

    vehicle = Vehicle.objects.select_related("owner").filter(registration_no=upload.vehicle_no).first()
    if vehicle:
        owner = vehicle.owner
        owner.name, owner.phone, owner.email = cleaned["owner_name"], upload.phone, upload.email
        owner.whatsapp_opt_in = upload.whatsapp_consent
        owner.save()
    else:
        owner = Owner.objects.filter(phone=upload.phone, name__iexact=cleaned["owner_name"]).first()
        if owner:
            owner.email = upload.email or owner.email
            owner.whatsapp_opt_in = upload.whatsapp_consent
            owner.save()
        else:
            owner = Owner.objects.create(
                name=cleaned["owner_name"], phone=upload.phone, email=upload.email,
                whatsapp_opt_in=upload.whatsapp_consent)
        vehicle = Vehicle(registration_no=upload.vehicle_no, owner=owner)

    for f in VEHICLE_FIELDS:
        value = cleaned.get(f)
        if value not in (None, ""):
            setattr(vehicle, f, value)
    vehicle.owner = owner
    vehicle.save()

    policy = Policy(
        vehicle=vehicle, insurer=cleaned["insurer"], policy_number=cleaned["policy_number"],
        policy_type=cleaned["policy_type"], premium=cleaned["premium"],
        start_date=cleaned["start_date"], expiry_date=cleaned["expiry_date"],
        extracted_data=upload.extracted_data, created_by=user,
        **{f: cleaned.get(f) for f in POLICY_EXTRA_FIELDS if f not in CHAR_EXTRA},
        **{f: cleaned.get(f) or "" for f in CHAR_EXTRA},
    )
    # "Move" the staged file into policies/
    with upload.file.open("rb") as fh:
        content = fh.read()
    policy.document.save(os.path.basename(upload.file.name), ContentFile(content), save=False)
    policy.save()

    old_name = upload.file.name
    storage = upload.file.storage
    upload.status = PolicyUpload.CONFIRMED
    upload.save(update_fields=["status"])
    transaction.on_commit(lambda: storage.delete(old_name))

    on_policy_created(policy)
    return policy
