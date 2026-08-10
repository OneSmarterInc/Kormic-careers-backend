from django.contrib import admin

from .models import Corridor, CorridorRung, Person, VerificationClaim


class CorridorRungInline(admin.TabularInline):
    model = CorridorRung
    extra = 0


@admin.register(Corridor)
class CorridorAdmin(admin.ModelAdmin):
    list_display = ("key", "display_name", "is_active")
    inlines = [CorridorRungInline]


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("person_id", "full_name", "email", "agent_name")
    search_fields = ("person_id", "full_name", "email")


@admin.register(VerificationClaim)
class VerificationClaimAdmin(admin.ModelAdmin):
    """Read-only on purpose: a claim is corrected by superseding it, never edited."""

    list_display = ("person", "rung_key", "method", "status", "checked_at", "expires_at")
    list_filter = ("method", "status", "corridor")
    readonly_fields = [f.name for f in VerificationClaim._meta.fields]
