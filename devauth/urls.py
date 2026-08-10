"""DEV STUB — delete on integration."""
from django.urls import path

from . import views

urlpatterns = [
    path("login/", views.dev_login, name="dev-login"),
    # Marks the caller's open questions answered, so a pending bubble can be
    # seen to flip without waiting on a real practice.
    path("answer/", views.dev_answer, name="dev-answer"),
]
