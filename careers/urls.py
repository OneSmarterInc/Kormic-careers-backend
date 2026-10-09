from django.urls import path

from . import views

urlpatterns = [
    # The open front door. Careers is not invitation-only; the claim flow below
    # is the secondary path for when a practice does bring a roster.
    path("signup/start/", views.signup_start, name="careers-signup-start"),
    path("signup/verify/", views.signup_verify, name="careers-signup-verify"),
    path("me/", views.me, name="careers-me"),
    path("corridors/<str:corridor_key>/", views.corridor_detail, name="careers-corridor"),
    path("claims/submit/", views.submit_rung, name="careers-claim-submit"),
    path("claims/<str:rung_key>/document/", views.rung_document, name="careers-rung-document"),
    path("claims/<str:rung_key>/", views.claim_status, name="careers-claim-status"),
    path("agent/escalations/", views.escalation_statuses, name="careers-escalations"),
]
