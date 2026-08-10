from django.apps import AppConfig


class DjangoApiStubConfig(AppConfig):
    """DEV STUB — delete on integration. Shadows the real django_api."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "django_api"
