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
    #
    # Not unique. It is what this person calls their own agent, not a handle
    # anyone else addresses them by, so two people both naming theirs Ada is
    # ordinary rather than a collision. It was unique, and the second person to
    # pick a taken word got an IntegrityError on a rename that should never
    # have been able to fail.
    agent_name = models.CharField(max_length=100, null=True, blank=True, db_index=True)

    full_name = models.CharField(max_length=255, blank=True, default="")
    email = models.CharField(max_length=255, blank=True, default="", db_index=True)
    phone = models.CharField(max_length=64, blank=True, default="")
    city = models.CharField(max_length=255, blank=True, default="")
    region = models.CharField(max_length=255, blank=True, default="")
    country = models.CharField(max_length=255, blank=True, default="")

    # --- identity resolution, for screening only --------------------------
    # Both exist for one reason: telling this person apart from somebody with
    # the same name on a federal exclusion list. Neither is used to identify
    # them anywhere else, and neither is required to use the product.
    #
    # A date of birth is what turns "somebody called Amara Okafor is excluded"
    # into an answer. 99% of rows on the OIG list carry one, so without it
    # almost every name collision becomes manual review; with it, most resolve
    # on their own. It is also the field that *clears* people — a shared name
    # with a different date of birth is a different person, and saying so is
    # the single most valuable thing this column does.
    date_of_birth = models.DateField(null=True, blank=True)

    # Exclusions are recorded under the name held at the time. Screening only
    # the current name misses exactly the people worth finding, and in a
    # profession that is overwhelmingly women a marriage change is ordinary.
    previous_names = models.JSONField(default=list, blank=True)

    # When the person agreed to be screened against the federal exclusion
    # lists. Null means they have not, and no background check runs without
    # it. Set by the server at the moment they agree — never taken from the
    # client — so the timestamp is evidence of when consent was given.
    screening_consent_at = models.DateTimeField(null=True, blank=True)

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

    class Route(models.TextChoices):
        """
        Whether reaching this rung's authority costs money.

        FREE routes run on their own when a person joins, because there is no
        decision to make about spending nothing. PAID routes wait until a
        hiring human ticks that person, since anything that costs money is the
        client's decision. NONE means no programmatic route to the authority
        exists at all, so the claim stays where the person left it however much
        anyone would like to spend.

        This is set by a human per corridor and never inferred. Which
        authorities have a free route, a paid one, or none is research with a
        defined end, and until it is done a rung's route is unset rather than
        guessed at.
        """

        FREE = "free", "Free"
        PAID = "paid", "Paid"
        NONE = "none", "No route"

    class Input(models.TextChoices):
        IDENTIFIER = "identifier", "Identifier"
        IDENTIFIER_WITH_JURISDICTION = "identifier_with_jurisdiction", "Identifier with jurisdiction"
        OAUTH = "oauth", "Third-party login"
        DOCUMENT_UPLOAD = "document_upload", "Document upload"
        SCREENSHOTS = "screenshots", "Screenshots"
        # Nothing for the person to hand in. The check runs from what we
        # already hold about them — an exclusion screen on their name and date
        # of birth — so it is not a step on their ladder and the app shows it
        # under background checks instead.
        AUTOMATIC = "automatic", "Automatic"

    corridor = models.ForeignKey(Corridor, on_delete=models.CASCADE, related_name="rungs")
    key = models.CharField(max_length=64)
    display_name = models.CharField(max_length=255)
    requirement = models.CharField(max_length=20, choices=Requirement.choices)
    input = models.CharField(max_length=40, choices=Input.choices)
    # Which helper bot services this rung. Null means the person tells us and it
    # is shown to practices that way.
    verifier = models.CharField(max_length=64, blank=True, null=True)
    # Null until somebody has established what reaching this authority costs.
    # A null route behaves as none: nothing runs by itself and nothing is
    # offered for sale, which is the only honest default.
    route = models.CharField(max_length=10, choices=Route.choices, blank=True, null=True)
    # Where this credential can be issued, as [{"code": ..., "label": ...}].
    #
    # The client renders a picker from this rather than a text box. It has to:
    # the code is matched against the authority directory, so a person typing
    # "California" instead of "US-CA" reaches nothing, and the field previously
    # asked for "the body that issued it" — which no amount of careful typing
    # could turn into a code the lookup would find.
    #
    # Empty means the corridor has not enumerated them, and the client falls
    # back to free text. That keeps a rung usable before anybody has done the
    # research, at the cost of the lookup rarely matching — which is the same
    # honest "not confirmed with anyone" it would reach anyway.
    jurisdictions = models.JSONField(default=list, blank=True)
    order = models.IntegerField(default=0)

    def jurisdiction_codes(self) -> list:
        """The codes a submission may name, for validating one server-side."""
        return [
            str(entry.get("code", "")).strip()
            for entry in (self.jurisdictions or [])
            if isinstance(entry, dict) and str(entry.get("code", "")).strip()
        ]

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

    class Shape(models.TextChoices):
        # What kind of statement this row makes. Orthogonal to `method`, which
        # says how good the source was.
        #
        # A screen is the result of searching a list for somebody — an OIG
        # exclusion check, a SAM.gov debarment check. It is legitimately
        # `primary_source`, because the list's publisher really was asked, but
        # "no matching record found" and "this licence is confirmed" are not
        # the same sentence and must never render as one. Without this column a
        # screen is stored as an ordinary fact and `headline_claim` crowns it.
        ASSERTS = "asserts", "A fact about the person"
        SCREENS = "screens", "The result of searching a list"

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

    shape = models.CharField(max_length=16, choices=Shape.choices, default=Shape.ASSERTS)

    # For a screen: the date of the *data*, which is not the date we looked.
    # The OIG exclusion list is a monthly file, so a check run today answers a
    # question about last month. A screen stored without this cannot be
    # challenged, because nobody can say what it was true of.
    #
    # A DateField rather than a DateTimeField, unlike every other date on this
    # model: a file has a publication date, not a publication instant, and
    # widening it to a datetime would invent a precision the source never had.
    source_as_of = models.DateField(null=True, blank=True)

    # For a screen: which identifiers it searched on. The whole difference
    # between a weak miss and a strong one — "no match on a name" and "no match
    # on name, date of birth and NPI" are not the same claim.
    matched_on = models.JSONField(default=list, blank=True)

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


class SignupCode(models.Model):
    """
    The open front door. Careers is not an invitation corridor: anyone may join,
    and the practice pays to hire rather than to gate who exists.

    The code proves control of the address before an account is minted, which is
    what stops the open door from filling with addresses nobody owns. The
    discipline is the claim flow's, because the reasoning is the same: the code
    is hashed at rest so a database read does not hand over live codes, it has a
    short life, and attempts are capped.

    What it deliberately does not do is tell anyone whether an address is
    already registered. Start answers identically either way.
    """

    TTL_MINUTES = 10
    MAX_ATTEMPTS = 5

    email = models.CharField(max_length=255, db_index=True)
    code_hash = models.CharField(max_length=64)
    attempts = models.IntegerField(default=0)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["email", "consumed_at"])]

    @staticmethod
    def hash_code(code: str) -> str:
        import hashlib

        return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()

    def is_live(self, now) -> bool:
        return (
            self.consumed_at is None
            and self.attempts < self.MAX_ATTEMPTS
            and self.expires_at > now
        )

    def __str__(self):
        return f"SignupCode({self.email}, attempts={self.attempts})"
