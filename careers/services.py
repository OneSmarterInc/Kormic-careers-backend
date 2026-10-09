"""
What happens to a submission after it is recorded.

Two rules live here and nowhere else.

A verifier only runs when the corridor says reaching that authority is free.
A paid route waits for a hiring human to decide to spend, and an unset route
behaves as none — because a guess about what an authority costs is not
something to act on.

A raised method supersedes; it never edits. The self_attested claim the person
created by typing stays in the table exactly as they left it, and the bot's
finding is a new row. That is what makes a check challengeable months later,
and it is why nothing in here calls `.save()` on somebody else's claim.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set

from django.db import transaction
from django.utils import timezone

from .models import Corridor, CorridorRung, Person, VerificationClaim
from .verifiers import Finding, Submission, VerifierResult, get as get_verifier

logger = logging.getLogger(__name__)


def may_run_now(rung: CorridorRung) -> bool:
    """
    Whether this rung's bot may run without anybody deciding to spend.

    Null is not "probably fine". It means nobody has established what reaching
    this authority costs, and until they have, nothing runs by itself.
    """
    return rung.route == CorridorRung.Route.FREE


def run_verifier_for(
    person: Person,
    corridor: Corridor,
    rung: CorridorRung,
    submission: Submission,
) -> Optional[VerifierResult]:
    """
    Hands the submission to the rung's bot, if it has one that may run.

    Returns None when nothing ran, which is the ordinary case: most rungs have
    no bot, and most bots that exist cost money to call.
    """
    if not rung.verifier or not may_run_now(rung):
        return None

    # Filled here rather than at every call site, so a new upload route cannot
    # forget it and silently lose the name cross-check.
    #
    # The identity fields are passed whether or not a rung screens a list. An
    # agent that does not need them ignores them, and deciding here which rungs
    # "deserve" a date of birth would mean this function knowing what each
    # agent does — which is the coupling the whole bridge exists to avoid.
    if not submission.subject:
        submission.subject = {
            "full_name": person.full_name,
            "email": person.email,
            # Screening needs identifiers a name cannot supply. Absent is
            # normal and handled: a screen run on a name alone reports itself
            # as weak rather than pretending to be conclusive.
            "date_of_birth": (
                person.date_of_birth.isoformat() if person.date_of_birth else ""
            ),
            "previous_names": list(person.previous_names or []),
        }

    verifier = get_verifier(rung.verifier)
    if verifier is None:
        # Named in the corridor but not registered in this deployment. Not an
        # error the person should see: their claim stands as self_attested.
        logger.info("careers: no verifier registered for %s", rung.verifier)
        return None

    try:
        result = verifier.run(submission)
    except Exception:
        # A bot that fell over does not fail the submission. The person has
        # given us the fact; the check can be retried.
        logger.exception("careers: verifier %s failed on %s", rung.verifier, rung.key)
        return None

    if result.unavailable:
        logger.info("careers: verifier %s unavailable: %s", rung.verifier, result.detail)
        return None

    return result


@transaction.atomic
def record_findings(
    person: Person,
    corridor: Corridor,
    rung: CorridorRung,
    result: VerifierResult,
    evidence_hash: str = "",
) -> List[VerificationClaim]:
    """
    Writes one claim per fact the verifier established.

    A resume states a name, an institution and a work history. Those are three
    things a practice weighs differently, and rolling them into one claim is
    precisely what this model exists to avoid. So the rung is the origin, and
    `fact_type` is what the claim is about.

    Superseding is per fact type, not per rung: a bot that establishes two of
    the three facts must not retire a claim about the third.

    It also happens once, before anything is written, rather than inside the
    loop. A fact type is not single-valued — a CV states two degrees and three
    jobs — and superseding per finding meant each one retired the one before
    it, so a five-finding parse left two claims standing and lost three without
    a trace. What a re-check replaces is the *previous* answer, never the rest
    of its own.

    It is scoped by shape as well as fact type, for the same reason. A screen
    and a fact are different questions about the same person, and a screen that
    retired the fact it was filed alongside would be the identical bug wearing
    a new column.
    """
    written: List[VerificationClaim] = []
    now = timezone.now()

    types_by_shape: Dict[str, Set[str]] = {}
    for finding in result.findings:
        shape = getattr(finding, "shape", "") or VerificationClaim.Shape.ASSERTS
        types_by_shape.setdefault(shape, set()).add(finding.fact_type)

    for shape, fact_types in types_by_shape.items():
        VerificationClaim.objects.filter(
            person=person,
            rung_key=rung.key,
            fact_type__in=fact_types,
            shape=shape,
            status=VerificationClaim.Status.ACTIVE,
        ).update(status=VerificationClaim.Status.SUPERSEDED)

    for finding in result.findings:
        written.append(
            VerificationClaim.objects.create(
                person=person,
                corridor=corridor,
                rung_key=rung.key,
                fact_type=finding.fact_type,
                fact_value=finding.fact_value,
                method=finding.method,
                source_ref=finding.source_ref,
                evidence_hash=evidence_hash,
                verifier=rung.verifier,
                verifier_version=result.verifier_version,
                checked_at=now,
                expires_at=finding.expires_at,
                status=VerificationClaim.Status.ACTIVE,
                raw_finding=finding.raw,
                shape=getattr(finding, "shape", "") or VerificationClaim.Shape.ASSERTS,
                source_as_of=getattr(finding, "source_as_of", None),
                matched_on=list(getattr(finding, "matched_on", []) or []),
            )
        )

    return written


def headline_claim(person: Person, rung_key: str) -> Optional[VerificationClaim]:
    """
    The one claim that stands for a rung.

    A rung can now produce several. The single-rung status route answers with
    the strongest of them, so a client polling "has the verifier answered"
    stops as soon as any fact has been raised above what the person typed.

    **Screens are excluded, and that exclusion is load-bearing.** This ranks by
    method, and a screen against an exclusion list is legitimately
    `primary_source`. Left in, it would sort above everything and this function
    would answer "confirmed with the issuing body" for a person whose licence
    is still sitting at `self_attested` — on the strength of not appearing on a
    list of excluded providers. An absence of bad news is not a credential and
    must never be the headline for one.
    """
    order = {
        VerificationClaim.Method.PRIMARY_SOURCE: 0,
        VerificationClaim.Method.SOURCE_CHECKED: 1,
        VerificationClaim.Method.ORG_VOUCHED: 2,
        VerificationClaim.Method.SELF_ATTESTED: 3,
    }
    claims = list(
        VerificationClaim.objects.filter(person=person, rung_key=rung_key)
        .exclude(status=VerificationClaim.Status.SUPERSEDED)
        .exclude(shape=VerificationClaim.Shape.SCREENS)
    )
    if not claims:
        return None
    claims.sort(key=lambda c: (order.get(c.method, 9), -c.checked_at.timestamp()))
    return claims[0]


def self_attested_finding(rung_key: str, value: str) -> Finding:
    """What the person told us, expressed in the same shape a verifier answers in."""
    return Finding(
        fact_type=rung_key,
        fact_value=value,
        method=VerificationClaim.Method.SELF_ATTESTED,
    )


# --- background checks ----------------------------------------------------


def can_screen(person: Person) -> bool:
    """
    A screen needs consent, a name and a date of birth before it runs.

    Consent first: searching federal exclusion lists for somebody is not
    something to do because they happened to type a date of birth.

    The agents would screen on a name alone and report it as weak, but every
    stranger sharing a name with an excluded provider would then land in a
    manual queue. The date of birth is what clears people, so it is waited for.
    """
    return (
        person.screening_consent_at is not None
        and bool((person.full_name or "").strip())
        and person.date_of_birth is not None
    )


def screening_rungs(corridor: Corridor, verifiers: Optional[Set[str]] = None) -> List[CorridorRung]:
    rungs = (
        CorridorRung.objects.filter(corridor=corridor, input=CorridorRung.Input.AUTOMATIC)
        .exclude(requirement=CorridorRung.Requirement.NOT_APPLICABLE)
        .exclude(verifier__isnull=True)
    )
    if verifiers:
        rungs = rungs.filter(verifier__in=verifiers)
    return list(rungs)


def run_screens(
    person: Person,
    corridor: Optional[Corridor] = None,
    verifiers: Optional[Set[str]] = None,
    only_missing: bool = False,
) -> int:
    """
    Run the automatic checks for this person, in every corridor they are in.

    A person is "in" a corridor once they have a claim there; `corridor` adds
    the one they are acting in right now, before their first claim lands.
    `only_missing` skips checks that already have a live result, so an ordinary
    submission does not spend a rate-limited SAM.gov call re-asking a question
    that was already answered.

    Returns how many checks produced a result. A check that could not run
    writes nothing and leaves any earlier result standing.
    """
    if not can_screen(person):
        return 0

    corridor_ids = set(
        VerificationClaim.objects.filter(person=person).values_list("corridor_id", flat=True)
    )
    if corridor is not None:
        corridor_ids.add(corridor.pk)

    ran = 0
    for current in Corridor.objects.filter(pk__in=corridor_ids, is_active=True):
        for rung in screening_rungs(current, verifiers):
            if only_missing and VerificationClaim.objects.filter(
                person=person, rung_key=rung.key,
                shape=VerificationClaim.Shape.SCREENS,
                status=VerificationClaim.Status.ACTIVE,
            ).exists():
                continue
            result = run_verifier_for(person, current, rung, Submission(rung_key=rung.key))
            if result is not None:
                record_findings(person, current, rung, result)
                ran += 1
    return ran


def run_screens_quietly(person: Person, **kwargs) -> int:
    """
    For request handlers. A background check that fails must never fail the
    thing the person was actually doing, such as saving their date of birth.
    """
    try:
        return run_screens(person, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("careers: background checks failed for %s", person.person_id)
        return 0


def rescreen_everyone(verifiers: Optional[Set[str]] = None) -> int:
    """Re-run checks for everyone who can be screened. Used when a new list arrives."""
    screened = 0
    people = (
        Person.objects.exclude(full_name="")
        .exclude(date_of_birth__isnull=True)
        .exclude(screening_consent_at__isnull=True)
    )
    for person in people.iterator():
        if run_screens_quietly(person, verifiers=verifiers):
            screened += 1
    return screened
