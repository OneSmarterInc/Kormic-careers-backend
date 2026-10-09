"""
Careers candidate endpoints.

Scope note. The claim front door (start, verify, confirm) is not reimplemented
here: institutes_list already serves it, with the OTP hashed at rest, a
ten-minute TTL, a five-attempt ceiling, a signed claim session and a generic 404
so lists cannot be enumerated. Careers reuses that app with org-typed lists
rather than forking it. Chat history and send stay in django_api, which already
annotates history with escalation state.

What is new here is the corridor config the client derives its ladder from, the
rung submission that produces a claim, and the escalation status endpoint that
lets a pending bubble flip without a new message arriving.
"""
import hashlib
import logging
import os
import secrets
import tempfile
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldError
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import Corridor, CorridorRung, Person, SignupCode, VerificationClaim
from .services import headline_claim, record_findings, run_screens_quietly, run_verifier_for
from .verifiers import Submission
from .serializers import (
    CorridorSerializer,
    PersonSerializer,
    PersonUpdateSerializer,
    SignupStartSerializer,
    SignupVerifySerializer,
    EscalationStatusSerializer,
    RungSubmissionSerializer,
    VerificationClaimSerializer,
)


logger = logging.getLogger(__name__)


def _error(code: str, detail: str, http_status: int) -> Response:
    return Response({"code": code, "detail": detail}, status=http_status)


@api_view(["GET"])
@permission_classes([AllowAny])
def corridor_detail(request, corridor_key: str):
    """
    The ladder. Fetched before the client renders any step, because a corridor
    that has not answered is not a ladder with zero steps.

    Open to unauthenticated callers on purpose: the tour shows a person what
    they would be asked for before they sign on, and that is the whole point of
    showing it before they sign on.
    """
    corridor = Corridor.objects.filter(key=corridor_key, is_active=True).prefetch_related("rungs").first()
    if corridor is None:
        return _error("not_found", "No such corridor.", status.HTTP_404_NOT_FOUND)
    return Response(CorridorSerializer(corridor).data)


