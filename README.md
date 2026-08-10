# Kormic Careers — Backend

The `careers` Django app: corridors, the ladder, and one verification claim per
fact.

This repository contains **two different things**, and the difference matters:

| Path | What it is |
|---|---|
| `careers/` | **Ships.** The app that integrates into the main project. |
| `accounts-multirole.patch` | **Ships.** Patch against the existing accounts app. |
| `accounts-tests/` | **Ships.** Tests for the patch, run in the main project. |
| `manage.py`, `config/` | Dev scaffold. Lets the app run standalone. |
| `devauth/` | **Dev stub.** Stands in for the real `accounts`. |
| `django_api/` | **Dev stub.** ⚠️ Shadows a real app. See below. |

---

## Run it

```bash
pip install -r requirements.txt

export DJANGO_DEBUG=1          # Windows: set DJANGO_DEBUG=1
python manage.py migrate
python manage.py runserver 8900
```

Seed a corridor to have something to serve:

```bash
python manage.py shell -c "
from careers.models import Corridor, CorridorRung
c = Corridor.objects.create(key='sample', display_name='Sample corridor')
for k,n,r,i,v,o in [
 ('licence','Licence','required','identifier_with_jurisdiction','licence_bot',1),
 ('certification','Certification','required','identifier','cert_bot',2),
 ('registry','Registry number','optional','identifier','registry_bot',3),
 ('github','GitHub','not_applicable','oauth',None,4),
 ('cv','CV','required','document_upload',None,5),
 ('linkedin','LinkedIn','optional','screenshots',None,6)]:
    CorridorRung.objects.create(corridor=c,key=k,display_name=n,requirement=r,input=i,verifier=v,order=o)
"
```

That fixture matches `sampleCorridor` in the client, so the app behaves the same
against this backend as it does on its mocks.

### Tests

```bash
python manage.py test
```

23 tests. They cover the rules that cannot be retrofitted: the corridor drives
the ladder, a claim always carries a method and a date, a raised method
supersedes rather than edits, and a candidate never sees the practice's internal
states.

---

## Endpoints

| Route | Auth | Notes |
|---|---|---|
| `GET /api/corridors/<key>/` | none | Open on purpose — the tour runs before signup |
| `POST /api/claims/submit/` | JWT | Writes a `self_attested` claim |
| `GET /api/claims/<rung_key>/` | JWT | Recomputes expiry on read |
| `POST /api/agent/escalations/` | JWT | Scoped to the caller |
| `POST /api/auth/refresh/` | none | `{refresh}` → `{access, refresh}` |
| `POST /api/dev/login/` | none | **Dev only**, 404s unless `DEBUG` |

`careers/urls.py` declares its paths **without** the `/api/` prefix, so the
project decides where the app is mounted. The client's endpoint map is written
against `/api/...`, so it must be included as:

```python
path("api/", include("careers.urls"))
```

### Not served here

The claim front door (`/api/claim/start|verify|confirm/`) stays in
`institutes_list`, and chat history and send stay in `django_api`. Both are
reused rather than forked — see the note at the top of `careers/views.py`.

`/api/dev/login/` is the local stand-in for `claim/confirm/`: it answers in the
same shape, so the client is authenticated from that point on.

Still unserved anywhere, and needed by the client: the OAuth authorize/status
pair, document upload, Navigator rename, and push registration.

---

## Connecting the app

In `kormic-careers-app/app.json`:

```json
"extra": {
  "apiHost": "http://localhost:8900",
  "corridorKey": "sample",
  "useMocks": false
}
```

CORS already allows `http://localhost:8081` in DEBUG, which is where Expo serves
web.

---

## Integrating into the main project

1. Copy **`careers/` only**.
2. Add `"careers"` to `INSTALLED_APPS` and mount it under `api/`.
3. Apply `accounts-multirole.patch` **against a copy of the real database
   first**, and confirm no existing account has a null role.
4. Copy `accounts-tests/test_multirole.py` into the accounts app's tests.
5. `python manage.py migrate`.

**Do not copy `django_api/`.** It shares a name with a real app in the main
project and would shadow it, taking the deployment down. Same for `devauth/`,
`config/` and `manage.py` — all four exist only so this can run alone.

### One thing that must change upstream

`careers` scopes the escalation lookup to `PendingQuery.person_id`. The real
`PendingQuery` is still owner-typed by university and student, so **that field
has to be added** before this route returns anything. Until then it refuses,
deliberately: serving the rows unscoped is how one candidate reads another's
escalation.

The test for this skips itself when the field is absent, and the skip reason
says why.

---

## Configuration

Nothing is hardcoded. See `.env.example`.

| Variable | Default |
|---|---|
| `DJANGO_DEBUG` | `False` |
| `DJANGO_SECRET_KEY` | none — **raises** if `DEBUG` is off |
| `DJANGO_ALLOWED_HOSTS` | localhost only, and only in `DEBUG` |
| `DJANGO_CORS_ORIGINS` | `localhost:8081`, and only in `DEBUG` |
| `DJANGO_DB_*` | sqlite in the project directory |

The four settings the brief flags as needing fixing before `careers.kormic.ai`
posts from a browser are done properly here rather than repeated, so
`config/settings.py` is a worked example of each: no wildcard hosts, no
`CORS_ALLOW_ALL_ORIGINS` anywhere, `DEBUG` off unless asked, and a secret key
that has to come from the environment.

**Session authentication is deliberately absent** from
`REST_FRAMEWORK`. With it, DRF answers `403` for an expired credential; the
client only refreshes on `401`, so a person would be signed out instead of
renewed. JWT-only keeps that path working.
