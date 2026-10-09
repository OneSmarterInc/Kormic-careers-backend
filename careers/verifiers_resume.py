"""
The resume parser, as a careers verifier.

The agent itself lives in `agents/resume_parser.py` in the main project and is
not vendored here. This is the adapter: it maps that agent's output onto
findings, and it is where the two decisions live that the agent cannot make for
itself.

**Everything it produces is `source_checked`, never `primary_source`.** The
parser read a document the candidate supplied. That is a real check and worth
showing a practice, but nobody asked the university whether the degree is real.
Only a bot that confirms directly against the issuing body may raise a claim to
primary_source, and reading a PDF is not that however good the model is.

**One finding per fact.** A CV asserts a name, an institution and a work
history; a practice weighs those differently, so they are three claims rather
than one blob with a resume in it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .verifiers import Finding, Submission, VerifierResult

logger = logging.getLogger(__name__)

# The agent's keys, and what each becomes in the claim table. Anything the
# agent returns that is not named here is deliberately dropped: a fact nobody
# has decided how to show a practice should not quietly become a claim.
#
# The agent was written for a student resume, so it also returns gre_quant,
# toefl and a hardcoded `program`. Those are not facts about a clinician and
# are not mapped.
FACT_MAP = {
    "name": "full_name",
    "email": "email",
    "institution": "institution",
    "major": "field_of_study",
    "graduation_year": "graduation_year",
    "work_months": "work_experience_months",
    "publications_count": "publications",
}


def _load_agent():
    """
    Imported lazily and by name.

    careers must not import from the agents package at module load: the app has
    to keep working in a deployment where the agents are not installed, and a
    missing bot is "no check ran" rather than a broken endpoint.
    """
    from agents.resume_parser import ResumeParserAgent  # noqa: PLC0415

    return ResumeParserAgent()


class ResumeParserVerifier:
    version = "resume_parser/1"

    def run(self, submission: Submission) -> VerifierResult:
        if not submission.file_paths:
            return VerifierResult(unavailable=True, detail="No document to read.")

        try:
            agent = _load_agent()
        except Exception as exc:
            # Not installed in this deployment. The claim stands as the person
            # left it and the rung is not marked as checked.
            return VerifierResult(unavailable=True, detail=f"Parser unavailable: {exc}")

        parsed: Dict[str, Any] = agent.parse(submission.file_paths[0])
        return VerifierResult(
            findings=findings_from(parsed),
            verifier_version=self.version,
        )


def findings_from(parsed: Dict[str, Any]) -> List[Finding]:
    """
    Pure, so the mapping can be tested without an API key or a PDF.

    Blank and zero values are dropped rather than recorded. "We read your CV
    and it says your work history is 0 months" is not a fact established, it is
    a field the parser did not fill.
    """
    findings: List[Finding] = []

    for source_key, fact_type in FACT_MAP.items():
        value = _clean(parsed.get(source_key))
        if not value:
            continue
        findings.append(
            Finding(
                fact_type=fact_type,
                fact_value=value,
                method="source_checked",
                source_ref="resume",
                raw={"field": source_key},
            )
        )

    skills = parsed.get("technical_skills") or parsed.get("skills") or []
    if isinstance(skills, list) and skills:
        findings.append(
            Finding(
                fact_type="skills",
                fact_value=", ".join(str(s) for s in skills[:20]),
                method="source_checked",
                source_ref="resume",
                raw={"count": len(skills)},
            )
        )

    return findings


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Zero is the parser's default for an unfilled number, not a finding.
        return str(value) if value else None
    text = str(value).strip()
    if not text or text.lower() in {"none", "none stated", "n/a", "unknown", "student"}:
        return None
    return text


def register_builtin_verifiers() -> None:
    """Called from AppConfig.ready(). Import-time side effects stay out of models."""
    from .verifiers import register

    register("resume_parser", ResumeParserVerifier())