def _person_for(request) -> Person | None:
    """
    Resolve the logged-in user to a person through Account.person_id.

    Not student_id, and not a header. Account already sits between auth.User and
    the domain identifiers, so this is the existing spine doing one more job
    rather than a new indirection layer.
    """
    account = getattr(request.user, "account", None)
    if account is None or not account.person_id:
        return None
    return Person.objects.filter(person_id=account.person_id).first()


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_rung(request):
    """
    The person hands in a fact. This writes a self_attested claim immediately
    and hands the submission to the rung's helper bot if it has one.

    The claim is written at self_attested rather than left absent, because the
    person has genuinely told us something and a practice is entitled to see
    that, marked honestly. Only a bot confirming against the issuing authority
    can raise the method afterwards, and it does so by writing a new claim and
    superseding this one rather than editing it, so the history stays intact for
    the audit trail.
    """
    form = RungSubmissionSerializer(data=request.data)
    form.is_valid(raise_exception=True)
    person = _person_for(request)
    if person is None:
        return _error("unauthorised", "No person on this request.", status.HTTP_401_UNAUTHORIZED)

    corridor_key = form.validated_data["corridor_key"]
    corridor = Corridor.objects.filter(key=corridor_key, is_active=True).first()
    if corridor is None:
        return _error("not_found", "No such corridor.", status.HTTP_404_NOT_FOUND)

    rung_key = form.validated_data["rung_key"]
    rung = CorridorRung.objects.filter(corridor=corridor, key=rung_key).first()
    if rung is None or rung.requirement == CorridorRung.Requirement.NOT_APPLICABLE:
        return _error("not_found", "That step is not part of this corridor.", status.HTTP_404_NOT_FOUND)

    # Per fact type, never rung-wide. The claim written below is about one
    # thing — `fact_type=rung_key`, the value the person typed — so retiring
    # the whole rung would take a bot's findings with it.
    #
    # It did. The client uploads then submits, so every document upload wrote
    # its facts and had them superseded a moment later by the person's own
    # empty submission. The bot's work was never visible, and the rung sat on
    # "still checking" forever because the only surviving claim was
    # self_attested.
    VerificationClaim.objects.filter(
        person=person,
        rung_key=rung_key,
        fact_type=rung_key,
        status=VerificationClaim.Status.ACTIVE,
    ).update(status=VerificationClaim.Status.SUPERSEDED)

    claim = VerificationClaim.objects.create(
        person=person,
        corridor=corridor,
        rung_key=rung_key,
        fact_type=rung_key,
        # `or ""` rather than a default: the field is nullable on the wire but
        # not on the model, so an explicit null has to land as blank.
        fact_value=form.validated_data.get("value") or "",
        jurisdiction=form.validated_data.get("jurisdiction") or "",
        method=VerificationClaim.Method.SELF_ATTESTED,
        checked_at=timezone.now(),
        status=VerificationClaim.Status.ACTIVE,
    )

    # Hand off to the helper bot, if this rung has one that may run without
    # somebody deciding to spend. It writes its own claims and supersedes by
    # fact type; it never edits the one above.
    result = run_verifier_for(
        person,
        corridor,
        rung,
        Submission(
            rung_key=rung_key,
            value=form.validated_data.get("value") or "",
            jurisdiction=form.validated_data.get("jurisdiction") or "",
        ),
    )
    if result is not None:
        record_findings(person, corridor, rung, result)

    # Background checks that have never run for this person in this corridor.
    # Here as well as on saving a date of birth, because a person who gave
    # their details before the corridor had any checks would otherwise never
    # be screened.
    run_screens_quietly(person, corridor=corridor, only_missing=True)

    # The rung-level claim is a fallback record, not a fact in its own right:
    # `cv: ""` says nothing, and `licence: R123456` says the same thing as the
    # bot's `licence_number: R123456`. So it stands only while nothing more
    # specific does.
    #
    # Checked against the rung as a whole rather than against this call's
    # findings, because the client uploads a document first and submits the
    # rung second — so by the time this runs, the facts usually arrived on the
    # other route and this call recorded nothing.
    #
    # Retired, never deleted. It is still true that the person typed it, and
    # that history is what makes a check challengeable months later.
    more_specific = (
        VerificationClaim.objects.filter(
            person=person, rung_key=rung_key, status=VerificationClaim.Status.ACTIVE
        )
        .exclude(pk=claim.pk)
        .exists()
    )
    if more_specific:
        VerificationClaim.objects.filter(pk=claim.pk).update(
            status=VerificationClaim.Status.SUPERSEDED
        )

    # Answer with whatever now stands for this rung, which may be the bot's
    # finding rather than what the person typed a moment ago.
    claim = headline_claim(person, rung_key) or claim

    return Response(VerificationClaimSerializer(claim).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def claim_status(request, rung_key: str):
    """
    The current claim for one rung. Expiry is computed on read as well as here,
    so a fact confirmed in March and read in September is never served as
    current on the strength of a stored flag.
    """
    person = _person_for(request)
    if person is None:
        return _error("unauthorised", "No person on this request.", status.HTTP_401_UNAUTHORIZED)

    # A rung can produce several claims now, so this answers with the strongest
    # of them: a client polling "has the verifier answered" should stop as soon
    # as any fact has been raised above what the person typed.
    claim = headline_claim(person, rung_key)
    if claim is None:
        return _error("not_found", "Nothing recorded for that step.", status.HTTP_404_NOT_FOUND)

    return Response(VerificationClaimSerializer(_freshen(claim)).data)


def _freshen(claim: VerificationClaim) -> VerificationClaim:
    """
    Recompute expiry on read.

    A licence confirmed in January is not still current in September because a
    stored flag says so, and no background job runs to say otherwise. Shared by
    the single-rung route and by /api/me/ so the two cannot answer differently
    about the same claim.
    """
    if (
        claim.status == VerificationClaim.Status.ACTIVE
        and claim.expires_at
        and claim.expires_at <= timezone.now()
    ):
        claim.status = VerificationClaim.Status.EXPIRED
        claim.save(update_fields=["status"])
    return claim


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def rung_document(request, rung_key: str):
    """
    Evidence for a rung, handed to its bot and then dropped.

    careers stores no credential documents. What survives is `evidence_hash`,
    which makes a check reproducible and challengeable months later without us
    holding the file — so this writes the upload to a temporary path only for
    as long as the verifier needs to read it, and deletes it in a finally.

    It runs the bot inline, which is wrong for anything but development: a
    parse is a model call over a document and takes seconds. `run_verifier_for`
    is written to be called from anywhere, so moving this into a queued task is
    a change at this line and nowhere else.
    """
    person = _person_for(request)
    if person is None:
        return _error("unauthorised", "No person on this request.", status.HTTP_401_UNAUTHORIZED)

    corridor_key = request.data.get("corridor_key") or ""
    corridor = Corridor.objects.filter(key=corridor_key, is_active=True).first()
    if corridor is None:
        corridor = Corridor.objects.filter(is_active=True).first()
    rung = CorridorRung.objects.filter(corridor=corridor, key=rung_key).first() if corridor else None
    if rung is None:
        return _error("not_found", "That step is not part of this corridor.", status.HTTP_404_NOT_FOUND)

    uploaded = request.FILES.get("file")
    if uploaded is None:
        return _error("bad_code", "No file was sent.", status.HTTP_400_BAD_REQUEST)

    hasher = hashlib.sha256()
    suffix = os.path.splitext(uploaded.name)[1] or ".bin"
    handle, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(handle, "wb") as out:
            for chunk in uploaded.chunks():
                hasher.update(chunk)
                out.write(chunk)
        evidence_hash = hasher.hexdigest()

        result = run_verifier_for(
            person,
            corridor,
            rung,
            Submission(rung_key=rung_key, file_paths=[temp_path], evidence_hash=evidence_hash),
        )
        facts = 0
        if result is not None:
            facts = len(record_findings(person, corridor, rung, result, evidence_hash=evidence_hash))
    finally:
        # The file goes, always. Whatever happened above, we do not keep it.
        try:
            os.remove(temp_path)
        except OSError:
            logger.warning("careers: could not remove temp upload %s", temp_path)

    return Response({"rung_key": rung_key, "evidence_hash": evidence_hash, "facts": facts})


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def me(request):
    """
    The person and everything recorded about them, in one call.

    This is what lets somebody come back. Until it existed the app could write
    claims and never read them, so a person who closed the tab returned to an
    empty ladder while their facts sat in the database unreachable.

    GET returns the person plus their live claims. PATCH saves the details they
    typed, and is the only thing that has ever written to Person from the open
    signup path — those fields were collected, shown back, and dropped.
    """
    person = _person_for(request)
    if person is None:
        return _error("unauthorised", "No person on this request.", status.HTTP_401_UNAUTHORIZED)

    if request.method == "PATCH":
        form = PersonUpdateSerializer(data=request.data, partial=True)
        form.is_valid(raise_exception=True)
        data = dict(form.validated_data)
        consent = data.pop("screening_consent", None)
        changed = [field for field, value in data.items() if value is not None]
        identity_before = _screening_identity(person)
        for field in changed:
            setattr(person, field, data[field])
        # Agreeing stamps the time once; agreeing again keeps the original.
        # Withdrawing clears it, and checks stop running from then on.
        if consent is True and person.screening_consent_at is None:
            person.screening_consent_at = timezone.now()
            changed.append("screening_consent_at")
        elif consent is False and person.screening_consent_at is not None:
            person.screening_consent_at = None
            changed.append("screening_consent_at")
        if changed:
            person.save(update_fields=[*changed, "updated_at"])
        # Background checks run on name and date of birth, so they re-run when
        # either changes — and only then, because SAM.gov allows few calls.
        if _screening_identity(person) != identity_before:
            run_screens_quietly(person)

    claims = (
        VerificationClaim.objects.filter(person=person)
        .exclude(status=VerificationClaim.Status.SUPERSEDED)
        .order_by("rung_key", "-checked_at")
    )

    return Response(
        {
            "person": PersonSerializer(person).data,
            "claims": [VerificationClaimSerializer(_freshen(c)).data for c in claims],
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def escalation_statuses(request):
    """
    Current status for the query ids the client is still showing as pending.
    This is what lets a bubble flip to answered without a new message arriving,
    which the backend was already built for and the student client never used.

    The response carries the status and nothing else. The candidate is told the
    practice is being asked, never how many hops it took inside the practice.

    Scoped to the caller. Query ids are guessable, and filtering on id alone
    would answer for any candidate's question, so the person is resolved first
    and the lookup is narrowed to their own rows.
    """
    person = _person_for(request)
    if person is None:
        return _error("unauthorised", "No person on this request.", status.HTTP_401_UNAUTHORIZED)

    query_ids = request.data.get("query_ids") or []
    if not isinstance(query_ids, list):
        return _error("bad_code", "query_ids must be a list.", status.HTTP_400_BAD_REQUEST)
    if not query_ids:
        return Response([])

    from django_api.models import PendingQuery  # local import: careers must not import at module load

    try:
        rows = list(
            PendingQuery.objects.filter(
                id__in=[str(q) for q in query_ids], person_id=person.person_id
            )
        )
    except FieldError:
        # PendingQuery is still owner-typed by university and student (the open
        # item in the brief). Until it carries person_id there is no way to
        # scope this, and refusing is the only safe answer: serving the rows
        # unscoped is how one candidate reads another's escalation.
        #
        # The client treats a failed poll as a no-op and retries, so this
        # degrades to "the bubble does not flip yet" rather than an error the
        # person has to read.
        return _error(
            "server",
            "PendingQuery is not person-scoped yet; refusing to serve unscoped rows.",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    payload = [{"query_id": str(row.id), "status": _external_status(row.status)} for row in rows]
    serializer = EscalationStatusSerializer(data=payload, many=True)
    serializer.is_valid(raise_exception=True)
    return Response(serializer.data)


def _external_status(internal: str) -> str:
    """
    Collapse whatever the queue calls its states into the three the candidate
    may see. Anything mid-flight reads as pending, because the internal hops are
    not the candidate's business.
    """
    if internal in ("answered", "resolved", "learned"):
        return "answered"
    if internal in ("closed", "withdrawn", "expired"):
        return "closed"
    return "pending"


# --- the open front door --------------------------------------------------
#
# Careers is not an invitation corridor. Kormic Student is: a university hands
# over a list and the people on it claim their row. Careers is the other way
# round, because the person is the supply side and gating who may exist would
# gate the thing the practice is paying to see. Anyone may join.
#
# The claim flow stays for the case where a practice or a staffing partner does
# bring a roster, and both paths land on the same ladder at the same rung. The
# only difference is what arrives prefilled and whether the address is pinned.


def _account_model():
    """
    Resolved by name so careers never imports the accounts app at module load.
    The dev project points this at its stub; the real project points it at
    accounts.Account, which accounts-multirole.patch gives person_id and roles.
    """
    from django.apps import apps

    label = getattr(settings, "CAREERS_ACCOUNT_MODEL", "accounts.Account")
    return apps.get_model(label)


def _normalise_email(email: str) -> str:
    return email.strip().lower()


@api_view(["POST"])
@permission_classes([AllowAny])
def signup_start(request):
    """
    Sends a code to the address. The response is identical whether the address
    is new, already registered, or was on a practice's roster, because a signup
    door that answers differently is an account-enumeration oracle. Someone who
    already has an account gets an email saying so rather than a different HTTP
    response.
    """
    form = SignupStartSerializer(data=request.data)
    form.is_valid(raise_exception=True)
    email = _normalise_email(form.validated_data["email"])
    now = timezone.now()

    # A fresh code supersedes any live one, so a person who asks twice uses the
    # code from the email they are actually looking at.
    SignupCode.objects.filter(email=email, consumed_at__isnull=True).update(consumed_at=now)

    code = f"{secrets.randbelow(1000000):06d}"
    SignupCode.objects.create(
        email=email,
        code_hash=SignupCode.hash_code(code),
        expires_at=now + timedelta(minutes=SignupCode.TTL_MINUTES),
    )

    # Delivery is the notification layer's job. In DEBUG the code is logged so
    # the ladder can be walked locally; it is never returned in the response,
    # because that would make the code pointless.
    if settings.DEBUG:
        logger.info("careers signup code for %s: %s", email, code)

    return Response({"email": email}, status=status.HTTP_202_ACCEPTED)


@api_view(["POST"])
@permission_classes([AllowAny])
def signup_verify(request):
    """
    Proves control of the address and mints the session.

    A person who already exists signs in here rather than being refused, so the
    same door works the second time. That also means a person a practice put on
    a roster can arrive on their own and reach the same Person row, which is why
    the lookup is by address rather than by how they got here.
    """
    form = SignupVerifySerializer(data=request.data)
    form.is_valid(raise_exception=True)
    email = _normalise_email(form.validated_data["email"])
    submitted = form.validated_data["code"]
    now = timezone.now()

    record = SignupCode.objects.filter(email=email, consumed_at__isnull=True).first()
    if record is None or not record.is_live(now):
        # Same answer for an unknown address, an expired code and a wrong one.
        return _error("bad_code", "That code did not match.", status.HTTP_400_BAD_REQUEST)

    if record.code_hash != SignupCode.hash_code(submitted):
        record.attempts += 1
        record.save(update_fields=["attempts"])
        if record.attempts >= SignupCode.MAX_ATTEMPTS:
            record.consumed_at = now
            record.save(update_fields=["consumed_at"])
        return _error("bad_code", "That code did not match.", status.HTTP_400_BAD_REQUEST)

    record.consumed_at = now
    record.save(update_fields=["consumed_at"])

    person, _ = Person.objects.get_or_create(email=email)
    User = get_user_model()
    user, _ = User.objects.get_or_create(username=email, defaults={"email": email})

    Account = _account_model()
    account, created = Account.objects.get_or_create(
        user=user, defaults={"role": "candidate", "person_id": person.person_id}
    )
    if not created:
        # An existing account keeps the role it already acts in and gains
        # candidate alongside it, which is the whole point of the plural roles.
        account.add_role("candidate")
        if not account.person_id:
            account.person_id = person.person_id
            account.save(update_fields=["person_id"])

    refresh = RefreshToken.for_user(user)
    return Response(
        {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "person_id": person.person_id,
        }
    )


def _screening_identity(person: Person) -> tuple:
    return (
        (person.full_name or "").strip(),
        person.date_of_birth,
        tuple(person.previous_names or []),
        person.screening_consent_at is not None,
    )
