"""
Wire shapes for the careers candidate app.

These match src/services/contract.ts in the client one for one, and the rules
there apply here. One name per field, so no `access` alongside `access_token`
and no six shapes for one payload. Nullable rather than absent, so a field the
backend has not filled arrives as null and the client's adapter can refuse it
loudly instead of it surfacing as undefined three screens later.
"""
from rest_framework import serializers

from .models import Corridor, CorridorRung, Person, VerificationClaim


class CorridorRungSerializer(serializers.ModelSerializer):
    verifier = serializers.CharField(allow_null=True)
    # Null on the wire rather than absent, and the client reads a null as
    # "none". A rung whose cost nobody has established must not look free.
    route = serializers.CharField(allow_null=True)

    class Meta:
        model = CorridorRung
        fields = [
            "key", "display_name", "requirement", "input", "verifier", "route",
            # Always present, empty list rather than absent. The client decides
            # picker-or-text-box from whether it has entries, and an absent key
            # would make that check depend on the server's mood.
            "jurisdictions",
            "order",
        ]


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
    source_as_of = serializers.DateField(allow_null=True)

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
            # A client must be able to tell a fact from a list search before it
            # renders either. `method` cannot do it — a screen is legitimately
            # primary_source — so a client without these three fields can only
            # show an exclusion check as though it were a confirmed credential.
            # They travel together for that reason; none is optional detail.
            "shape",
            "source_as_of",
            "matched_on",
        ]

    def to_representation(self, instance):
        """
        A screen that found something reaches the person as `under_review`.

        Every endpoint using this serializer answers the person the claim is
        about. A `possible_match` is usually somebody else with the same name,
        and telling a nurse "possible match on a federal exclusion list" before
        a human has looked would be alarming and, most of the time, wrong. The
        real outcome stays in the claims table for whoever reviews it.
        """
        data = super().to_representation(instance)
        if data.get("shape") == VerificationClaim.Shape.SCREENS and data.get("fact_value") in (
            "possible_match", "match",
        ):
            data["fact_value"] = "under_review"
        return data


class PersonSerializer(serializers.ModelSerializer):
    """
    The person as the app reads them back. `agent_name` is nullable rather than
    absent, so a person who has not named their Navigator is a null and not a
    missing key.
    """

    agent_name = serializers.CharField(allow_null=True)

    class Meta:
        model = Person
        fields = [
            "person_id",
            "full_name",
            "email",
            "phone",
            "city",
            "region",
            "country",
            "agent_name",
            # Collected only to tell this person apart from somebody with
            # the same name on an exclusion list. Returned so the app can
            # show what it holds and let them correct it.
            "date_of_birth",
            "previous_names",
            # Null until they agree. The app shows it back so a person can see
            # what they agreed to and when.
            "screening_consent_at",
        ]


class PersonUpdateSerializer(serializers.Serializer):
    """
    What a person may change about themselves.

    Email is deliberately not here. It is the identity the code was sent to and
    the thing every session is minted against, so accepting it on an update
    would let a signed-in person rewrite themselves into somebody else's row.
    Changing an address is a re-verification, not a field edit.

    person_id is absent for the same reason, one step more obviously.
    """

    full_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=64, required=False, allow_blank=True)
    city = serializers.CharField(max_length=255, required=False, allow_blank=True)
    region = serializers.CharField(max_length=255, required=False, allow_blank=True)
    country = serializers.CharField(max_length=255, required=False, allow_blank=True)

    # Editable, unlike email. These are identity *resolution* fields, not
    # the identity itself: getting a date of birth wrong makes a screen
    # weaker, never someone else's row reachable. `allow_null` because
    # "I would rather not say" has to be expressible — the product works
    # without them and says so.
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    previous_names = serializers.ListField(
        child=serializers.CharField(max_length=255, allow_blank=False),
        required=False, allow_empty=True, max_length=10,
    )
    # A yes or no, not a timestamp. The server records when, so a client
    # cannot backdate consent or claim it was given at a time it was not.
    screening_consent = serializers.BooleanField(required=False)

    def validate_date_of_birth(self, value):
        """A birth date in the future is a typo, and would screen nobody correctly."""
        from django.utils import timezone  # noqa: PLC0415

        if value is not None and value > timezone.localdate():
            raise serializers.ValidationError("A date of birth cannot be in the future.")
        return value


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


class SignupStartSerializer(serializers.Serializer):
    """Just the address. Careers asks for nothing else to let someone in."""

    email = serializers.EmailField(max_length=255)


class SignupVerifySerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=255)
    code = serializers.CharField(min_length=6, max_length=6)
