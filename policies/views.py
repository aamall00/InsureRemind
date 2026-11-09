import io
import mimetypes
import os
import zipfile
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from notifications.models import Reminder
from notifications.scheduling import reschedule

from . import extraction
from .audit import record_access
from .documents import document_available, document_filename
from .forms import OwnerForm, PolicyForm, ReviewForm, UploadForm, VehicleForm, policy_warnings
from .models import Owner, Policy, PolicyUpload, Vehicle
from .services import confirm_upload
from .utils import normalize_reg_no


class Perm(PermissionRequiredMixin):
    raise_exception = True  # authenticated users lacking the permission get 403


class DashboardView(Perm, View):
    permission_required = "policies.view_policy"

    def get(self, request):
        today = timezone.localdate()
        latest = Policy.objects.latest_per_vehicle()
        live = latest.filter(expiry_date__gte=today)
        month_start = today.replace(day=1)
        ctx = {
            "total_active": live.count(),
            "exp7": live.filter(expiry_date__lte=today + timedelta(days=7)).count(),
            "exp30": live.filter(expiry_date__lte=today + timedelta(days=30)).count(),
            "expired": latest.filter(expiry_date__lt=today).count(),
            "sent_month": Reminder.objects.filter(status=Reminder.SENT, sent_at__date__gte=month_start).count(),
            "upcoming": live.filter(expiry_date__lte=today + timedelta(days=30))
                .select_related("vehicle__owner").order_by("expiry_date"),
        }
        return render(request, "policies/dashboard.html", ctx)


class VehicleListView(Perm, View):
    permission_required = "policies.view_policy"

    def get(self, request):
        q = request.GET.get("q", "").strip()
        status = request.GET.get("status", "")
        qs = Policy.objects.latest_per_vehicle().select_related("vehicle__owner").order_by("expiry_date")
        if q:
            digits = "".join(ch for ch in q if ch.isdigit())
            cond = (Q(vehicle__registration_no__contains=normalize_reg_no(q))
                    | Q(vehicle__owner__name__icontains=q) | Q(policy_number__icontains=q))
            if len(digits) >= 3:
                cond |= Q(vehicle__owner__phone__contains=digits)
            qs = qs.filter(cond)
        rng = Policy.status_range(status)
        if rng is not None:
            qs = qs.filter(rng)
        page = Paginator(qs, 20).get_page(request.GET.get("page"))
        qd = request.GET.copy()
        qd.pop("page", None)
        return render(request, "policies/vehicle_list.html", {
            "page": page, "q": q, "status": status, "querystring": qd.urlencode(),
        })


class VehicleDetailView(Perm, View):
    permission_required = "policies.view_vehicle"

    def get(self, request, pk):
        vehicle = get_object_or_404(Vehicle.objects.select_related("owner"), pk=pk)
        policies = list(vehicle.policies.select_related("vehicle").order_by("-expiry_date", "-id"))
        for p in policies:
            p.doc_available = document_available(p)
            p.doc_filename = document_filename(p) if p.doc_available else ""
        latest = policies[0] if policies else None
        reminders = latest.reminders.order_by("scheduled_for", "channel", "recipient_type") if latest else []
        return render(request, "policies/vehicle_detail.html", {
            "vehicle": vehicle, "owner": vehicle.owner, "policies": policies, "latest": latest,
            "reminders": reminders, "docs_count": sum(p.doc_available for p in policies),
        })


