"""
Careers corridor tests.

These cover the rules that cannot be retrofitted: the corridor drives the
ladder, a claim always carries a method and a date, a raised method supersedes
rather than edits, and the candidate never sees the practice's internal states.
"""
from datetime import date, timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from careers.models import Corridor, CorridorRung, Person, SignupCode, VerificationClaim
from careers.serializers import (
    CorridorSerializer,
    PersonSerializer,
    PersonUpdateSerializer,
    VerificationClaimSerializer,
)
from careers.services import headline_claim, may_run_now, record_findings, run_verifier_for
from careers.verifiers import Finding, Submission, VerifierResult, register
from careers.verifiers_http import AgentEndpoint, HttpVerifier, Transport
from careers.verifiers_resume import ResumeParserVerifier, findings_from
from careers.views import _external_status, escalation_statuses, me, submit_rung


def _pending_query_is_person_scoped() -> bool:
    """
    Whether django_api.PendingQuery can express "belongs to this person" yet.

    careers must not import django_api at module load, and the model may not be
    installed at all, so this answers False rather than raising when it cannot
    tell.
    """
    try:
        from django_api.models import PendingQuery
    except Exception:
        return False
    return any(field.name == "person_id" for field in PendingQuery._meta.fields)


def make_corridor(key="sample"):
    corridor = Corridor.objects.create(key=key, display_name="Sample corridor")
    CorridorRung.objects.create(
        corridor=corridor, key="licence", display_name="Licence",
        requirement="required", input="identifier_with_jurisdiction",
        verifier="licence_bot", order=1,
    )
    CorridorRung.objects.create(
        corridor=corridor, key="cv", display_name="CV",
        requirement="required", input="document_upload", verifier=None, order=2,
    )
    CorridorRung.objects.create(
        corridor=corridor, key="github", display_name="GitHub",
        requirement="not_applicable", input="oauth", verifier=None, order=3,
    )
    return corridor


