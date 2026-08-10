"""
DEV STUB — delete on integration. ⚠️ This app shadows a real one.

`careers/views.py` does `from django_api.models import PendingQuery`, so a
module of exactly this name has to exist for the escalation route to run. The
real django_api is a large app in the main project; this is four fields of it.

**Do not copy this directory into the real project.** It would shadow the real
django_api and take the whole deployment down. It exists only so the careers
app can be run standalone.

One field here does not yet exist upstream: `person_id`. That is deliberate.
The brief lists generalising PendingQuery from a university/student owner to an
org-typed one as still open, and careers now scopes the escalation lookup to
the caller's person. Until the real model carries an equivalent, that route
refuses rather than serving another candidate's rows.
"""
from django.db import models


class PendingQuery(models.Model):
    """The four fields careers reads. Nothing else about the real model."""

    # CharField primary key because careers casts incoming ids with str().
    id = models.CharField(primary_key=True, max_length=64)

    # The scoping field the real model still needs.
    person_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    # The internal state. careers collapses this to one of three the candidate
    # may see, and anything unrecognised reads as pending.
    status = models.CharField(max_length=32, default="pending")

    question = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"PendingQuery({self.id}, {self.status})"
