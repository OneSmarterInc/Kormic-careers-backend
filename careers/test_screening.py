"""
Background checks: the monthly OIG list, when screens run, and what the person
is shown.

The rules worth a test each. A broken download never replaces a good list. A
screen waits for a date of birth, re-runs only when the identity it searched on
changes, and never fails the request that triggered it. And a possible match
reaches the person as "under review", never as a federal exclusion.
"""
import csv
import json
import os
import time
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from careers.leie import LatestLeie, read_manifest, write_manifest
from careers.models import Corridor, CorridorRung, Person, VerificationClaim
from careers.serializers import VerificationClaimSerializer
from careers.services import rescreen_everyone, run_screens
from careers.verifiers import Finding, VerifierResult, register
from careers.views import me, submit_rung

LEIE_COLUMNS = [
    "LASTNAME", "FIRSTNAME", "MIDNAME", "BUSNAME", "GENERAL", "SPECIALTY", "UPIN",
    "NPI", "DOB", "ADDRESS", "CITY", "STATE", "ZIP", "EXCLTYPE", "EXCLDATE",
    "REINDATE", "WAIVERDATE", "WVRSTATE",
]


def write_leie(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEIE_COLUMNS)
        writer.writeheader()
        for i in range(rows):
            writer.writerow({
                "LASTNAME": f"PERSON{i}", "FIRSTNAME": "TEST", "NPI": "0000000000",
                "DOB": "19700101", "EXCLTYPE": "1128a1", "EXCLDATE": "20200101",
            })
    return path


class FetchLeieTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.folder = Path(self.temp.name) / "leie"
        self.override = override_settings(LEIE_DIR=self.folder)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        self.temp.cleanup()

    def fetch(self, rows=50, as_of="2026-09-10", **options):
        source = write_leie(Path(self.temp.name) / f"source-{rows}.csv", rows)
        out = StringIO()
        with patch("careers.management.commands.fetch_leie.MIN_RECORDS", 20):
            call_command("fetch_leie", file=str(source), as_of=as_of, stdout=out, **options)
        return out.getvalue()

    def test_a_good_file_becomes_the_current_list_with_its_date(self):
        self.fetch()
        manifest = read_manifest(self.folder)
        self.assertEqual(manifest["as_of"], "2026-09-10")
        self.assertEqual(manifest["records"], 50)
        self.assertTrue((self.folder / manifest["file"]).is_file())

    def test_a_truncated_download_never_replaces_a_good_list(self):
        # A screen against an empty list says "no match" for everybody, which is
        # the one failure this command exists to make impossible.
        self.fetch()
        with self.assertRaises(CommandError):
            self.fetch(rows=3, as_of="2026-10-10")
        self.assertEqual(read_manifest(self.folder)["as_of"], "2026-09-10")

    def test_the_same_file_is_not_replaced_twice(self):
        self.fetch()
        self.assertIn("Already current", self.fetch())

    def test_a_local_file_needs_its_publication_date_stated(self):
        source = write_leie(Path(self.temp.name) / "x.csv", 50)
        with self.assertRaises(CommandError):
            call_command("fetch_leie", file=str(source), stdout=StringIO())

    def test_the_date_comes_from_oig_not_from_this_machine(self):
        source = write_leie(Path(self.temp.name) / "dl.csv", 50)
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Last-Modified": "Thu, 10 Sep 2026 12:13:06 GMT"}
        response.iter_content.return_value = [source.read_bytes()]
        with patch("requests.get", return_value=response), \
                patch("careers.management.commands.fetch_leie.MIN_RECORDS", 20):
            call_command("fetch_leie", stdout=StringIO())
        self.assertEqual(read_manifest(self.folder)["as_of"], "2026-09-10")

    def test_older_months_are_pruned_but_recent_ones_kept(self):
        for month, rows in [("2026-06-10", 30), ("2026-07-10", 31),
                            ("2026-08-10", 32), ("2026-09-10", 33)]:
            self.fetch(rows=rows, as_of=month)
        kept = sorted(p.name for p in self.folder.glob("LEIE-*.csv"))
        self.assertEqual(kept, ["LEIE-2026-07-10.csv", "LEIE-2026-08-10.csv",
                                "LEIE-2026-09-10.csv"])


