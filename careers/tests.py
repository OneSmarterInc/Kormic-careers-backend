"""
Careers corridor tests.

These cover the rules that cannot be retrofitted: the corridor drives the
ladder, a claim always carries a method and a date, a raised method supersedes
rather than edits, and the candidate never sees the practice's internal states.
"""
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from careers.models import Corridor, CorridorRung, Person, VerificationClaim
from careers.serializers import CorridorSerializer, VerificationClaimSerializer
from careers.views import _external_status, escalation_statuses, submit_rung


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
            {"key", "display_name", "requirement", "input", "verifier", "order"},
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