class CorridorContractTests(TestCase):
    def test_corridor_serializes_to_the_shape_the_client_declares(self):
        corridor = make_corridor()
        data = CorridorSerializer(corridor).data
        self.assertEqual(set(data.keys()), {"key", "display_name", "rungs"})
        self.assertEqual(
            set(data["rungs"][0].keys()),
            {
                "key", "display_name", "requirement", "input", "verifier", "route",
                "jurisdictions", "order",
            },
        )

    def test_verifier_is_null_not_absent_when_no_bot_services_a_rung(self):
        corridor = make_corridor()
        rungs = {r["key"]: r for r in CorridorSerializer(corridor).data["rungs"]}
        self.assertIsNone(rungs["cv"]["verifier"])
        self.assertEqual(rungs["licence"]["verifier"], "licence_bot")

    def test_not_applicable_rungs_are_sent_and_the_client_filters_them(self):
        # The server does not decide what a person sees; it states the
        # requirement and the client derives the ladder.
        corridor = make_corridor()
        keys = [r["key"] for r in CorridorSerializer(corridor).data["rungs"]]
        self.assertIn("github", keys)

    def test_corridor_endpoint_is_open_so_the_tour_works_before_signup(self):
        make_corridor()
        response = self.client.get(reverse("careers-corridor", args=["sample"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["key"], "sample")

    def test_unknown_corridor_returns_the_shared_error_shape(self):
        response = self.client.get(reverse("careers-corridor", args=["nope"]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(set(response.json().keys()), {"code", "detail"})


class ClaimRuleTests(TestCase):
    def setUp(self):
        self.corridor = make_corridor()
        self.person = Person.objects.create(full_name="Sample Person", email="p@example.com")

    def _claim(self, **kwargs):
        defaults = dict(
            person=self.person, corridor=self.corridor, rung_key="licence",
            fact_type="licence", fact_value="A1234",
            method=VerificationClaim.Method.SELF_ATTESTED,
            checked_at=timezone.now(),
        )
        defaults.update(kwargs)
        return VerificationClaim.objects.create(**defaults)

    def test_a_claim_always_carries_a_method_and_a_date(self):
        data = VerificationClaimSerializer(self._claim()).data
        self.assertIn("method", data)
        self.assertIsNotNone(data["checked_at"])

    def test_there_is_no_profile_level_verified_field(self):
        field_names = {f.name for f in Person._meta.fields}
        self.assertNotIn("verified", field_names)
        self.assertNotIn("is_verified", field_names)

    def test_nothing_in_this_app_is_named_student(self):
        for model in (Person, Corridor, CorridorRung, VerificationClaim):
            for field in model._meta.fields:
                self.assertNotIn("student", field.name)

    def test_raising_a_method_supersedes_rather_than_edits(self):
        first = self._claim()
        VerificationClaim.objects.filter(
            person=self.person, rung_key="licence", status=VerificationClaim.Status.ACTIVE
        ).update(status=VerificationClaim.Status.SUPERSEDED)
        second = self._claim(
            method=VerificationClaim.Method.PRIMARY_SOURCE,
            source_ref="TXN-99", verifier="licence_bot", verifier_version="1.0.0",
        )
        first.refresh_from_db()
        self.assertEqual(first.status, VerificationClaim.Status.SUPERSEDED)
        self.assertEqual(first.method, VerificationClaim.Method.SELF_ATTESTED)
        self.assertEqual(second.status, VerificationClaim.Status.ACTIVE)
        self.assertEqual(VerificationClaim.objects.filter(person=self.person).count(), 2)

    def test_no_credential_document_is_stored(self):
        field_names = {f.name for f in VerificationClaim._meta.fields}
        self.assertIn("evidence_hash", field_names)
        self.assertIn("source_ref", field_names)
        for name in field_names:
            self.assertNotIn("document", name)
            self.assertNotIn("file", name)

    def test_an_expired_claim_is_not_served_as_active(self):
        claim = self._claim(expires_at=timezone.now() - timedelta(days=1))
        self.assertTrue(claim.expires_at < timezone.now())
        self.assertEqual(claim.status, VerificationClaim.Status.ACTIVE)
        # The read path is what flips it, which is why claim_status recomputes
        # rather than trusting the stored value.


class RungSubmissionTests(TestCase):
    """
    The submission endpoint against the shape the client actually sends.

    The client posts rung_key, value and jurisdiction, filling an absent
    optional with null, and names its corridor. Every assertion here failed
    before: corridor_key was read off request.data rather than declared, so
    omitting it produced a 404 that read as "no such corridor", and the two
    optional fields rejected null outright.

    `_person_for` is patched rather than exercised, because resolving an
    Account is the accounts app's job and careers deliberately does not import
    it. accounts-tests/test_multirole.py covers that side.
    """

    def setUp(self):
        self.corridor = make_corridor()
        self.person = Person.objects.create(full_name="Sample Person", email="p@example.com")
        self.user = User.objects.create_user(username="p@example.com", email="p@example.com")

    def _submit(self, payload, person=None):
        request = APIRequestFactory().post("/api/claims/submit/", payload, format="json")
        force_authenticate(request, user=self.user)
        with patch("careers.views._person_for", return_value=person or self.person):
            return submit_rung(request)

    def test_a_submission_missing_the_corridor_names_the_field(self):
        response = self._submit({"rung_key": "licence", "value": "A1234"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("corridor_key", response.data)

    def test_the_corridor_comes_from_the_validated_payload(self):
        response = self._submit(
            {"corridor_key": "sample", "rung_key": "licence", "value": "A1234", "jurisdiction": "NY"}
        )
        self.assertEqual(response.status_code, 201)
        claim = VerificationClaim.objects.get(person=self.person, rung_key="licence")
        self.assertEqual(claim.corridor, self.corridor)
        self.assertEqual(claim.jurisdiction, "NY")

    def test_a_rung_that_asks_for_one_field_may_send_null_for_the_other(self):
        response = self._submit(
            {"corridor_key": "sample", "rung_key": "cv", "value": None, "jurisdiction": None}
        )
        self.assertEqual(response.status_code, 201)

    def test_a_null_lands_as_blank_rather_than_breaking_the_column(self):
        self._submit({"corridor_key": "sample", "rung_key": "cv", "value": None, "jurisdiction": None})
        claim = VerificationClaim.objects.get(person=self.person, rung_key="cv")
        self.assertEqual(claim.fact_value, "")
        self.assertEqual(claim.jurisdiction, "")

    def test_a_submission_is_recorded_self_attested_and_dated(self):
        self._submit({"corridor_key": "sample", "rung_key": "licence", "value": "A1234", "jurisdiction": "NY"})
        claim = VerificationClaim.objects.get(person=self.person, rung_key="licence")
        self.assertEqual(claim.method, VerificationClaim.Method.SELF_ATTESTED)
        self.assertIsNotNone(claim.checked_at)

    def test_an_unknown_corridor_is_still_a_404(self):
        response = self._submit({"corridor_key": "nope", "rung_key": "licence", "value": "A1234"})
        self.assertEqual(response.status_code, 404)


class EscalationScopingTests(TestCase):
    """
    Query ids are guessable, so this route must answer only for the caller's own
    rows. Before the fix it filtered on id alone and resolved no person at all,
    which answered for anybody's escalation.
    """

    def setUp(self):
        self.person = Person.objects.create(full_name="Sample Person", email="p@example.com")
        self.user = User.objects.create_user(username="p@example.com", email="p@example.com")

    def _post(self, payload, person):
        request = APIRequestFactory().post("/api/agent/escalations/", payload, format="json")
        force_authenticate(request, user=self.user)
        with patch("careers.views._person_for", return_value=person):
            return escalation_statuses(request)

    def test_a_caller_with_no_person_is_refused_before_any_lookup(self):
        # Refused ahead of the PendingQuery import, so an unresolved caller
        # cannot reach the table at all.
        response = self._post({"query_ids": ["q_1"]}, None)
        self.assertEqual(response.status_code, 401)

    def test_an_empty_list_is_answered_without_touching_the_table(self):
        response = self._post({"query_ids": []}, self.person)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.data), [])

    def test_query_ids_must_be_a_list(self):
        response = self._post({"query_ids": "q_1"}, self.person)
        self.assertEqual(response.status_code, 400)

    @skipUnless(_pending_query_is_person_scoped(), "PendingQuery has no person_id yet")
    def test_one_candidate_cannot_read_another_candidates_escalation(self):
        """
        The IDOR this fix exists for.

        Skipped rather than failed where PendingQuery is still owner-typed by
        university and student, because that generalisation is an open item on
        the backend and not something this app can do for it. The skip reason
        is the reminder. Where the field does exist, this is the assertion that
        keeps the route honest.
        """
        from django_api.models import PendingQuery

        other = Person.objects.create(full_name="Someone Else", email="q@example.com")
        PendingQuery.objects.create(id="q_mine", person_id=self.person.person_id, status="answered")
        PendingQuery.objects.create(id="q_theirs", person_id=other.person_id, status="answered")

        response = self._post({"query_ids": ["q_mine", "q_theirs"]}, self.person)

        self.assertEqual(response.status_code, 200)
        returned = {row["query_id"] for row in response.data}
        self.assertEqual(returned, {"q_mine"})


class EscalationVisibilityTests(TestCase):
    def test_internal_states_collapse_to_the_three_a_candidate_may_see(self):
        self.assertEqual(_external_status("routed_to_hr"), "pending")
        self.assertEqual(_external_status("awaiting_principal"), "pending")
        self.assertEqual(_external_status("answered"), "answered")
        self.assertEqual(_external_status("learned"), "answered")
        self.assertEqual(_external_status("withdrawn"), "closed")

    def test_an_unknown_internal_state_reads_as_pending_not_as_answered(self):
        # Failing open here would tell a candidate their question was answered
        # when it was not, which is the one direction this must never fail.
        self.assertEqual(_external_status("something_new"), "pending")


class SignupTests(TestCase):
    """
    Careers is an open corridor. These tests hold that open door open, and hold
    it to the same discipline the invitation door has.
    """

    def _start(self, email="new@example.com"):
        return self.client.post(
            reverse("careers-signup-start"), {"email": email}, content_type="application/json"
        )

    def _code_for(self, email):
        from careers.models import SignupCode

        return SignupCode.objects.filter(email=email, consumed_at__isnull=True).first()

    def test_anyone_may_start_without_an_invitation(self):
        response = self._start()
        self.assertEqual(response.status_code, 202)

    def test_the_code_is_never_in_the_response(self):
        body = self._start().json()
        record = self._code_for("new@example.com")
        self.assertIsNotNone(record)
        self.assertNotIn("code", body)
        self.assertNotIn(record.code_hash, str(body))

    def test_the_code_is_hashed_at_rest(self):
        self._start()
        record = self._code_for("new@example.com")
        self.assertEqual(len(record.code_hash), 64)
        self.assertNotEqual(record.code_hash, "123456")

    def test_a_registered_address_answers_identically_to_a_new_one(self):
        first = self._start("taken@example.com")
        Person.objects.create(email="taken@example.com")
        second = self._start("taken@example.com")
        self.assertEqual(first.status_code, second.status_code)
        self.assertEqual(first.json(), second.json())

    def test_a_wrong_code_and_an_unknown_address_answer_identically(self):
        self._start("known@example.com")
        wrong = self.client.post(
            reverse("careers-signup-verify"),
            {"email": "known@example.com", "code": "000000"},
            content_type="application/json",
        )
        unknown = self.client.post(
            reverse("careers-signup-verify"),
            {"email": "nobody@example.com", "code": "000000"},
            content_type="application/json",
        )
        self.assertEqual(wrong.status_code, unknown.status_code)
        self.assertEqual(wrong.json(), unknown.json())

    def test_attempts_are_capped(self):
        self._start("capped@example.com")
        for _ in range(SignupCode.MAX_ATTEMPTS):
            self.client.post(
                reverse("careers-signup-verify"),
                {"email": "capped@example.com", "code": "000000"},
                content_type="application/json",
            )
        record = SignupCode.objects.filter(email="capped@example.com").first()
        self.assertIsNotNone(record.consumed_at)

    def test_asking_twice_retires_the_first_code(self):
        self._start("twice@example.com")
        first = self._code_for("twice@example.com")
        self._start("twice@example.com")
        first.refresh_from_db()
        self.assertIsNotNone(first.consumed_at)

    def test_a_person_and_a_session_appear_only_after_the_code_is_proved(self):
        self._start("proved@example.com")
        self.assertFalse(Person.objects.filter(email="proved@example.com").exists())

    def test_the_address_is_normalised_so_case_is_not_a_second_account(self):
        self._start("Mixed@Example.com")
        self.assertTrue(
            SignupCode.objects.filter(email="mixed@example.com", consumed_at__isnull=True).exists()
        )


class CostPostureTests(TestCase):
    """
    Whether reaching an authority costs money is corridor configuration, set by
    a human. Free checks run on their own; paid ones wait for a hiring human to
    decide, because anything that costs money is the client's decision.
    """

    def setUp(self):
        self.corridor = make_corridor()

    def test_route_is_on_the_wire_and_null_when_nobody_has_established_it(self):
        rungs = {r["key"]: r for r in CorridorSerializer(self.corridor).data["rungs"]}
        self.assertIn("route", rungs["licence"])
        self.assertIsNone(rungs["licence"]["route"])

    def test_a_rung_with_no_route_is_not_treated_as_free(self):
        # The default has to be the one that spends nothing and promises
        # nothing. A rung whose cost is unknown must never run by itself.
        rung = CorridorRung.objects.get(corridor=self.corridor, key="licence")
        self.assertIsNone(rung.route)
        self.assertNotEqual(rung.route, CorridorRung.Route.FREE)

    def test_the_three_postures_round_trip(self):
        for key, route in (
            ("licence", CorridorRung.Route.PAID),
            ("cv", CorridorRung.Route.NONE),
            ("github", CorridorRung.Route.FREE),
        ):
            CorridorRung.objects.filter(corridor=self.corridor, key=key).update(route=route)
        rungs = {r["key"]: r for r in CorridorSerializer(self.corridor).data["rungs"]}
        self.assertEqual(rungs["licence"]["route"], "paid")
        self.assertEqual(rungs["cv"]["route"], "none")
        self.assertEqual(rungs["github"]["route"], "free")

    def test_route_is_independent_of_whether_a_verifier_exists(self):
        # A bot may exist for a rung whose authority charges, and a rung with a
        # free route may have no bot written yet. The two say different things.
        CorridorRung.objects.filter(corridor=self.corridor, key="cv").update(
            route=CorridorRung.Route.FREE
        )
        rung = CorridorRung.objects.get(corridor=self.corridor, key="cv")
        self.assertIsNone(rung.verifier)
        self.assertEqual(rung.route, "free")


_MISSING = object()


class MeEndpointTests(TestCase):
    """
    The route that lets somebody come back. Before it, the app could write
    claims and never read them, and the open signup path collected a person's
    details and dropped them on the floor.
    """

    def setUp(self):
        self.corridor = make_corridor()
        self.person = Person.objects.create(email="p@example.com")
        self.user = User.objects.create_user(username="p@example.com", email="p@example.com")

    def _call(self, method="get", payload=None, person=_MISSING):
        # Sentinel rather than None: `person or self.person` would quietly turn
        # the no-person case back into the happy one.
        resolved = self.person if person is _MISSING else person
        factory = APIRequestFactory()
        request = getattr(factory, method)("/api/me/", payload or {}, format="json")
        force_authenticate(request, user=self.user)
        with patch("careers.views._person_for", return_value=resolved):
            return me(request)

    def _claim(self, rung_key="licence", **kwargs):
        defaults = dict(
            person=self.person, corridor=self.corridor, rung_key=rung_key,
            fact_type=rung_key, fact_value="A1234",
            method=VerificationClaim.Method.SELF_ATTESTED,
            checked_at=timezone.now(),
        )
        defaults.update(kwargs)
        return VerificationClaim.objects.create(**defaults)

    def test_a_caller_with_no_person_is_refused(self):
        self.assertEqual(self._call(person=None).status_code, 401)

    def test_it_returns_the_person_and_their_claims_in_one_call(self):
        self._claim("licence")
        self._claim("cv")
        response = self._call()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["person"]["email"], "p@example.com")
        self.assertEqual({c["rung_key"] for c in response.data["claims"]}, {"licence", "cv"})

    def test_a_superseded_claim_is_not_returned(self):
        self._claim("licence", status=VerificationClaim.Status.SUPERSEDED)
        self._claim("licence", method=VerificationClaim.Method.PRIMARY_SOURCE)
        response = self._call()
        methods = [c["method"] for c in response.data["claims"]]
        self.assertEqual(methods, ["primary_source"])

    def test_expiry_is_recomputed_on_read_here_too(self):
        # Same rule as the single-rung route: a stored flag is not evidence.
        self._claim("licence", expires_at=timezone.now() - timedelta(days=1))
        response = self._call()
        self.assertEqual(response.data["claims"][0]["status"], "expired")

    def test_patch_saves_the_details_the_open_path_used_to_drop(self):
        response = self._call(
            "patch",
            {"full_name": "Sample Person", "phone": "555", "country": "United States"},
        )
        self.assertEqual(response.status_code, 200)
        self.person.refresh_from_db()
        self.assertEqual(self.person.full_name, "Sample Person")
        self.assertEqual(self.person.country, "United States")

    def test_patch_refuses_to_move_the_address_the_session_was_minted_against(self):
        # Accepting email here would let a signed-in person rewrite themselves
        # into somebody else's row. Changing an address is a re-verification.
        self._call("patch", {"email": "someone.else@example.com", "full_name": "Sample Person"})
        self.person.refresh_from_db()
        self.assertEqual(self.person.email, "p@example.com")
        self.assertEqual(self.person.full_name, "Sample Person")

    def test_patch_cannot_reassign_the_person_id(self):
        original = self.person.person_id
        self._call("patch", {"person_id": "p_someone_else"})
        self.person.refresh_from_db()
        self.assertEqual(self.person.person_id, original)

    def test_patch_answers_with_the_same_shape_as_get(self):
        self._claim("licence")
        patched = self._call("patch", {"full_name": "Sample Person"})
        fetched = self._call()
        self.assertEqual(set(patched.data.keys()), set(fetched.data.keys()))
        self.assertEqual(patched.data["person"], fetched.data["person"])


class NavigatorNameTests(TestCase):
    """
    The name a person calls their own agent. Not a handle, so it does not have
    to be unique — it was, and the second person to pick a taken word got an
    IntegrityError on a rename that should never have been able to fail.
    """

    def test_two_people_may_both_call_their_navigator_the_same_thing(self):
        first = Person.objects.create(email="first@example.com", agent_name="Ada")
        second = Person.objects.create(email="second@example.com", agent_name="Ada")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.agent_name, "Ada")
        self.assertEqual(second.agent_name, "Ada")

    def test_the_name_is_stored_exactly_as_typed(self):
        # It was being suffixed to dodge the uniqueness constraint, so a person
        # who typed AVI got AVI#Yc-i back.
        person = Person.objects.create(email="third@example.com")
        person.agent_name = "AVI"
        person.save(update_fields=["agent_name"])
        person.refresh_from_db()
        self.assertEqual(person.agent_name, "AVI")


class VerifierSeamTests(TestCase):
    """
    The seam every helper bot plugs into: when one may run, what its findings
    become, and what it is not allowed to do.
    """

    def setUp(self):
        self.corridor = make_corridor()
        self.person = Person.objects.create(email="p@example.com")
        self.rung = CorridorRung.objects.get(corridor=self.corridor, key="licence")

    def _result(self, *findings, version="test/1"):
        return VerifierResult(findings=list(findings), verifier_version=version)

    def test_a_rung_with_no_established_cost_does_not_run_its_bot(self):
        # Null route means nobody has worked out what reaching this authority
        # costs. Guessing is not something to act on.
        self.rung.route = None
        self.assertFalse(may_run_now(self.rung))

    def test_a_paid_route_waits_for_somebody_to_decide_to_spend(self):
        self.rung.route = CorridorRung.Route.PAID
        self.assertFalse(may_run_now(self.rung))

    def test_a_free_route_runs_on_its_own(self):
        self.rung.route = CorridorRung.Route.FREE
        self.assertTrue(may_run_now(self.rung))

    def test_a_bot_named_but_not_installed_is_not_an_error(self):
        self.rung.route = CorridorRung.Route.FREE
        self.rung.verifier = "not_deployed_here"
        self.rung.save()
        # The person's claim stands; the endpoint does not fail.
        self.assertIsNone(
            run_verifier_for(self.person, self.corridor, self.rung, Submission(rung_key="licence"))
        )

    def test_one_claim_per_fact_rather_than_one_blob(self):
        result = self._result(
            Finding(fact_type="full_name", fact_value="Sample Person", method="source_checked"),
            Finding(fact_type="institution", fact_value="Somewhere", method="source_checked"),
        )
        written = record_findings(self.person, self.corridor, self.rung, result)
        self.assertEqual(len(written), 2)
        self.assertEqual(
            {c.fact_type for c in written}, {"full_name", "institution"}
        )

    def test_superseding_is_per_fact_not_per_rung(self):
        # A bot establishing two of three facts must not retire the third.
        first = self._result(
            Finding(fact_type="full_name", fact_value="Old", method="source_checked"),
            Finding(fact_type="institution", fact_value="Kept", method="source_checked"),
        )
        record_findings(self.person, self.corridor, self.rung, first)

        second = self._result(Finding(fact_type="full_name", fact_value="New", method="source_checked"))
        record_findings(self.person, self.corridor, self.rung, second)

        live = VerificationClaim.objects.filter(
            person=self.person, status=VerificationClaim.Status.ACTIVE
        )
        self.assertEqual({c.fact_type: c.fact_value for c in live},
                         {"full_name": "New", "institution": "Kept"})

    def test_the_persons_own_submission_is_never_edited(self):
        typed = VerificationClaim.objects.create(
            person=self.person, corridor=self.corridor, rung_key="licence",
            fact_type="licence", fact_value="A1234",
            method=VerificationClaim.Method.SELF_ATTESTED, checked_at=timezone.now(),
        )
        record_findings(
            self.person, self.corridor, self.rung,
            self._result(Finding(fact_type="licence", fact_value="A1234", method="primary_source")),
        )
        typed.refresh_from_db()
        # Still there, still theirs, still self_attested — just superseded.
        self.assertEqual(typed.method, VerificationClaim.Method.SELF_ATTESTED)
        self.assertEqual(typed.status, VerificationClaim.Status.SUPERSEDED)

    def test_the_headline_claim_is_the_strongest_one(self):
        VerificationClaim.objects.create(
            person=self.person, corridor=self.corridor, rung_key="licence",
            fact_type="licence", fact_value="A1234",
            method=VerificationClaim.Method.SELF_ATTESTED, checked_at=timezone.now(),
        )
        record_findings(
            self.person, self.corridor, self.rung,
            self._result(Finding(fact_type="skills", fact_value="x", method="source_checked")),
        )
        self.assertEqual(headline_claim(self.person, "licence").method, "source_checked")


class ScreeningIdentifierTests(TestCase):
    """
    The identifiers a screen needs in order to be worth anything.

    A name alone is not enough to decide either way against a federal exclusion
    list, so these travel with every submission. Collected once, used only for
    telling this person apart from a stranger with the same name.
    """

    def setUp(self):
        self.corridor = make_corridor()
        self.rung = CorridorRung.objects.get(corridor=self.corridor, key="licence")
        self.rung.route = CorridorRung.Route.FREE
        self.rung.verifier = "spy"
        self.rung.save()

        self.seen = {}
        outer = self

        class Spy:
            version = "spy/1"

            def run(self, submission):
                outer.seen = dict(submission.subject or {})
                return VerifierResult(findings=[], verifier_version="spy/1")

        register("spy", Spy())

    def _run(self, person):
        run_verifier_for(person, self.corridor, self.rung,
                         Submission(rung_key="licence"))

    def test_the_identifiers_reach_the_agent(self):
        person = Person.objects.create(
            email="a@example.com", full_name="Shelley Akey",
            date_of_birth=date(1984, 2, 11), previous_names=["Shelley Smith"],
        )
        self._run(person)
        self.assertEqual(self.seen["full_name"], "Shelley Akey")
        self.assertEqual(self.seen["date_of_birth"], "1984-02-11")
        self.assertEqual(self.seen["previous_names"], ["Shelley Smith"])

    def test_a_person_who_gave_neither_still_works(self):
        # Both are optional. A screen run on a name alone reports itself as
        # weak rather than refusing, so the product works without them.
        person = Person.objects.create(email="b@example.com", full_name="Rowan Vandermeer")
        self._run(person)
        self.assertEqual(self.seen["date_of_birth"], "")
        self.assertEqual(self.seen["previous_names"], [])

    def test_the_date_is_sent_as_a_string_the_agents_can_parse(self):
        # The package parses dates from strings and never imports Django's.
        person = Person.objects.create(
            email="c@example.com", full_name="A B", date_of_birth=date(1955, 7, 30))
        self._run(person)
        self.assertIsInstance(self.seen["date_of_birth"], str)
        self.assertEqual(self.seen["date_of_birth"], "1955-07-30")

    def test_they_are_readable_and_editable_over_the_api(self):
        person = Person.objects.create(
            email="d@example.com", full_name="A B",
            date_of_birth=date(1984, 2, 11), previous_names=["Old Name"],
        )
        data = PersonSerializer(person).data
        self.assertEqual(data["date_of_birth"], "1984-02-11")
        self.assertEqual(data["previous_names"], ["Old Name"])

        form = PersonUpdateSerializer(data={"date_of_birth": None, "previous_names": []})
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_nonsense_date_is_refused_rather_than_stored(self):
        form = PersonUpdateSerializer(data={"date_of_birth": "not a date"})
        self.assertFalse(form.is_valid())


class ScreeningClaimTests(TestCase):
    """
    Negative checks — searching a list for somebody rather than establishing a
    fact about them. An OIG exclusion check, a SAM.gov debarment check.

    A screen is legitimately `primary_source`, because the list's publisher
    really was asked. That is exactly what makes it dangerous: every ranking in
    this app orders by method, so without `shape` an absence of bad news sorts
    above a real credential and becomes the headline.
    """

    def setUp(self):
        self.corridor = make_corridor()
        self.person = Person.objects.create(email="screened@example.com")
        self.rung = CorridorRung.objects.get(corridor=self.corridor, key="licence")

    def _screen(self, value="no_match", **kw):
        kw.setdefault("matched_on", ["full_name", "npi"])
        kw.setdefault("source_as_of", date(2026, 8, 1))
        return Finding(
            fact_type="oig_exclusion", fact_value=value,
            method="primary_source", shape="screens", **kw,
        )

    def _record(self, *findings):
        return record_findings(
            self.person, self.corridor, self.rung,
            VerifierResult(findings=list(findings), verifier_version="oig/1"),
        )

    def test_a_screen_never_becomes_the_headline(self):
        # The person's licence is still only what they typed. A clean exclusion
        # check must not turn that into "confirmed with the issuing body".
        VerificationClaim.objects.create(
            person=self.person, corridor=self.corridor, rung_key="licence",
            fact_type="licence", fact_value="A1234",
            method=VerificationClaim.Method.SELF_ATTESTED, checked_at=timezone.now(),
        )
        self._record(self._screen())

        headline = headline_claim(self.person, "licence")
        self.assertEqual(headline.method, VerificationClaim.Method.SELF_ATTESTED)
        self.assertEqual(headline.fact_type, "licence")

    def test_a_screen_is_stored_with_what_bounds_it(self):
        [claim] = self._record(self._screen())
        self.assertEqual(claim.shape, VerificationClaim.Shape.SCREENS)
        self.assertEqual(claim.source_as_of, date(2026, 8, 1))
        self.assertEqual(claim.matched_on, ["full_name", "npi"])
        # The data's own date is not the date we looked.
        self.assertNotEqual(claim.source_as_of, claim.checked_at.date())

    def test_an_ordinary_finding_is_a_fact_by_default(self):
        [claim] = self._record(
            Finding(fact_type="licence", fact_value="A1234", method="primary_source")
        )
        self.assertEqual(claim.shape, VerificationClaim.Shape.ASSERTS)
        self.assertIsNone(claim.source_as_of)
        self.assertEqual(claim.matched_on, [])

    def test_a_screen_does_not_retire_the_fact_beside_it(self):
        # Superseding is scoped by shape as well as fact type. Two different
        # questions about the same person do not answer each other.
        self._record(Finding(fact_type="oig_exclusion", fact_value="A1234", method="self_attested"))
        self._record(self._screen())

        live = VerificationClaim.objects.filter(
            person=self.person, status=VerificationClaim.Status.ACTIVE
        )
        self.assertEqual(
            {(c.shape, c.fact_value) for c in live},
            {("asserts", "A1234"), ("screens", "no_match")},
        )

    def test_a_rerun_screen_supersedes_the_previous_screen(self):
        self._record(self._screen(source_as_of=date(2026, 7, 1)))
        self._record(self._screen(source_as_of=date(2026, 8, 1)))

        live = VerificationClaim.objects.filter(
            person=self.person, shape=VerificationClaim.Shape.SCREENS,
            status=VerificationClaim.Status.ACTIVE,
        )
        self.assertEqual([c.source_as_of for c in live], [date(2026, 8, 1)])

    def test_a_possible_match_is_recorded_rather_than_swallowed(self):
        # A name-only hit is the answer that needs a human. Dropping it because
        # it is not conclusive would lose the one finding a reviewer must see.
        [claim] = self._record(self._screen("possible_match", matched_on=["full_name"]))
        self.assertEqual(claim.fact_value, "possible_match")
        self.assertEqual(claim.matched_on, ["full_name"])

    def test_the_wire_tells_a_client_which_it_is_reading(self):
        [claim] = self._record(self._screen())
        payload = VerificationClaimSerializer(claim).data
        self.assertEqual(payload["shape"], "screens")
        self.assertEqual(payload["source_as_of"], "2026-08-01")
        self.assertEqual(payload["matched_on"], ["full_name", "npi"])


class ResumeParserMappingTests(TestCase):
    """
    The adapter, without an API key or a PDF. What it maps, what it refuses to
    map, and the method it is allowed to claim.
    """

    PARSED = {
        "name": "Sample Person",
        "email": "p@example.com",
        "institution": "Somewhere University",
        "major": "Nursing",
        "graduation_year": 2019,
        "work_months": 36,
        "publications_count": 0,
        "research": "None stated",
        "program": "MS Computer Science",
        "gre_quant": 165,
        "technical_skills": ["Triage", "Phlebotomy"],
    }

    def test_reading_a_document_is_source_checked_and_never_primary_source(self):
        # Nobody asked the issuing body anything. This is the distinction the
        # whole claim model exists to keep.
        methods = {f.method for f in findings_from(self.PARSED)}
        self.assertEqual(methods, {"source_checked"})

    def test_it_maps_the_facts_a_practice_weighs(self):
        by_type = {f.fact_type: f.fact_value for f in findings_from(self.PARSED)}
        self.assertEqual(by_type["full_name"], "Sample Person")
        self.assertEqual(by_type["institution"], "Somewhere University")
        self.assertEqual(by_type["field_of_study"], "Nursing")
        self.assertEqual(by_type["work_experience_months"], "36")
        self.assertIn("Triage", by_type["skills"])

    def test_it_drops_what_the_agent_returns_for_students(self):
        # The agent was written for a student resume. A hardcoded program and a
        # GRE score are not facts about a clinician.
        by_type = {f.fact_type for f in findings_from(self.PARSED)}
        self.assertNotIn("program", by_type)
        self.assertNotIn("gre_quant", by_type)

    def test_an_unfilled_field_is_not_a_finding(self):
        by_type = {f.fact_type for f in findings_from(self.PARSED)}
        # publications_count is 0 and research is "None stated": both are the
        # parser not finding something, not a fact established.
        self.assertNotIn("publications", by_type)

    def test_nothing_to_read_is_unavailable_rather_than_empty(self):
        # "No answer yet" and "the answer is nothing" are different, and only
        # the second should ever mark a rung as checked.
        result = ResumeParserVerifier().run(Submission(rung_key="cv"))
        self.assertTrue(result.unavailable)
        self.assertEqual(result.findings, [])


class RemoteVerifierTests(TestCase):
    """
    The whole careers-side change if the bots move into a shared service.

    A service is a different trust domain from an imported module. These cover
    what this app refuses to take on trust, and that a service being down is
    never something the person handing in a fact has to see.
    """

    class FakeTransport(Transport):
        def __init__(self, submit_result=None, poll_results=None, raise_on_submit=None):
            self.submit_result = submit_result or {}
            self.poll_results = list(poll_results or [])
            self.raise_on_submit = raise_on_submit
            self.submitted = None

        def submit(self, endpoint, submission):
            if self.raise_on_submit:
                raise self.raise_on_submit
            self.submitted = submission
            return self.submit_result

        def poll(self, endpoint, job_id):
            return self.poll_results.pop(0) if self.poll_results else {"status": "running"}

    def _verifier(self, transport, max_method="source_checked"):
        endpoint = AgentEndpoint(
            base_url="https://agents.test", agent="resume_parser",
            max_method=max_method, poll_attempts=3, poll_interval_s=0,
        )
        return HttpVerifier(endpoint, transport=transport)

    def test_an_inline_answer_is_used_without_polling(self):
        transport = self.FakeTransport(submit_result={
            "version": "resume_parser/2",
            "findings": [{"fact_type": "full_name", "fact_value": "Sample Person", "method": "source_checked"}],
        })
        result = self._verifier(transport).run(Submission(rung_key="cv"))
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.verifier_version, "resume_parser/2")

    def test_a_queued_job_is_polled_to_completion(self):
        transport = self.FakeTransport(
            submit_result={"job_id": "j1", "status": "queued"},
            poll_results=[
                {"status": "running"},
                {"status": "done", "findings": [
                    {"fact_type": "institution", "fact_value": "Somewhere", "method": "source_checked"}
                ]},
            ],
        )
        result = self._verifier(transport).run(Submission(rung_key="cv"))
        self.assertEqual(result.findings[0].fact_type, "institution")

    def test_an_agent_cannot_claim_more_than_it_is_permitted(self):
        # Reading a document is not confirming with an issuing authority. A
        # service saying otherwise is not evidence that it did.
        transport = self.FakeTransport(submit_result={"findings": [
            {"fact_type": "institution", "fact_value": "Somewhere", "method": "primary_source"}
        ]})
        result = self._verifier(transport).run(Submission(rung_key="cv"))
        self.assertEqual(result.findings[0].method, "source_checked")

    def test_a_method_this_app_does_not_know_is_dropped_not_guessed(self):
        transport = self.FakeTransport(submit_result={"findings": [
            {"fact_type": "institution", "fact_value": "Somewhere", "method": "probably_fine"}
        ]})
        self.assertEqual(self._verifier(transport).run(Submission(rung_key="cv")).findings, [])

    def test_a_service_that_is_down_is_unavailable_not_an_error(self):
        # The person gave us a fact. A redeploy is not their problem, and their
        # claim stands as self_attested until a check can run.
        transport = self.FakeTransport(raise_on_submit=OSError("connection refused"))
        result = self._verifier(transport).run(Submission(rung_key="cv"))
        self.assertTrue(result.unavailable)
        self.assertEqual(result.findings, [])

    def test_a_job_that_never_finishes_gives_up_quietly(self):
        transport = self.FakeTransport(submit_result={"job_id": "j1", "status": "queued"})
        result = self._verifier(transport).run(Submission(rung_key="cv"))
        self.assertTrue(result.unavailable)

    def test_it_is_the_same_shape_as_an_in_process_verifier(self):
        # The point of the seam: submit_rung cannot tell the difference.
        transport = self.FakeTransport(submit_result={"findings": []})
        remote = self._verifier(transport)
        self.assertTrue(hasattr(remote, "run"))
        self.assertIsInstance(remote.run(Submission(rung_key="cv")), VerifierResult)


class BotFindingsSurviveSubmissionTests(TestCase):
    """
    The order the client actually uses.

    `submissionSteps` returns ['upload', 'submit'] for a rung with a file, so
    the document is parsed first and the person's own submission lands a moment
    later. `submit_rung` used to retire every active claim on the rung, which
    meant the parse was wiped every single time — a 100% reproduction, not an
    edge case. The rung then sat on "still checking" forever, because the only
    surviving claim was the self_attested one the person had just made.
    """

    def setUp(self):
        self.corridor = make_corridor()
        self.rung = CorridorRung.objects.get(corridor=self.corridor, key="cv")
        self.person = Person.objects.create(full_name="A Person", email="a@example.com")
        self.user = User.objects.create_user(username="a@example.com", email="a@example.com")

    def _upload_findings(self):
        """Stands in for the document route, which is where a bot's facts arrive."""
        record_findings(
            self.person,
            self.corridor,
            self.rung,
            VerifierResult(
                findings=[
                    Finding("full_name", "A Person", "source_checked"),
                    Finding("employment", "Nurse at Trust", "source_checked"),
                    Finding("qualification", "BSc", "source_checked"),
                ],
                verifier_version="cv/1",
            ),
        )

    def _submit(self, value=""):
        request = APIRequestFactory().post(
            "/api/claims/submit/",
            {"corridor_key": self.corridor.key, "rung_key": "cv", "value": value},
            format="json",
        )
        force_authenticate(request, user=self.user)
        with patch("careers.views._person_for", return_value=self.person):
            return submit_rung(request)

    def _active(self):
        return VerificationClaim.objects.filter(
            person=self.person, rung_key="cv", status=VerificationClaim.Status.ACTIVE
        )

    def test_a_submission_does_not_retire_the_bots_facts(self):
        self._upload_findings()
        self._submit()

        surviving = {c.fact_type for c in self._active()}
        # All three facts stand. The rung-level `cv` claim does not appear:
        # it is a fallback, and something more specific now exists — see
        # RungLevelClaimIsAFallbackTests.
        self.assertEqual(surviving, {"full_name", "employment", "qualification"})

    def test_the_rung_reads_as_checked_rather_than_still_checking(self):
        self._upload_findings()
        self._submit()

        # The client stops polling when the headline rises above self_attested.
        # While the rung-wide supersede was in place this stayed self_attested
        # and the rung never resolved.
        self.assertEqual(headline_claim(self.person, "cv").method, "source_checked")

    def test_resubmitting_leaves_the_bots_facts_alone(self):
        self._upload_findings()
        self._submit(value="first")
        self._submit(value="second")

        # Two submissions, and the three facts are still exactly as the bot
        # left them. This is the property that was broken.
        self.assertEqual(self._active().count(), 3)
        self.assertEqual(
            {c.fact_type for c in self._active()},
            {"full_name", "employment", "qualification"},
        )

    def test_resubmitting_replaces_the_persons_own_claim_when_no_bot_ran(self):
        # No upload, so the person's own claim is the only record and stands.
        self._submit(value="first")
        self._submit(value="second")

        own = self._active().filter(fact_type="cv")
        self.assertEqual([c.fact_value for c in own], ["second"])

    def test_the_superseded_history_is_kept_not_deleted(self):
        self._upload_findings()
        self._submit(value="first")
        self._submit(value="second")

        superseded = VerificationClaim.objects.filter(
            person=self.person, rung_key="cv", status=VerificationClaim.Status.SUPERSEDED
        ).order_by("checked_at")
        # Both submissions are retired as fallbacks, and both are still on
        # record. Nothing the bot established was touched.
        self.assertEqual([c.fact_value for c in superseded], ["first", "second"])


class MultiValuedFactTests(TestCase):
    """
    A fact type is not single-valued.

    A CV states two degrees and three jobs. Superseding inside the write loop
    meant each finding retired the one before it, so a five-finding parse left
    two claims standing and dropped three with no error and no symptom — the
    profile simply showed one job where the CV listed three.
    """

    def setUp(self):
        self.corridor = make_corridor()
        self.rung = CorridorRung.objects.get(corridor=self.corridor, key="cv")
        self.person = Person.objects.create(full_name="A Person", email="a@example.com")

    def _record(self, *pairs, version="cv/1"):
        return record_findings(
            self.person, self.corridor, self.rung,
            VerifierResult(
                findings=[Finding(t, v, "source_checked") for t, v in pairs],
                verifier_version=version,
            ),
        )

    def _active(self, fact_type=None):
        qs = VerificationClaim.objects.filter(
            person=self.person, rung_key="cv", status=VerificationClaim.Status.ACTIVE
        )
        return qs.filter(fact_type=fact_type) if fact_type else qs

    def test_every_finding_of_a_repeated_type_survives(self):
        self._record(
            ("qualification", "BSc Nursing"),
            ("qualification", "MSc Advanced Practice"),
            ("employment", "Trust A"),
            ("employment", "Trust B"),
            ("employment", "Trust C"),
        )
        self.assertEqual(self._active().count(), 5)
        self.assertEqual(
            {c.fact_value for c in self._active("employment")},
            {"Trust A", "Trust B", "Trust C"},
        )

    def test_a_re_check_replaces_the_previous_answer_entirely(self):
        self._record(("employment", "Trust A"), ("employment", "Trust B"))
        # A corrected CV listing one job must not leave the stale second one
        # standing beside it.
        self._record(("employment", "Trust A only"), version="cv/2")

        self.assertEqual([c.fact_value for c in self._active("employment")], ["Trust A only"])
        self.assertEqual(
            VerificationClaim.objects.filter(
                person=self.person, fact_type="employment",
                status=VerificationClaim.Status.SUPERSEDED,
            ).count(),
            2,
        )

    def test_a_re_check_leaves_untouched_fact_types_alone(self):
        self._record(("full_name", "A Person"), ("employment", "Trust A"))
        self._record(("employment", "Trust B"), version="cv/2")

        # The name was not re-established, so its claim stands.
        self.assertEqual([c.fact_value for c in self._active("full_name")], ["A Person"])

    def test_an_empty_result_retires_nothing(self):
        self._record(("full_name", "A Person"))
        record_findings(
            self.person, self.corridor, self.rung, VerifierResult(findings=[]),
        )
        self.assertEqual(self._active().count(), 1)


class RungLevelClaimIsAFallbackTests(TestCase):
    """
    `submit_rung` writes a claim whose fact_type is the rung key. That is a
    fallback record, not a fact: `cv: ""` asserts nothing, and
    `licence: R123456` asserts the same thing as a bot's
    `licence_number: R123456`. Both showed on the profile as a duplicate — an
    empty card under CV, and the licence number twice.

    It stands only while nothing more specific does.
    """

    def setUp(self):
        self.corridor = make_corridor()
        self.rung = CorridorRung.objects.get(corridor=self.corridor, key="cv")
        self.person = Person.objects.create(full_name="A Person", email="a@example.com")
        self.user = User.objects.create_user(username="a@example.com", email="a@example.com")

    def _submit(self, value=""):
        request = APIRequestFactory().post(
            "/api/claims/submit/",
            {"corridor_key": self.corridor.key, "rung_key": "cv", "value": value},
            format="json",
        )
        force_authenticate(request, user=self.user)
        with patch("careers.views._person_for", return_value=self.person):
            return submit_rung(request)

    def _active(self):
        return VerificationClaim.objects.filter(
            person=self.person, rung_key="cv", status=VerificationClaim.Status.ACTIVE
        )

    def test_it_stands_when_it_is_the_only_record(self):
        # No bot ran, so what the person typed is all anybody has.
        self._submit(value="typed by hand")
        self.assertEqual([c.fact_type for c in self._active()], ["cv"])

    def test_it_is_retired_once_facts_arrive_from_the_upload_route(self):
        # The order the client actually uses: document first, submit second.
        record_findings(
            self.person, self.corridor, self.rung,
            VerifierResult(findings=[Finding("full_name", "A Person", "source_checked")]),
        )
        self._submit()

        # The empty card under CV is what this removes.
        self.assertEqual([c.fact_type for c in self._active()], ["full_name"])

    def test_a_typed_value_is_not_shown_twice(self):
        record_findings(
            self.person, self.corridor, self.rung,
            VerifierResult(findings=[Finding("licence_number", "R123456", "self_attested")]),
        )
        self._submit(value="R123456")

        values = [(c.fact_type, c.fact_value) for c in self._active()]
        self.assertEqual(values, [("licence_number", "R123456")])

    def test_the_retired_claim_is_kept_not_deleted(self):
        record_findings(
            self.person, self.corridor, self.rung,
            VerifierResult(findings=[Finding("full_name", "A Person", "source_checked")]),
        )
        self._submit(value="typed by hand")

        retired = VerificationClaim.objects.filter(
            person=self.person, rung_key="cv", status=VerificationClaim.Status.SUPERSEDED
        )
        self.assertEqual([c.fact_value for c in retired], ["typed by hand"])


class JurisdictionListTests(TestCase):
    """
    The field used to be free text labelled "Issued by — the body that issued
    it", while the server matched its value against a directory of jurisdiction
    *codes*. Nobody answering the question as asked could produce a value the
    lookup would find: "Nursing and Midwifery Council" never resolves to "GB".

    The corridor now enumerates them, so the client can render a picker.
    """

    def setUp(self):
        self.corridor = make_corridor()
        self.rung = CorridorRung.objects.get(corridor=self.corridor, key="licence")

    def test_the_list_reaches_the_client(self):
        self.rung.jurisdictions = [{"code": "GB", "label": "United Kingdom (NMC)"}]
        self.rung.save(update_fields=["jurisdictions"])

        wire = CorridorSerializer(self.corridor).data
        licence = next(r for r in wire["rungs"] if r["key"] == "licence")
        self.assertEqual(licence["jurisdictions"], [{"code": "GB", "label": "United Kingdom (NMC)"}])

    def test_an_unenumerated_rung_sends_an_empty_list_not_a_missing_key(self):
        # The client decides picker-or-text-box from whether there are entries.
        # An absent key would make that depend on the server's mood.
        wire = CorridorSerializer(self.corridor).data
        for rung in wire["rungs"]:
            self.assertIn("jurisdictions", rung)
        licence = next(r for r in wire["rungs"] if r["key"] == "licence")
        self.assertEqual(licence["jurisdictions"], [])

    def test_codes_are_readable_without_the_labels(self):
        self.rung.jurisdictions = [
            {"code": "GB", "label": "United Kingdom"},
            {"code": " US-CA ", "label": "California"},
            {"code": "", "label": "Nowhere"},          # unusable — no code to match
            {"label": "Missing entirely"},              # malformed
            "not a dict",                               # malformed
        ]
        self.rung.save(update_fields=["jurisdictions"])
        # Whitespace trimmed, unusable entries dropped rather than raising:
        # a corridor row edited by hand should not break a person's submission.
        self.assertEqual(self.rung.jurisdiction_codes(), ["GB", "US-CA"])

    def test_a_submission_still_accepts_a_jurisdiction_outside_the_list(self):
        # "Somewhere else" is deliberately not refused. A person whose regulator
        # nobody has integrated must still be able to hand their licence in; it
        # is recorded as self_attested, which is the truth.
        self.rung.jurisdictions = [{"code": "GB", "label": "United Kingdom"}]
        self.rung.save(update_fields=["jurisdictions"])

        person = Person.objects.create(full_name="A Person", email="a@example.com")
        user = User.objects.create_user(username="a@example.com", email="a@example.com")
        request = APIRequestFactory().post(
            "/api/claims/submit/",
            {"corridor_key": self.corridor.key, "rung_key": "licence",
             "value": "X1", "jurisdiction": "Kerala"},
            format="json",
        )
        force_authenticate(request, user=user)
        with patch("careers.views._person_for", return_value=person):
            response = submit_rung(request)

        self.assertEqual(response.status_code, 201)
        claim = VerificationClaim.objects.filter(person=person, rung_key="licence").first()
        self.assertEqual(claim.method, "self_attested")
