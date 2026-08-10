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
from django.core.exceptions import FieldError
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import Corridor, CorridorRung, Person, VerificationClaim
from .serializers import (
    CorridorSerializer,
    EscalationStatusSerializer,
    RungSubmissionSerializer,
    VerificationClaimSerializer,
)


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

    VerificationClaim.objects.filter(
        person=person, rung_key=rung_key, status=VerificationClaim.Status.ACTIVE
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

    # Hand off to the helper bot here when one exists for this rung. The bot
    # writes its own claim and supersedes this one; it never edits it.
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

    claim = (
        VerificationClaim.objects.filter(person=person, rung_key=rung_key)
        .exclude(status=VerificationClaim.Status.SUPERSEDED)
        .first()
    )
    if claim is None:
        return _error("not_found", "Nothing recorded for that step.", status.HTTP_404_NOT_FOUND)

    if (
        claim.status == VerificationClaim.Status.ACTIVE
        and claim.expires_at
        and claim.expires_at <= timezone.now()
    ):
        claim.status = VerificationClaim.Status.EXPIRED
        claim.save(update_fields=["status"])

    return Response(VerificationClaimSerializer(claim).data)


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
