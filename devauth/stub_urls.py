"""
DEV STUB — delete on integration.

Routes the client calls that careers does not serve. Included after careers in
config/urls.py, so careers always wins where the two could overlap.

In the real project these paths are already served by institutes_list and
django_api, and this file goes away.
"""
from django.urls import path

from . import views

urlpatterns = [
    # The claim front door — really institutes_list.
    path("claim/start/", views.claim_start, name="stub-claim-start"),
    path("claim/verify/", views.claim_verify, name="stub-claim-verify"),
    path("claim/confirm/", views.claim_confirm, name="stub-claim-confirm"),
    # Chat — really django_api. The escalation *status* route is not here:
    # that one is careers' own, and it reads the rows chat_send writes.
    path("agent/history/", views.chat_history, name="stub-chat-history"),
    path("agent/message/", views.chat_send, name="stub-chat-send"),
    path("agent/name/", views.chat_rename, name="stub-chat-rename"),
    # Not yet served anywhere. Shapes are the client's.
    path("claims/<str:rung_key>/document/", views.rung_document, name="stub-rung-document"),
    path("notifications/register/", views.push_register, name="stub-push-register"),
]