class UploadView(Perm, View):
    permission_required = "policies.add_policy"

    def get(self, request):
        initial = {}
        vid = request.GET.get("vehicle")
        if vid and vid.isdigit():
            v = Vehicle.objects.filter(pk=vid).select_related("owner").first()
            if v:
                initial = {"vehicle_no": v.registration_no, "email": v.owner.email, "phone": v.owner.phone,
                           "whatsapp_consent": v.owner.whatsapp_opt_in}
        return render(request, "policies/upload.html", {"form": UploadForm(initial=initial)})

    def post(self, request):
        form = UploadForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, "policies/upload.html", {"form": form})
        f = form.cleaned_data["file"]
        upload = PolicyUpload.objects.create(
            file=f, vehicle_no=form.cleaned_data["vehicle_no"], email=form.cleaned_data["email"],
            phone=form.cleaned_data["phone"], whatsapp_consent=form.cleaned_data["whatsapp_consent"],
            created_by=request.user,
        )
        try:
            f.seek(0)
            upload.extracted_data = extraction.extract_policy_data(f.name, f.read())
        except extraction.ExtractionError as e:  # includes ExtractionUnavailable
            upload.extraction_error = str(e)
        except Exception:  # never let extraction crash the upload
            upload.extraction_error = "Unexpected error while reading the document. Please enter details manually."
        upload.save()
        return redirect("upload_review", pk=upload.pk)


class UploadReviewView(Perm, View):
    permission_required = "policies.add_policy"

    def _get(self, request, pk):
        return get_object_or_404(PolicyUpload, pk=pk)

    def _context(self, upload, form):
        cleaned = extraction.clean_extracted(upload.extracted_data) if upload.extracted_data else {}
        extracted_reg = cleaned.get("registration_no", "")
        vehicle = Vehicle.objects.select_related("owner").filter(registration_no=upload.vehicle_no).first()
        warnings = list(cleaned.get("warnings", []))
        data = form.cleaned_data if form.is_bound and form.is_valid() else (cleaned if not form.is_bound else {})
        for w in policy_warnings(data):
            if w not in warnings:
                warnings.append(w)
        return {
            "upload": upload, "form": form, "vehicle": vehicle, "warnings": warnings,
            "latest": vehicle.latest_policy if vehicle else None,
            "extracted_reg": extracted_reg,
            "reg_mismatch": bool(extracted_reg) and extracted_reg != upload.vehicle_no,
        }

    def get(self, request, pk):
        upload = self._get(request, pk)
        if upload.status != PolicyUpload.PENDING:
            messages.info(request, "This upload has already been processed.")
            return redirect("dashboard")
        initial = {}
        if upload.extracted_data:
            initial = {k: v for k, v in extraction.clean_extracted(upload.extracted_data).items()
                       if k not in ("registration_no", "warnings") and v not in (None, "")}
        vehicle = Vehicle.objects.select_related("owner").filter(registration_no=upload.vehicle_no).first()
        if vehicle:  # prefill known details for renewals when extraction gave nothing
            for k in ("make", "model", "variant", "fuel_type", "manufacturing_year", "chassis_no", "engine_no",
                      "rto_location", "registration_date", "vehicle_usage"):
                initial.setdefault(k, getattr(vehicle, k))
            initial.setdefault("owner_name", vehicle.owner.name)
        initial.setdefault("policy_type", "comprehensive")
        return render(request, "policies/review.html", self._context(upload, ReviewForm(initial=initial)))

    def post(self, request, pk):
        upload = self._get(request, pk)
        if upload.status != PolicyUpload.PENDING:
            messages.info(request, "This upload has already been processed.")
            return redirect("dashboard")
        form = ReviewForm(request.POST)
        if not form.is_valid():
            return render(request, "policies/review.html", self._context(upload, form))
        try:
            policy = confirm_upload(upload, form.cleaned_data, request.user)
        except ValueError:
            messages.info(request, "This upload has already been processed.")
            return redirect("dashboard")
        messages.success(request, f"Policy {policy.policy_number} saved for {policy.vehicle.registration_no}.")
        return redirect("vehicle_detail", pk=policy.vehicle_id)


class UploadDiscardView(Perm, View):
    permission_required = "policies.add_policy"

    def post(self, request, pk):
        upload = get_object_or_404(PolicyUpload, pk=pk, status=PolicyUpload.PENDING)
        upload.status = PolicyUpload.DISCARDED
        upload.save(update_fields=["status"])
        upload.file.storage.delete(upload.file.name)
        messages.info(request, "Upload discarded.")
        return redirect("dashboard")


