"""
The seam every helper bot plugs into.

A verifier is handed what the person submitted and answers with findings —
facts it established, each with the method by which it established them. It
does not write to the database, does not know what a claim is, and does not
decide whether it was allowed to run. Those are this app's job, which is what
keeps a bot from being able to raise its own method or overwrite history.

Registration is by the string already on `CorridorRung.verifier`, so adding a
bot is a row in the corridor table plus a register() call, and never a change
to the submission endpoint.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass
class Submission:
    """What the person handed in, plus anything the upload route captured."""

    rung_key: str
    value: str = ""
    jurisdiction: str = ""
    # Local paths to whatever was uploaded, valid only for the length of the
    # run. careers stores no credential documents, so a verifier reads these
    # and the caller deletes them.
    file_paths: List[str] = field(default_factory=list)
    evidence_hash: str = ""
    # What the person already told us, for a verifier to agree or disagree
    # with. Never treated as true — only as something to check a document
    # against. Without it, a CV belonging to somebody else parses perfectly
    # and nothing notices.
    #
    # A plain dict rather than the Person row on purpose: a verifier that can
    # reach the ORM is a verifier that can be moved out of process only by
    # rewriting it.
    subject: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    """
    One fact a verifier established.

    `method` is the verifier stating how it knows, and it is the single most
    load-bearing field in this app. `primary_source` means it confirmed the
    fact directly against the body that issued it. Reading a document the
    person supplied is `source_checked`, however good the parser is.
    """

    fact_type: str
    fact_value: str
    method: str
    source_ref: Optional[str] = None
    expires_at: Optional[Any] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    # `asserts` (a fact about the person) or `screens` (the result of searching
    # a list for them). A screen against an exclusion list is legitimately
    # `primary_source` — the publisher really was asked — so `method` alone
    # cannot tell the two apart, and anything that ranks by method will crown
    # an absence of bad news unless it reads this first.
    shape: str = "asserts"
    # A screen's data vintage, and the identifiers it searched on. Both bound
    # the claim: without them "no exclusion found" is undated and unfalsifiable.
    source_as_of: Optional[Any] = None
    matched_on: List[str] = field(default_factory=list)


@dataclass
class VerifierResult:
    findings: List[Finding] = field(default_factory=list)
    verifier_version: str = ""
    # Set when the bot could not reach its authority at all. Distinct from
    # finding nothing: one is "no answer yet", the other is "the answer is no".
    unavailable: bool = False
    detail: str = ""


class Verifier(Protocol):
    version: str

    def run(self, submission: Submission) -> VerifierResult: ...


_REGISTRY: Dict[str, Verifier] = {}


def register(name: str, verifier: Verifier) -> None:
    _REGISTRY[name] = verifier


def get(name: str) -> Optional[Verifier]:
    return _REGISTRY.get(name) if name else None


def registered() -> List[str]:
    return sorted(_REGISTRY)
