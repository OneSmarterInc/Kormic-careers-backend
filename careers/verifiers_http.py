"""
A verifier that lives somewhere else.

This is the whole careers-side change if the helper bots move into a shared
service. It implements the same `Verifier` protocol as the in-process ones, so
`submit_rung`, the models, the claims and the client are untouched — the only
difference is which object is under a name in the registry.

Two things this file takes seriously that an in-process bot did not have to.

**The service is a different trust domain.** An imported agent was our code. A
JSON response is data from another system, and a method it names is a claim
about how well something was checked. So the method is validated against this
app's own vocabulary and clamped by what the agent is permitted to assert.
A parser that answers `primary_source` is refused, not believed.

**It must fail quietly.** A person handing in a fact should never see an error
because a service was redeploying. Every failure here lands as `unavailable`,
which leaves their claim self_attested and the rung uncheck — honest, and
retryable.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from .verifiers import Finding, Submission, VerifierResult

logger = logging.getLogger(__name__)

# What this app is willing to record, whatever a service says. Anything else is
# dropped rather than mapped to a neighbour, because guessing which method was
# meant is exactly the kind of softening the claim model exists to prevent.
KNOWN_METHODS = {"primary_source", "source_checked", "org_vouched", "self_attested"}


@dataclass
class AgentEndpoint:
    base_url: str
    agent: str
    token: str = ""
    # The strongest method this agent may assert. Reading a document is not
    # confirming with an authority, however good the model is, so a parser is
    # capped at source_checked regardless of what it answers.
    max_method: str = "source_checked"
    timeout_s: float = 10.0
    poll_attempts: int = 30
    poll_interval_s: float = 2.0


_STRENGTH = {"primary_source": 0, "source_checked": 1, "org_vouched": 2, "self_attested": 3}


class HttpVerifier:
    """
    Submits a job, polls it, and maps the answer onto findings.

    Deliberately a job API rather than request-and-wait: a parse takes seconds
    and a repository crawl can take a minute, which is longer than any sensible
    gateway will hold a connection open. careers already speaks this shape —
    the client submits, sees `checking`, and polls — so nothing above has to
    learn a new rhythm.
    """

    def __init__(self, endpoint: AgentEndpoint, transport: Optional["Transport"] = None):
        self.endpoint = endpoint
        self.version = f"{endpoint.agent}@remote"
        self._transport = transport or RequestsTransport()

    def run(self, submission: Submission) -> VerifierResult:
        try:
            job = self._transport.submit(self.endpoint, submission)
        except Exception as exc:
            return VerifierResult(unavailable=True, detail=f"Could not reach agents: {exc}")

        if job.get("status") == "failed":
            return VerifierResult(unavailable=True, detail=str(job.get("detail", "")))

        # A fast agent may answer inline. Only poll when it did not.
        payload = job if "findings" in job else self._await(job.get("job_id", ""))
        if payload is None:
            return VerifierResult(unavailable=True, detail="Agent did not answer in time.")

        return VerifierResult(
            findings=self._findings(payload.get("findings") or []),
            verifier_version=str(payload.get("version") or self.version),
        )

    def _await(self, job_id: str) -> Optional[Dict[str, Any]]:
        if not job_id:
            return None
        for _ in range(self.endpoint.poll_attempts):
            try:
                payload = self._transport.poll(self.endpoint, job_id)
            except Exception as exc:
                # One dropped poll is not the end of the job.
                logger.info("careers: poll of %s failed: %s", job_id, exc)
                payload = None

            if payload:
                status = payload.get("status")
                if status == "done":
                    return payload
                if status == "failed":
                    return None
            time.sleep(self.endpoint.poll_interval_s)
        return None

    def _findings(self, raw: Sequence[Dict[str, Any]]) -> List[Finding]:
        findings: List[Finding] = []
        cap = _STRENGTH.get(self.endpoint.max_method, 1)

        for entry in raw:
            fact_type = str(entry.get("fact_type") or "").strip()
            fact_value = str(entry.get("fact_value") or "").strip()
            method = str(entry.get("method") or "").strip()

            if not fact_type or not fact_value:
                continue

            if method not in KNOWN_METHODS:
                logger.warning(
                    "careers: agent %s returned unknown method %r; dropping %s",
                    self.endpoint.agent, method, fact_type,
                )
                continue

            if _STRENGTH[method] < cap:
                # It claimed more than it is allowed to. Recorded at the cap
                # rather than dropped: the fact is real, the confidence is not.
                logger.warning(
                    "careers: agent %s claimed %s for %s; capped at %s",
                    self.endpoint.agent, method, fact_type, self.endpoint.max_method,
                )
                method = self.endpoint.max_method

            findings.append(
                Finding(
                    fact_type=fact_type,
                    fact_value=fact_value,
                    method=method,
                    source_ref=entry.get("source_ref") or self.endpoint.agent,
                    expires_at=_expiry(entry.get("expires_at")),
                    raw=entry.get("raw") or {},
                )
            )
        return findings


def _expiry(value: Any) -> Optional[date]:
    """
    Read an expiry off the wire, or None.

    Without this the remote path silently dropped expiry: a licence the agent
    said runs out in 2028 arrived as a claim that never expires, and because
    expiry is computed on read, nothing downstream would ever have noticed.
    A bad date is treated as no date rather than raised on — a malformed field
    from a service should not cost the person their whole submission.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        logger.warning("careers: agent returned an unreadable expires_at %r", value)
        return None


