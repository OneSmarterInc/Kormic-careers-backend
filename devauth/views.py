"""
DEV STUB — delete on integration.

Everything the client calls that the careers app does not serve itself. The
real implementations live elsewhere in the main project: the claim front door
is institutes_list, chat history and send are django_api. Neither is
reimplemented here in any meaningful sense — these exist so the ladder can be
walked against a real server instead of against fixtures.

Two things are kept honest even in the stubs, because they are rules rather
than implementation:

  * A wrong code and an unknown invitation answer identically. Telling them
    apart is how a roster gets enumerated.
  * The document endpoint hashes what arrives and drops it. careers stores no
    credential documents on purpose, and a stub that quietly kept them would
    be modelling the wrong thing.

Every view refuses to work unless DEBUG is on.
"""
import hashlib
import secrets
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from careers.models import Person
from django_api.models import PendingQuery

from .models import Account

DEV_CODE = "123456"


def _dev_only() -> Response:
    return Response(
        {"code": "not_found", "detail": "Not available."}, status=status.HTTP_404_NOT_FOUND
    )


def _now() -> str:
    return datetime.now(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def _person_for(request) -> Person | None:
    account = getattr(request.user, "account", None)
    if account is None or not account.person_id:
        return None
    return Person.objects.filter(person_id=account.person_id).first()


def _session_for(user, person) -> dict:
    """One name per field, matching WireSession in the client's contract.ts."""
    refresh = RefreshToken.for_user(user)
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "person_id": person.person_id,
    }


def _link(email: str, full_name: str = "Sample Person"):
    """Creates or finds the user, person and account for a dev session."""
    user, _ = User.objects.get_or_create(username=email, defaults={"email": email})
    person, _ = Person.objects.get_or_create(email=email, defaults={"full_name": full_name})
    account, created = Account.objects.get_or_create(
        user=user, defaults={"role": Account.Role.CANDIDATE, "person_id": person.person_id}
    )
    if not created and account.person_id != person.person_id:
        account.person_id = person.person_id
        account.save(update_fields=["person_id"])
    return user, person


# --- a session without the claim flow -------------------------------------


@api_view(["POST"])
@permission_classes([AllowAny])
def dev_login(request):
    """Shortcut for testing an authenticated screen directly. Body: {"email": ...}."""
    if not settings.DEBUG:
        return _dev_only()
    email = (request.data.get("email") or "dev@example.com").strip().lower()
    user, person = _link(email, "Dev Person")
    return Response(_session_for(user, person))


# --- the claim front door -------------------------------------------------


@api_view(["POST"])
@permission_classes([AllowAny])
def claim_start(request):
    """Returns a masked address and nothing else, which is the real rule."""
    if not settings.DEBUG:
        return _dev_only()
    if not (request.data.get("token") or "").strip():
        return Response(
            {"code": "not_found", "detail": "No such invitation."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response({"masked_email": "p•••@•••.com"})


@api_view(["POST"])
@permission_classes([AllowAny])
def claim_verify(request):
    if not settings.DEBUG:
        return _dev_only()
    if (request.data.get("code") or "").strip() != DEV_CODE:
        return Response(
            {"code": "bad_code", "detail": "That code did not match."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(
        {
            "claim_token": "claim_" + secrets.token_urlsafe(8),
            "pinned_email": "person@example.com",
            # Keys are the client's Person fields. WireClaimVerify declares
            # prefill as Partial<Record<keyof Person, string>>, so this one
            # object is camel case on purpose while everything else is not.
            "prefill": {"fullName": "Sample Person", "country": "United States"},
        }
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def claim_confirm(request):
    """Mints the session. The real one countersigns divergences from the roster."""
    if not settings.DEBUG:
        return _dev_only()

    email = "person@example.com"
    user, person = _link(email, request.data.get("full_name") or "Sample Person")

    for field in ("full_name", "phone", "city", "region", "country"):
        value = request.data.get(field)
        if value:
            setattr(person, field, value)
    person.save()

    return Response(_session_for(user, person))


# --- chat -----------------------------------------------------------------


def _wire_message(row_id, role, content, escalation=None) -> dict:
    return {
        "id": str(row_id),
        "role": role,
        "content": content,
        "created_at": _now(),
        "escalation": escalation,
    }


def _visible(internal: str) -> str:
    if internal in ("answered", "resolved", "learned"):
        return "answered"
    if internal in ("closed", "withdrawn", "expired"):
        return "closed"
    return "pending"


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def chat_history(request):
    if not settings.DEBUG:
        return _dev_only()
    rows = [_wire_message("m_1", "navigator", "Ask me anything about this position.")]
    person = _person_for(request)
    if person:
        queries = PendingQuery.objects.filter(person_id=person.person_id).order_by("created_at")
        for query in queries:
            rows.append(
                _wire_message(
                    "m_" + str(query.id),
                    "navigator",
                    "I do not have that from the practice yet: " + query.question,
                    {"query_id": str(query.id), "status": _visible(query.status)},
                )
            )
    return Response(rows)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def chat_send(request):
    """
    Answers nothing and escalates everything, which is the interesting path: it
    produces a pending bubble the client then polls to completion.

    The PendingQuery row it writes is real, so that poll runs against the
    actual careers endpoint rather than against another stub.
    """
    if not settings.DEBUG:
        return _dev_only()
    person = _person_for(request)
    if person is None:
        return Response(
            {"code": "unauthorised", "detail": "No person on this request."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    text = (request.data.get("content") or "").strip()
    query = PendingQuery.objects.create(
        id="q_" + secrets.token_urlsafe(6),
        person_id=person.person_id,
        status="pending",
        question=text,
    )
    return Response(
        _wire_message(
            "m_" + str(query.id),
            "navigator",
            "I do not have that from the practice yet: " + text,
            {"query_id": str(query.id), "status": "pending"},
        )
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def chat_rename(request):
    if not settings.DEBUG:
        return _dev_only()
    person = _person_for(request)
    name = (request.data.get("name") or "").strip()
    if person and name:
        # Person.agent_name is globally unique, which breaks the moment two
        # people pick the same word. Suffixed here so the dev server does not
        # 500 on it — the model is what actually needs changing.
        person.agent_name = name + "#" + person.person_id[-4:]
        person.save(update_fields=["agent_name"])
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def dev_answer(request):
    """Marks the caller's open questions answered, so a bubble can be seen to flip."""
    if not settings.DEBUG:
        return _dev_only()
    person = _person_for(request)
    if person is None:
        return Response({"answered": 0})
    count = PendingQuery.objects.filter(
        person_id=person.person_id, status="pending"
    ).update(status="answered")
    return Response({"answered": count})


# --- document upload ------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def rung_document(request, rung_key: str):
    """
    DEV STUB — and a deliberate shape for the real one.

    careers stores no credential documents: it keeps source_ref and
    evidence_hash so a check stays reproducible and challengeable without us
    holding the file. So this hashes what arrives and drops it. A real
    implementation may hand the bytes to a verifier bot, but it must not end
    with the file in our storage either.
    """
    if not settings.DEBUG:
        return _dev_only()

    uploaded = request.FILES.get("file")
    digest = ""
    if uploaded is not None:
        hasher = hashlib.sha256()
        for chunk in uploaded.chunks():
            hasher.update(chunk)
        digest = hasher.hexdigest()

    return Response({"rung_key": rung_key, "evidence_hash": digest})


# --- push registration ----------------------------------------------------


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
def push_register(request):
    """DEV STUB. Accepts and discards; the client treats failure as no push."""
    if not settings.DEBUG:
        return _dev_only()
    return Response(status=status.HTTP_204_NO_CONTENT)