class LatestLeieTests(TestCase):
    def test_a_running_server_picks_up_the_new_month(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            write_leie(folder / "a.csv", 5)
            write_leie(folder / "b.csv", 7)
            write_manifest(folder, {"file": "a.csv", "as_of": "2026-08-10"})
            source = LatestLeie(folder)
            self.assertEqual(source.as_of(), date(2026, 8, 10))
            self.assertEqual(len(list(source.records())), 5)

            time.sleep(0.05)  # a distinct modification time on coarse filesystems
            write_manifest(folder, {"file": "b.csv", "as_of": "2026-09-10"})
            os.utime(folder / "latest.json", (time.time() + 5, time.time() + 5))
            self.assertEqual(source.as_of(), date(2026, 9, 10))
            self.assertEqual(len(list(source.records())), 7)

    def test_no_download_means_not_available(self):
        with TemporaryDirectory() as temp:
            self.assertFalse(LatestLeie(Path(temp)).available())


class FakeScreen:
    """Stands in for the OIG agent and counts how often it was asked."""

    version = "fake_screen/1"

    def __init__(self, outcome="no_match", fail=False):
        self.outcome = outcome
        self.fail = fail
        self.calls = 0
        self.subjects = []

    def run(self, submission):
        self.calls += 1
        self.subjects.append(dict(submission.subject or {}))
        if self.fail:
            raise RuntimeError("list unreadable")
        return VerifierResult(
            findings=[Finding(
                fact_type="oig_exclusion", fact_value=self.outcome,
                method="primary_source", shape="screens",
                source_as_of=date(2026, 9, 10),
                matched_on=["full_name", "date_of_birth"],
            )],
            verifier_version="fake_screen/1",
        )


class ScreeningFixture:
    """A corridor with one automatic check, and a consenting person. No tests of its own."""

    def setUp(self):
        self.corridor = Corridor.objects.create(key="sample", display_name="Sample")
        CorridorRung.objects.create(
            corridor=self.corridor, key="licence", display_name="Licence",
            requirement="required", input="identifier", verifier=None, order=1,
        )
        self.rung = CorridorRung.objects.create(
            corridor=self.corridor, key="oig", display_name="OIG exclusion list",
            requirement="optional", input=CorridorRung.Input.AUTOMATIC,
            verifier="fake_screen", route=CorridorRung.Route.FREE, order=10,
        )
        self.screen = FakeScreen()
        register("fake_screen", self.screen)
        self.person = Person.objects.create(
            email="n@example.com", full_name="Nora Lane", screening_consent_at=timezone.now(),
        )
        self.user = User.objects.create_user(username="n@example.com", email="n@example.com")

    def patch_me(self, payload):
        request = APIRequestFactory().patch("/api/me/", payload, format="json")
        force_authenticate(request, user=self.user)
        with patch("careers.views._person_for", return_value=self.person):
            return me(request)

    def submit(self, payload):
        request = APIRequestFactory().post("/api/claims/submit/", payload, format="json")
        force_authenticate(request, user=self.user)
        with patch("careers.views._person_for", return_value=self.person):
            return submit_rung(request)

    def screens(self):
        return VerificationClaim.objects.filter(
            person=self.person, shape=VerificationClaim.Shape.SCREENS,
            status=VerificationClaim.Status.ACTIVE,
        )


class ScreeningTests(ScreeningFixture, TestCase):
    def test_nothing_runs_without_a_date_of_birth(self):
        self.submit({"corridor_key": "sample", "rung_key": "licence", "value": "A1"})
        self.assertEqual(self.screen.calls, 0)

    def test_giving_a_date_of_birth_runs_the_checks(self):
        self.submit({"corridor_key": "sample", "rung_key": "licence", "value": "A1"})
        self.patch_me({"date_of_birth": "1984-02-11"})
        self.assertEqual(self.screen.calls, 1)
        self.assertEqual(self.screen.subjects[0]["date_of_birth"], "1984-02-11")
        [claim] = self.screens()
        self.assertEqual(claim.rung_key, "oig")
        self.assertEqual(claim.source_as_of, date(2026, 9, 10))

    def test_saving_the_same_details_again_does_not_re_screen(self):
        # SAM.gov allows ten calls a day on a free key. Re-asking an answered
        # question on every profile save would spend them on nothing.
        self.submit({"corridor_key": "sample", "rung_key": "licence", "value": "A1"})
        self.patch_me({"date_of_birth": "1984-02-11"})
        self.patch_me({"date_of_birth": "1984-02-11", "full_name": "Nora Lane"})
        self.patch_me({"phone": "555"})
        self.assertEqual(self.screen.calls, 1)

    def test_a_new_name_re_screens(self):
        self.submit({"corridor_key": "sample", "rung_key": "licence", "value": "A1"})
        self.patch_me({"date_of_birth": "1984-02-11"})
        self.patch_me({"previous_names": ["Nora Smith"]})
        self.assertEqual(self.screen.calls, 2)
        self.assertEqual(self.screens().count(), 1)

    def test_a_submission_runs_checks_that_have_never_run(self):
        # Covers a person who gave their details before this corridor had any
        # checks, and so would otherwise never be screened.
        self.person.date_of_birth = date(1984, 2, 11)
        self.person.save()
        self.submit({"corridor_key": "sample", "rung_key": "licence", "value": "A1"})
        self.submit({"corridor_key": "sample", "rung_key": "licence", "value": "A2"})
        self.assertEqual(self.screen.calls, 1)

    def test_a_failing_check_never_fails_the_save(self):
        register("fake_screen", FakeScreen(fail=True))
        self.submit({"corridor_key": "sample", "rung_key": "licence", "value": "A1"})
        response = self.patch_me({"date_of_birth": "1984-02-11"})
        self.assertEqual(response.status_code, 200)
        self.person.refresh_from_db()
        self.assertEqual(self.person.date_of_birth, date(1984, 2, 11))
        self.assertEqual(self.screens().count(), 0)

    def test_a_check_is_not_a_step_on_the_ladder_but_still_screens(self):
        self.assertEqual(self.rung.input, "automatic")
        self.person.date_of_birth = date(1984, 2, 11)
        self.person.save()
        self.assertEqual(run_screens(self.person, corridor=self.corridor), 1)

    def test_rescreening_everyone_reaches_only_people_who_can_be_screened(self):
        self.person.date_of_birth = date(1984, 2, 11)
        self.person.save()
        run_screens(self.person, corridor=self.corridor)
        Person.objects.create(email="no-dob@example.com", full_name="No Birthday",
                              screening_consent_at=timezone.now())
        Person.objects.create(email="no-consent@example.com", full_name="Not Asked",
                              date_of_birth=date(1980, 1, 1))
        self.assertEqual(rescreen_everyone(), 1)
        self.assertEqual(self.screen.calls, 2)


class ConsentTests(ScreeningFixture, TestCase):
    """Nobody is searched for on a federal exclusion list without agreeing to it."""

    def setUp(self):
        super().setUp()
        self.person.screening_consent_at = None
        self.person.save()
        self.submit({"corridor_key": "sample", "rung_key": "licence", "value": "A1"})

    def test_nothing_runs_without_consent_even_with_a_date_of_birth(self):
        self.patch_me({"date_of_birth": "1984-02-11"})
        self.assertEqual(self.screen.calls, 0)

    def test_agreeing_runs_the_checks(self):
        self.patch_me({"date_of_birth": "1984-02-11"})
        self.patch_me({"screening_consent": True})
        self.assertEqual(self.screen.calls, 1)

    def test_the_server_stamps_when_consent_was_given(self):
        before = timezone.now()
        response = self.patch_me({"screening_consent": True})
        self.person.refresh_from_db()
        self.assertGreaterEqual(self.person.screening_consent_at, before)
        self.assertIsNotNone(response.data["person"]["screening_consent_at"])

    def test_a_client_cannot_backdate_consent(self):
        self.patch_me({"screening_consent": True,
                       "screening_consent_at": "2020-01-01T00:00:00Z"})
        self.person.refresh_from_db()
        self.assertEqual(self.person.screening_consent_at.year, timezone.now().year)

    def test_agreeing_again_keeps_the_original_time(self):
        self.patch_me({"screening_consent": True})
        self.person.refresh_from_db()
        first = self.person.screening_consent_at
        self.patch_me({"screening_consent": True})
        self.person.refresh_from_db()
        self.assertEqual(self.person.screening_consent_at, first)

    def test_withdrawing_stops_further_checks(self):
        self.patch_me({"date_of_birth": "1984-02-11", "screening_consent": True})
        self.patch_me({"screening_consent": False})
        self.patch_me({"previous_names": ["Nora Smith"]})
        self.assertEqual(self.screen.calls, 1)
        self.person.refresh_from_db()
        self.assertIsNone(self.person.screening_consent_at)

    def test_a_future_date_of_birth_is_refused(self):
        response = self.patch_me({"date_of_birth": "2999-01-01"})
        self.assertEqual(response.status_code, 400)


class WhatThePersonSeesTests(TestCase):
    def setUp(self):
        self.corridor = Corridor.objects.create(key="sample", display_name="Sample")
        self.person = Person.objects.create(email="w@example.com")

    def claim(self, value):
        from django.utils import timezone
        return VerificationClaim.objects.create(
            person=self.person, corridor=self.corridor, rung_key="oig",
            fact_type="oig_exclusion", fact_value=value,
            method=VerificationClaim.Method.PRIMARY_SOURCE,
            shape=VerificationClaim.Shape.SCREENS, source_as_of=date(2026, 9, 10),
            matched_on=["full_name"], checked_at=timezone.now(),
        )

    def test_a_possible_match_reaches_the_person_as_under_review(self):
        # Usually somebody else with the same name. A human looks first.
        data = VerificationClaimSerializer(self.claim("possible_match")).data
        self.assertEqual(data["fact_value"], "under_review")

    def test_a_match_also_waits_for_a_person(self):
        data = VerificationClaimSerializer(self.claim("match")).data
        self.assertEqual(data["fact_value"], "under_review")

    def test_the_real_outcome_stays_in_the_table_for_the_reviewer(self):
        claim = self.claim("possible_match")
        claim.refresh_from_db()
        self.assertEqual(claim.fact_value, "possible_match")

    def test_no_match_is_shown_as_it_is(self):
        data = VerificationClaimSerializer(self.claim("no_match")).data
        self.assertEqual(data["fact_value"], "no_match")
        self.assertEqual(data["source_as_of"], "2026-09-10")


class StateLicenceRegisterWiringTests(TestCase):
    """The six open-data states reach the licence agent in every deployment."""

    def test_the_licence_agent_can_reach_the_state_registers(self):
        from kormic_agents import registry

        agent = registry.get("licence")
        self.assertIsNotNone(agent)
        for state in ("US-TX", "US-IL", "US-WA", "US-CO", "US-CT", "US-DE"):
            self.assertIsNotNone(agent.directory.find("licence", state), state)
        # A state with no route stays unrouted rather than borrowing another's.
        self.assertIsNone(agent.directory.find("licence", "US-NY"))
