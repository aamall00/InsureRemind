from django.urls import path

from . import reports, views

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("vehicles/", views.VehicleListView.as_view(), name="vehicle_list"),
    path("vehicles/<int:pk>/", views.VehicleDetailView.as_view(), name="vehicle_detail"),
    path("vehicles/<int:pk>/documents.zip", views.VehicleDocumentsZipView.as_view(), name="vehicle_documents_zip"),
    path("reports/", reports.ReportView.as_view(), name="report"),
    path("reports/export.xlsx", reports.ReportXlsxView.as_view(), name="report_xlsx"),
    path("reports/export.pdf", reports.ReportPdfView.as_view(), name="report_pdf"),
    path("vehicles/<int:pk>/edit/", views.VehicleEditView.as_view(), name="vehicle_edit"),
    path("owners/<int:pk>/edit/", views.OwnerEditView.as_view(), name="owner_edit"),
    path("policies/<int:pk>/edit/", views.PolicyEditView.as_view(), name="policy_edit"),
    path("policies/<int:pk>/document/", views.PolicyDocumentView.as_view(), name="policy_document"),
    path("upload/", views.UploadView.as_view(), name="upload"),
    path("upload/<int:pk>/review/", views.UploadReviewView.as_view(), name="upload_review"),
    path("upload/<int:pk>/discard/", views.UploadDiscardView.as_view(), name="upload_discard"),
    path("upload/<int:pk>/file/", views.UploadFileView.as_view(), name="upload_file"),
]
