"""
Root urlconf for the dev project.

The one line that matters for integration is the careers include: the client's
endpoint map in src/services/contract.ts is written against `/api/...`, so
careers has to be mounted under that prefix. Its own urls.py declares paths
without it on purpose, so the project decides where the app lives.
"""
from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

urlpatterns = [
    path("admin/", admin.site.urls),
    # The app that ships.
    path("api/", include("careers.urls")),
    # Session renewal. Takes {"refresh": ...} and answers {"access": ...},
    # which is exactly what the client's api.ts sends and reads.
    path("api/auth/refresh/", TokenRefreshView.as_view(), name="auth-refresh"),
    # Dev only. Included after careers so careers wins any overlap, and the
    # whole block disappears on integration.
    path("api/dev/", include("devauth.urls")),
    path("api/", include("devauth.stub_urls")),
]