# --- transport ------------------------------------------------------------


class Transport:
    """Injected so the mapping and the capping can be tested without a server."""

    def submit(self, endpoint: AgentEndpoint, submission: Submission) -> Dict[str, Any]:
        raise NotImplementedError

    def poll(self, endpoint: AgentEndpoint, job_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


class RequestsTransport(Transport):
    def _headers(self, endpoint: AgentEndpoint) -> Dict[str, str]:
        # A service token, not the person's. The agents service never sees a
        # candidate's session, and has no reason to know who they are.
        return {"Authorization": f"Bearer {endpoint.token}"} if endpoint.token else {}

    def submit(self, endpoint: AgentEndpoint, submission: Submission) -> Dict[str, Any]:
        import requests  # noqa: PLC0415

        url = f"{endpoint.base_url.rstrip('/')}/v1/jobs"
        data = {
            "agent": endpoint.agent,
            "rung_key": submission.rung_key,
            "value": submission.value,
            "jurisdiction": submission.jurisdiction,
            # Lets the service drop a duplicate rather than pay for the same
            # parse twice when a submission is retried.
            "idempotency_key": submission.evidence_hash or "",
        }

        files = [("files", (path.rsplit("/", 1)[-1], open(path, "rb"))) for path in submission.file_paths]
        try:
            response = requests.post(
                url, data=data, files=files or None,
                headers=self._headers(endpoint), timeout=endpoint.timeout_s,
            )
            response.raise_for_status()
            return response.json()
        finally:
            for _, (_, handle) in files:
                handle.close()

    def poll(self, endpoint: AgentEndpoint, job_id: str) -> Optional[Dict[str, Any]]:
        import requests  # noqa: PLC0415

        url = f"{endpoint.base_url.rstrip('/')}/v1/jobs/{job_id}"
        response = requests.get(url, headers=self._headers(endpoint), timeout=endpoint.timeout_s)
        response.raise_for_status()
        return response.json()


# --- registration ---------------------------------------------------------


def register_remote_verifiers(config: Dict[str, Dict[str, Any]], base_url: str, token: str = "") -> None:
    """
    Point named rungs at the shared service.

    Called from AppConfig.ready() when settings name a host. This is the whole
    switch from in-process to remote: the registry key stays the same, so the
    corridor rows, the endpoint and the client never learn that anything moved.

        CAREERS_AGENTS_URL = "https://agents.internal"
        CAREERS_AGENTS = {"resume_parser": {"max_method": "source_checked"}}
    """
    from .verifiers import register  # noqa: PLC0415

    for name, options in config.items():
        register(
            name,
            HttpVerifier(
                AgentEndpoint(
                    base_url=base_url,
                    agent=name,
                    token=token,
                    max_method=options.get("max_method", "source_checked"),
                    timeout_s=options.get("timeout_s", 10.0),
                    poll_attempts=options.get("poll_attempts", 30),
                    poll_interval_s=options.get("poll_interval_s", 2.0),
                )
            ),
        )
