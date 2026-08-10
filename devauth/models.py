"""
DEV STUB — delete on integration.

This stands in for the real `accounts` app. It reproduces `Account` exactly as
`accounts-multirole.patch` leaves it, and no more: careers only ever reads
`request.user.account.person_id`, so that is the whole surface that has to be
right.

It exists because careers cannot be run at all without something answering
`request.user.account`. When careers moves into the real project this app is
removed and the patch is applied to the real accounts instead. Nothing here is
imported by careers, so removing it touches nothing that ships.
"""
from django.contrib.auth.models import User
from django.db import models


class Account(models.Model):
    """Mirrors the patched shape. Kept in step with accounts-multirole.patch."""

    class Role(models.TextChoices):
        STUDENT = "student", "Student"
        UNIVERSITY = "university", "University"
        INSTITUTE = "institute", "Institute"
        CANDIDATE = "candidate", "Candidate"
        SUPERUSER = "superuser", "Superuser"

    # related_name="account" is the part careers depends on.
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="account")

    role = models.CharField(max_length=20, choices=Role.choices)
    roles = models.JSONField(default=list, blank=True)

    student_id = models.CharField(max_length=255, null=True, blank=True, unique=True, db_index=True)
    university_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    institute_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    # The join to careers.Person. Deliberately not student_id.
    person_id = models.CharField(max_length=64, null=True, blank=True, unique=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        """Keep `roles` and `role` in step, so neither can drift from the other."""
        if self.role and self.role not in self.roles:
            self.roles = [self.role, *self.roles]
        super().save(*args, **kwargs)

    def has_role(self, role: str) -> bool:
        return role == self.role or role in (self.roles or [])

    def add_role(self, role: str) -> None:
        if not self.has_role(role):
            self.roles = [*(self.roles or []), role]
            self.save(update_fields=["roles"])

    def __str__(self) -> str:
        return f"Account({self.user.email}, {'/'.join(self.roles or [self.role])})"
