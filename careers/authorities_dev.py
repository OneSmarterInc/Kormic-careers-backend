"""
Authorities for local testing.

DEV FIXTURE — registered only when DEBUG is on, and never a substitute for an
integration. Nothing in here talks to a real register, and the demo one below
invents its answers from the number it is given.

It exists because the interesting behaviour of a credential check is invisible
until an authority actually answers: confirmed-and-current reads differently
from confirmed-but-expired, which reads differently from suspended, which reads
differently from no-such-number. Until a real register is wired up, every one
of those collapses into "not confirmed with anyone" and there is nothing to
look at.

Two very different things live here, and the difference matters:

  `nmc_format` is REAL and safe to keep. It checks that a number is the right
  shape for the NMC and nothing more, and it says `self_attested` — because a
  regular expression cannot confirm a licence, only catch a typo before a
  person is asked to wait a week for a human to spot it.

  `DemoRegister` is a FAKE. It returns `primary_source` — the strongest claim
  this system can make — for numbers matching a prefix. That is only tolerable
  because it is behind DEBUG and behind a jurisdiction code nobody would type
  by accident. It must never be registered against a real jurisdiction.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict

logger = logging.getLogger(__name__)

# The jurisdiction a person types to reach the fake register. Deliberately not
# a real place: if this ever leaked into a deployment, a claim made against
# "DEMO" is obviously a test artefact rather than something a practice might
# act on.
DEMO_JURISDICTION = "DEMO"


class DemoRegister:
    """
    A register that answers from the number's prefix, so every branch of the
    model can be driven from the UI.

        RN-OK-1234    confirmed, current
        RN-EXP-1234   confirmed, but expired last year
        RN-SUS-1234   confirmed, and suspended
        RN-OTHER-123  confirmed, but registered to somebody else
        anything else no record

    The default is deliberately "no record" rather than "found". A fixture whose
    happy path is the fallback teaches the wrong reflex — and if this ever ran
    somewhere it should not, refusing by default is the safer failure.
    """

    key = "demo-register"
    name = "Demo Register"

    def check(self, number: str, kind: str, subject: Dict[str, Any]):
        from kormic_agents.credentials import Outcome, name_mismatch  # noqa: PLC0415

        upper = (number or "").upper()
        holder = subject.get("full_name") or "The Registered Person"
        today = date.today()
        label = {"licence": "Registered Nurse", "certification": "Advanced Life Support"}.get(
            kind, "Registered Professional"
        )

        common = dict(
            method="primary_source",
            credential_type=label,
            issued_on=today - timedelta(days=900),
            source_ref=f"https://demo-register.invalid/{kind}/{number}",
            raw={"fixture": True},
        )

        if "-OK-" in upper:
            return Outcome(found=True, status="Active", holder_name=holder,
                           expires_on=today + timedelta(days=730), **common)

        if "-EXP-" in upper:
            # Found and genuinely issued — it has simply run out. The agent
            # records the expiry and the expiry is judged on read, so this row
            # becomes stale on its own with nothing having to notice.
            # "Lapsed" rather than "Active": a register that has noticed the
            # renewal date passed says so. The expiry still does the real work,
            # because it is judged on read.
            return Outcome(found=True, status="Lapsed", holder_name=holder,
                           expires_on=today - timedelta(days=200), **common)

        if "-SUS-" in upper:
            # Not the same as missing. A suspended licence exists and belongs
            # to this person; they may not currently practise on it.
            return Outcome(found=True, status="Suspended", holder_name=holder,
                           expires_on=today + timedelta(days=365), **common)

        if "-OTHER-" in upper:
            # The most valuable thing a register check produces after existence
            # itself: a real number belonging to somebody else.
            other = Outcome(found=True, status="Active", holder_name="Priya Deshpande",
                            expires_on=today + timedelta(days=365), **common)
            warning = name_mismatch(subject.get("full_name"), other.holder_name)
            if warning:
                other.notes.append(warning)
            return other

        return Outcome(
            found=False, method="primary_source", status="not_found",
            source_ref=common["source_ref"], raw={"fixture": True},
            detail="The Demo Register has no record of this number.",
        )


def build_directory():
    """
    The authorities this deployment can reach.

    Returns an empty directory if the agents package is missing, which leaves
    every credential honestly unchecked rather than breaking startup.
    """
    try:
        from kormic_agents.credentials import Directory, FormatAuthority  # noqa: PLC0415
    except ImportError:
        return None

    directory = Directory()

    # REAL, and safe in production. An NMC PIN is two digits, a letter, four
    # digits and a letter — 01A2345N. Catching a mistyped one here saves a
    # person a week of waiting to be told to check it.
    #
    # The pattern is confirmed against the NMC's own published format, not
    # inferred from examples. It is still only a shape check: it says nothing
    # about whether the PIN was ever issued, which is why it reports
    # self_attested. Confirming that needs the NMC's Employer Confirmations
    # service, which wants a PIN *and* a date of birth and is open to employers
    # rather than to candidate-side platforms.
    nmc_format = FormatAuthority(
        key="nmc-format", name="the NMC",
        pattern=r"\d{2}[A-Z]\d{4}[A-Z]", example="18J1234E",
    )
    directory.register("licence", "GB", nmc_format)
    directory.register("registry_number", "GB", nmc_format)

    demo = DemoRegister()
    for kind in ("licence", "certification", "registry_number"):
        directory.register(kind, DEMO_JURISDICTION, demo)

    # Certification has no jurisdiction field on the form, so the value arrives
    # blank. Registering the fixture against the empty key is what makes that
    # rung testable at all — and is exactly the line to delete first when a
    # real awarding body is integrated.
    directory.register("certification", "", demo)

    logger.info(
        "careers: DEV authorities registered — NMC format check (real), "
        "Demo Register fixture on jurisdiction %r (fake)", DEMO_JURISDICTION,
    )
    return directory
