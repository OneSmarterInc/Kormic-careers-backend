from django.urls import path

from . import views

urlpatterns = [
    path("corridors/<str:corridor_key>/", views.corridor_detail, name="careers-corridor"),
    path("claims/submit/", views.submit_rung, name="careers-claim-submit"),
    path("claims/<str:rung_key>/", views.claim_status, name="careers-claim-status"),
    path("agent/escalations/", views.escalation_statuses, name="careers-escalations"),
]
