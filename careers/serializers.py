"""
Wire shapes for the careers candidate app.

These match src/services/contract.ts in the client one for one, and the rules
there apply here. One name per field, so no `access` alongside `access_token`
and no six shapes for one payload. Nullable rather than absent, so a field the
backend has not filled arrives as null and the client's adapter can refuse it
loudly instead of it surfacing as undefined three screens later.
"""
from rest_framework import serializers

from .models import Corridor, CorridorRung, VerificationClaim


class CorridorRungSerializer(serializers.ModelSerializer):
    verifier = serializers.CharField(allow_null=True)

    class Meta:
        model = CorridorRung
        fields = ["key", "display_name", "requirement", "input", "verifier", "order"]


class CorridorSerializer(serializers.ModelSerializer):
    rungs = CorridorRungSerializer(many=True, read_only=True)

    class Meta:
        model = Corridor
        fields = ["key", "display_name", "rungs"]


class VerificationClaimSerializer(serializers.ModelSerializer):
    """
    checked_at is required on the wire because a method without a date is not a
    claim. The client's adapter throws on a null, so sending one is a contract
    violation rather than a soft failure.
    """

    source_ref = serializers.CharField(allow_null=True)
    verifier = serializers.CharField(allow_null=True)
    verifier_version = serializers.CharField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)

    class Meta:
        model = VerificationClaim
        fields = [
            "rung_key",
            "fact_type",
            "fact_value",
            "method",
            "source_ref",
            "verifier",
            "verifier_version",
            "checked_at",
            "expires_at",
            "status",
        ]


class RungSubmissionSerializer(serializers.Serializer):
    """
    What the person hands in at a rung. Not a claim yet.

    `corridor_key` is declared here rather than read off request.data, so it is
    validated like every other field and a caller that omits it gets a 400
    naming the field instead of a 404 that reads as "no such corridor".

    `value` and `jurisdiction` accept null as well as blank. A rung that asks
    for one field still sends the other, and a client that fills an absent
    optional with null is not making a malformed request — it is saying the
    field has no value, which is the same thing blank says.
    """

    corridor_key = serializers.CharField(max_length=64)
    rung_key = serializers.CharField(max_length=64)
    value = serializers.CharField(max_length=500, required=False, allow_blank=True, allow_null=True)
    jurisdiction = serializers.CharField(
        max_length=255, required=False, allow_blank=True, allow_null=True
    )


class EscalationStatusSerializer(serializers.Serializer):
    query_id = serializers.CharField()
    status = serializers.ChoiceField(choices=["pending", "answered", "closed"])


class ErrorSerializer(serializers.Serializer):
    """
    One error shape everywhere. `code` is what the client switches on; `detail`
    is for humans reading logs, never for the person on the screen, because the
    claim flow deliberately gives the same message for a wrong code and an
    unknown invitation.
    """

    code = serializers.ChoiceField(
        choices=["not_found", "bad_code", "expired", "locked", "unauthorised", "server"]
    )
    detail = serializers.CharField()