def _file_response(fieldfile, as_attachment=False, filename=None):
    if not fieldfile or not fieldfile.name:
        raise Http404("No document")
    try:
        fh = fieldfile.open("rb")
    except (FileNotFoundError, ValueError):
        raise Http404("Document file is missing")
    ctype = mimetypes.guess_type(fieldfile.name)[0] or "application/octet-stream"
    name = filename or fieldfile.name.rsplit("/", 1)[-1]
    resp = FileResponse(fh, content_type=ctype, as_attachment=as_attachment, filename=name)
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


class UploadFileView(Perm, View):
    permission_required = "policies.add_policy"

    def get(self, request, pk):
        upload = get_object_or_404(PolicyUpload, pk=pk)
        resp = _file_response(upload.file, filename=f"{upload.vehicle_no}{os.path.splitext(upload.file.name)[1]}")
        record_access(request, "upload_view", upload=upload)
        return resp


class PolicyDocumentView(Perm, View):
    permission_required = "policies.view_policy"

    def get(self, request, pk):
        policy = get_object_or_404(Policy.objects.select_related("vehicle"), pk=pk)
        download = bool(request.GET.get("download"))
        if download:
            resp = _file_response(policy.document, as_attachment=True, filename=document_filename(policy))
        else:
            resp = _file_response(policy.document)
        record_access(request, "download" if download else "view", policy=policy)
        return resp


class VehicleDocumentsZipView(Perm, View):
    permission_required = "policies.view_policy"

    def get(self, request, pk):
        vehicle = get_object_or_404(Vehicle, pk=pk)
        buf = io.BytesIO()
        used = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in vehicle.policies.select_related("vehicle").order_by("-expiry_date", "-id"):
                if not document_available(p):
                    continue
                name = document_filename(p)
                if name in used:  # same number+start twice: keep both
                    root, ext = name.rsplit(".", 1)
                    name = f"{root}_{p.pk}.{ext}"
                used.add(name)
                with p.document.open("rb") as fh:
                    zf.writestr(name, fh.read())
        resp = HttpResponse(buf.getvalue(), content_type="application/zip")
        resp["Content-Disposition"] = f'attachment; filename="{vehicle.registration_no}_policies.zip"'
        record_access(request, "zip", vehicle=vehicle)
        return resp


class _EditView(Perm, View):
    template = "policies/edit.html"
    form_class = None
    model = None
    title = ""

    def get_object(self, pk):
        return get_object_or_404(self.model, pk=pk)

    def redirect_target(self, obj):
        raise NotImplementedError

    def after_save(self, obj, form):
        pass

    def get(self, request, pk):
        obj = self.get_object(pk)
        return render(request, self.template, {"form": self.form_class(instance=obj), "title": self.title, "obj": obj,
                                               "cancel": self.redirect_target(obj)})

    def post(self, request, pk):
        obj = self.get_object(pk)
        form = self.form_class(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save()
            self.after_save(obj, form)
            messages.success(request, "Changes saved.")
            return redirect(self.redirect_target(obj))
        return render(request, self.template, {"form": form, "title": self.title, "obj": obj,
                                               "cancel": self.redirect_target(obj)})


class OwnerEditView(_EditView):
    permission_required = "policies.change_owner"
    form_class, model, title = OwnerForm, Owner, "Edit owner"

    def redirect_target(self, obj):
        v = obj.vehicles.first()
        return v.get_absolute_url() if v else "/"

    def after_save(self, owner, form):
        # contact details may have changed: refresh pending reminder recipients
        for v in owner.vehicles.all():
            p = v.latest_policy
            if p:
                reschedule(p)


class VehicleEditView(_EditView):
    permission_required = "policies.change_vehicle"
    form_class, model, title = VehicleForm, Vehicle, "Edit vehicle"

    def redirect_target(self, obj):
        return obj.get_absolute_url()


class PolicyEditView(_EditView):
    permission_required = "policies.change_policy"
    form_class, model, title = PolicyForm, Policy, "Edit policy"

    def redirect_target(self, obj):
        return obj.vehicle.get_absolute_url()

    def after_save(self, policy, form):
        if form.has_changed():
            # editing dates can change which policy is the latest for the vehicle
            reschedule(policy.vehicle.latest_policy)
