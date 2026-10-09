"""
The bridge from careers to the shared agents package.

careers speaks `Submission` / `VerifierResult`. `kormic_agents` speaks
`Evidence` / `AgentResult`. They say the same things in different words, and
this file is the translation — the only file in careers that knows the package
exists.

That containment is the point. The agents package must never learn what a
corridor, a rung or a Person is, because the moment it does it stops being
reusable and becomes a second copy of careers. So the coupling lives here, in
one file, on the careers side, where it belongs.

Replacing this with `verifiers_http.py` — the same agents behind a URL instead
of an import — changes which object is under a name in the registry and nothing
else. Neither the corridor rows nor the endpoint nor the client ever finds out.

**Nothing is promoted in translation.** The method an agent asserted is the
method careers records. The package already caps what each agent may claim; a
second opinion here would be a second place to get it wrong.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional

from django.utils import timezone

from .verifiers import Finding, Submission, VerifierResult, register

logger = logging.getLogger(__name__)

# Which agent answers for a rung's `verifier` name. Two names per agent,
# because the corridor rows were seeded before the package existed and
# renaming live data to suit a refactor is not a trade worth making.
AGENT_FOR = {
    "resume_parser": "cv",
    "cv": "cv",
    "github": "github",
    "linkedin": "linkedin",
    "licence_bot": "licence",
    "licence": "licence",
    "cert_bot": "certification",
    "certification": "certification",
    "registry_bot": "registry_number",
    "registry_number": "registry_number",
    "npi": "nppes",
    "nppes": "nppes",
    "oig": "oig",
    "exclusion": "oig",
    "sam": "sam",
    "debarment": "sam",
}


class KormicAgent:
    """
    One agent from the package, wearing the careers `Verifier` shape.

    The import is lazy and by name for the same reason the old resume adapter's
    was: careers has to keep working in a deployment where the package is not
    installed. A missing package is "no check ran", never a broken endpoint.
    """

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.version = f"kormic_agents/{agent_name}"

    def run(self, submission: Submission) -> VerifierResult:
        try:
            from kormic_agents import Evidence  # noqa: PLC0415
            from kormic_agents.registry import run as run_agent  # noqa: PLC0415
        except ImportError as exc:
            return VerifierResult(
                unavailable=True, detail=f"kormic_agents is not installed: {exc}"
            )

        result = run_agent(
            self.agent_name,
            Evidence(
                value=submission.value,
                jurisdiction=submission.jurisdiction,
                file_paths=list(submission.file_paths),
                # Lets the agent cross-check the name on a document against the
                # name the person gave us. Without it, a CV belonging to
                # somebody else parses perfectly and nobody notices.
                subject=dict(submission.subject or {}),
                # The service de-duplicates on this, so a retried upload does
                # not pay for the same parse twice.
                idempotency_key=submission.evidence_hash,
            ),
        )

        if result.unavailable:
            return VerifierResult(
                unavailable=True, detail=result.detail, verifier_version=result.version
            )

        # Notes are what a reviewer needs and careers currently has nowhere to
        # put — no claim means "the name on this document is not yours". Logged
        # so the signal is not simply discarded while that gap is open.
        for note in result.notes:
            logger.info("careers: %s on %s — %s", self.agent_name, submission.rung_key, note)

        return VerifierResult(
            findings=[_finding(f, result.notes) for f in result.findings],
            verifier_version=result.version or self.version,
        )


def _finding(finding: Any, notes: List[str]) -> Finding:
    """
    One finding, translated.

    `confidence` has no column on the careers side, so it is carried in `raw`
    rather than dropped — an extraction the agent was only 40% sure of should
    still be distinguishable later from one it was certain about.
    """
    raw: Dict[str, Any] = dict(finding.raw or {})
    raw["confidence"] = finding.confidence
    if notes:
        raw["agent_notes"] = notes

    return Finding(
        fact_type=finding.fact_type,
        fact_value=finding.fact_value,
        method=finding.method,
        source_ref=finding.source_ref,
        expires_at=_expires_at(finding.expires_at),
        raw=raw,
        # Read defensively: a deployment can have an older package installed
        # than this file expects, and the honest default for a package that
        # does not know about screens is that everything it emits is a fact.
        # Defaulting the other way would relabel every ordinary finding as a
        # screen and hide it from the profile.
        shape=getattr(finding, "shape", "asserts") or "asserts",
        source_as_of=getattr(finding, "source_as_of", None),
        matched_on=list(getattr(finding, "matched_on", []) or []),
    )


def _expires_at(value: Any) -> Any:
    """
    A calendar date from an agent, as an aware datetime for the column.

    The package speaks dates, because a licence expires on a day rather than at
    an instant, and careers stores a datetime. Left implicit, Django warned
    about a naive datetime and the value landed at midnight — which makes a
    licence valid *through* the 31st read as expired for the whole of the 31st.
    End of day in UTC is the closest a datetime column gets to what the
    register actually said.
    """
    if not isinstance(value, date) or isinstance(value, datetime):
        return value
    # No explicit tzinfo: `make_aware` uses the project's TIME_ZONE. Django 5
    # removed `django.utils.timezone.utc`, and reaching for `datetime.timezone`
    # here would hard-code UTC in a place that should follow the setting.
    return timezone.make_aware(datetime.combine(value, time(23, 59, 59)))


def register_kormic_agents(directory: Optional[Any] = None, leie: Optional[Any] = None) -> bool:
    """
    Point every rung name at the shared package.

    Returns whether it worked, so `AppConfig.ready` can fall back to whatever
    this deployment shipped with instead of starting up half-wired.

    `directory` carries the credential authorities. Passing None is correct
    until a register is actually integrated: every licence check then reports
    "not confirmed with anyone", which is true.

    `leie` is the OIG exclusion list. Passing None leaves the `oig` rung names
    unregistered rather than registering an agent with no list — a screen that
    always answers "nobody was screened" is worse than an absent rung, because
    it looks like a check that ran. The file is a monthly download and belongs
    to this project's data pipeline, not to the package.
    """
    try:
        from kormic_agents import (  # noqa: PLC0415
            CertificationAgent,
            ClaudeExtractor,
            CVAgent,
            Directory,
            GitHubAgent,
            LicenceAgent,
            LinkedInAgent,
            NppesAgent,
            OigAgent,
            RegistryNumberAgent,
            SamAgent,
            registry as agent_registry,
        )
    except ImportError as exc:
        logger.info("careers: kormic_agents not available (%s)", exc)
        return False

    extractor = ClaudeExtractor()
    directory = directory if directory is not None else Directory()

    agent_registry.register("cv", CVAgent(extractor))
    agent_registry.register("github", GitHubAgent(extractor))
    agent_registry.register("linkedin", LinkedInAgent(extractor))
    agent_registry.register("licence", LicenceAgent(directory, extractor))
    agent_registry.register("certification", CertificationAgent(directory, extractor))
    agent_registry.register("registry_number", RegistryNumberAgent(directory, extractor))
    # No directory and no extractor: NPPES is a public CMS endpoint with no key
    # and no contract, so this is the one rung that reaches a real register
    # without anybody negotiating access first.
    agent_registry.register("nppes", NppesAgent())
    if leie is not None:
        agent_registry.register("oig", OigAgent(leie))

    # SAM needs only a free key, so it wires itself when one is present.
    # Without a key the agent answers "nobody was screened" rather than a
    # clean screen, so leaving the rung out is about not showing a check
    # that cannot run, not about safety.
    sam_key = os.environ.get("SAM_API_KEY", "")
    if sam_key:
        agent_registry.register("sam", SamAgent(api_key=sam_key))

    unconfigured = set()
    if leie is None:
        unconfigured.add("oig")
    if not sam_key:
        unconfigured.add("sam")
    wired = {
        rung_name: agent_name
        for rung_name, agent_name in AGENT_FOR.items()
        if agent_name not in unconfigured
    }
    for rung_name, agent_name in wired.items():
        register(rung_name, KormicAgent(agent_name))

    for name, why in (
        ("oig", "no LEIE file source configured"),
        ("sam", "no SAM_API_KEY set"),
    ):
        if name in unconfigured:
            logger.info(
                "careers: %s screening is not wired (%s). A rung using it will "
                "report that no verifier is registered, which is true.", name, why
            )
    logger.info("careers: registered %d rung names against kormic_agents", len(wired))
    return True
