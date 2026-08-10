"""
Careers corridor: the person side of hiring.

Three things here are deliberate and are the reason this is a new app rather
than fields bolted onto django_api.

Person is the durable identity and holds only what is true regardless of
corridor. StudentProfile is a persona view onto a Person, not the other way
around, so nothing in careers is named student and no physician assistant
becomes a student row.

VerificationClaim is one row per fact. StudentProfile carries `verified` as a
single boolean across a whole profile, which is serviceable for students and
unusable here, where a licence may be confirmed against its issuing body while
work history is self-attested on the same person on the same day. There is no
profile-level verified column in this app and adding one undoes the model.

Corridor is a first-class object from the first migration. The onboarding ladder
is CorridorRung rows, not a constant in the client, so adding a discipline is an
insert rather than a release.
"""
import secrets

from django.db import models


def _new_person_id() -> str:
    return f"p_{secrets.token_urlsafe(12)}"


class Person(models.Model):
    """
    The durable identity. Corridor-specific data hangs off persona rows and
    claims; nothing corridor-specific belongs on this table, ever.
    """

    person_id = models.CharField(max_length=64, unique=True, db_index=True, default=_new_person_id)

    # The person's own agent. Named on first use and person-editable after,
    # which is the ownership cue. Called a Navigator in public copy.
    agent_name = models.CharField(max_length=100, unique=True, null=True, blank=True, db_index=True)

    full_name = models.CharField(max_length=255, blank=True, default="")
    email = models.CharField(max_length=255, blank=True, default="", db_index=True)
    phone = models.CharField(max_length=64, blank=True, default="")
    city = models.CharField(max_length=255, blank=True, default="")
    region = models.CharField(max_length=255, blank=True, default="")
    country = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Person({self.person_id})"


class Corridor(models.Model):
    """
    The governed layer. Claims vocabulary and the regulatory floor are set here
    by a human and are never inferred from an uploaded document. A new corridor
    is a decision; a new position is an upload.
    """

    key = models.CharField(max_length=64, unique=True, db_index=True)
    display_name = models.CharField(max_length=255)

    # Which disagreements the consistency engine looks for in this corridor.
    consistency_dimensions = models.JSONField(default=list, blank=True)
    # Which org role answers which topic.
    escalation_routing = models.JSONField(default=dict, blank=True)
    # What this corridor may honestly say it verified. Marketing copy inherits
    # this vocabulary rather than inventing its own.
    claims_vocabulary = models.JSONField(default=dict, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return f"Corridor({self.key})"


class CorridorRung(models.Model):
    """
    One step of the ladder. The client fetches these at start and derives its
    routes, gates and progress from them, so requirement changes without a code
    change on either side.
    """

    class Requirement(models.TextChoices):
        REQUIRED = "required", "Required"
        OPTIONAL = "optional", "Optional"
        NOT_APPLICABLE = "not_applicable", "Not applicable"

    class Input(models.TextChoices):
        IDENTIFIER = "identifier", "Identifier"
        IDENTIFIER_WITH_JURISDICTION = "identifier_with_jurisdiction", "Identifier with jurisdiction"
        OAUTH = "oauth", "Third-party login"
        DOCUMENT_UPLOAD = "document_upload", "Document upload"
        SCREENSHOTS = "screenshots", "Screenshots"

    corridor = models.ForeignKey(Corridor, on_delete=models.CASCADE, related_name="rungs")
    key = models.CharField(max_length=64)
    display_name = models.CharField(max_length=255)
    requirement = models.CharField(max_length=20, choices=Requirement.choices)
    input = models.CharField(max_length=40, choices=Input.choices)
    # Which helper bot services this rung. Null means the person tells us and it
    # is shown to practices that way.
    verifier = models.CharField(max_length=64, blank=True, null=True)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ["order"]
        unique_together = [("corridor", "key")]

    def __str__(self):
        return f"Rung({self.corridor_id}:{self.key}, {self.requirement})"


class VerificationClaim(models.Model):
    """
    One row per fact. A person is not verified; specific facts about them are
    verified, by a named method, on a date.

    No credential documents are stored. source_ref plus evidence_hash make a
    check reproducible and challengeable months later without us holding the
    file.
    """

    class Method(models.TextChoices):
        # Only a helper bot confirming directly against the issuing authority
        # may be called primary_source. Until a documented and permitted
        # programmatic route to a given authority exists, everything else is
        # source_checked.
        PRIMARY_SOURCE = "primary_source", "Primary source"
        SOURCE_CHECKED = "source_checked", "Source checked"
        ORG_VOUCHED = "org_vouched", "Vouched by an organisation"
        SELF_ATTESTED = "self_attested", "Self attested"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        SUPERSEDED = "superseded", "Superseded"
        FAILED = "failed", "Failed"
        DISPUTED = "disputed", "Disputed"

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="claims")
    corridor = models.ForeignKey(Corridor, on_delete=models.PROTECT, related_name="claims")

    rung_key = models.CharField(max_length=64, db_index=True)
    fact_type = models.CharField(max_length=64)
    fact_value = models.CharField(max_length=500)
    # Where the fact says it was issued, when the rung asks for that.
    jurisdiction = models.CharField(max_length=255, blank=True, default="")

    method = models.CharField(max_length=32, choices=Method.choices)
    source_ref = models.CharField(max_length=500, blank=True, null=True)
    evidence_hash = models.CharField(max_length=128, blank=True, null=True)
    verifier = models.CharField(max_length=64, blank=True, null=True)
    verifier_version = models.CharField(max_length=64, blank=True, null=True)

    # Never null. A method without a check date is not a claim, and the client
    # refuses to render one.
    checked_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    # The bot's raw finding, kept verbatim for the consistency engine. The
    # derived claim above is computed from it.
    raw_finding = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-checked_at"]
        indexes = [models.Index(fields=["person", "rung_key", "status"])]

    def __str__(self):
        return f"Claim({self.person_id}:{self.rung_key}, {self.method}, {self.status})"
