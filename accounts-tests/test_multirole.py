"""Multi-role and person_id behaviour on Account."""
from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import Account


class MultiRoleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="p@example.com", email="p@example.com")

    def test_existing_single_role_behaviour_is_unchanged(self):
        account = Account.objects.create(user=self.user, role=Account.Role.STUDENT, student_id="s1")
        self.assertEqual(account.role, "student")
        self.assertTrue(account.has_role(Account.Role.STUDENT))
        self.assertFalse(account.has_role(Account.Role.UNIVERSITY))

    def test_a_person_can_hold_a_second_role_without_losing_the_first(self):
        account = Account.objects.create(user=self.user, role=Account.Role.STUDENT, student_id="s1")
        account.add_role("candidate")
        account.refresh_from_db()
        self.assertEqual(account.role, "student")
        self.assertTrue(account.has_role(Account.Role.STUDENT))
        self.assertTrue(account.has_role("candidate"))

    def test_roles_and_role_cannot_drift(self):
        account = Account.objects.create(user=self.user, role=Account.Role.INSTITUTE)
        self.assertIn("institute", account.roles)

    def test_identifiers_stand_independently(self):
        account = Account.objects.create(
            user=self.user, role=Account.Role.STUDENT, student_id="s1", person_id="p_abc"
        )
        account.add_role("candidate")
        account.refresh_from_db()
        self.assertEqual(account.student_id, "s1")
        self.assertEqual(account.person_id, "p_abc")

    def test_person_id_is_unique(self):
        Account.objects.create(user=self.user, role=Account.Role.STUDENT, person_id="p_abc")
        other = User.objects.create_user(username="q@example.com", email="q@example.com")
        with self.assertRaises(Exception):
            Account.objects.create(user=other, role=Account.Role.STUDENT, person_id="p_abc")

    def test_adding_a_role_twice_is_a_no_op(self):
        account = Account.objects.create(user=self.user, role=Account.Role.STUDENT)
        account.add_role(Account.Role.STUDENT)
        account.add_role("candidate")
        account.add_role("candidate")
        account.refresh_from_db()
        self.assertEqual(sorted(account.roles), ["candidate", "student"])
