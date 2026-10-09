"""
Download this month's OIG exclusion list.

    python manage.py fetch_leie                 # download if OIG has a newer file
    python manage.py fetch_leie --rescreen      # ...and re-screen everyone against it
    python manage.py fetch_leie --as-of 2026-09-10 --file UPDATED.csv   # a file you already have

Meant to run monthly from cron or Windows Task Scheduler. Safe to run more
often: a file OIG has not changed is not downloaded twice.

The new file only becomes current after it has been parsed and found to hold a
plausible number of records. A truncated download or an error page must never
replace a good list — a screen against an empty list reports "no match" for
everybody, which is the one failure this has to make impossible.
"""
from __future__ import annotations

import hashlib
import shutil
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from careers.leie import read_manifest, write_manifest

# The real file holds ~80,000 rows. Far fewer means a broken download.
MIN_RECORDS = 10_000
# Older months kept beside the current one, so a screen can be traced back to
# the exact list it ran against.
KEEP = 3


class Command(BaseCommand):
    help = "Download the OIG exclusion list (LEIE) and make it the current one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--as-of", dest="as_of", default="",
            help="The date OIG published the file, YYYY-MM-DD. Defaults to OIG's Last-Modified header.",
        )
        parser.add_argument(
            "--file", default="",
            help="Use a file already downloaded instead of fetching one. Needs --as-of.",
        )
        parser.add_argument("--force", action="store_true",
                            help="Replace the current list even if OIG has not changed it.")
        parser.add_argument("--rescreen", action="store_true",
                            help="Afterwards, re-screen every person who can be screened.")

    def handle(self, *args, **options):
        folder = Path(settings.LEIE_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        current = read_manifest(folder)

        if options["file"]:
            if not options["as_of"]:
                raise CommandError("--file needs --as-of: the date OIG published that file.")
            as_of = _parse_date(options["as_of"])
            incoming = folder / "incoming.csv"
            shutil.copyfile(options["file"], incoming)
            source = str(Path(options["file"]).resolve())
        else:
            incoming, as_of = self._download(folder, options["as_of"])
            source = settings.LEIE_URL

        digest = _sha256(incoming)
        if current and current.get("sha256") == digest and not options["force"]:
            incoming.unlink()
            self.stdout.write(f"Already current: the list dated {current['as_of']}.")
            return self._maybe_rescreen(options)

        records = _count_records(incoming, as_of)
        if records < MIN_RECORDS:
            incoming.unlink()
            raise CommandError(
                f"The download holds only {records} records, so it is not a real LEIE. "
                "The current list was left in place."
            )

        name = f"LEIE-{as_of.isoformat()}.csv"
        incoming.replace(folder / name)
        write_manifest(folder, {
            "file": name,
            "as_of": as_of.isoformat(),
            "records": records,
            "sha256": digest,
            "source": source,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        _prune(folder, keep=name)

        self.stdout.write(self.style.SUCCESS(
            f"Current list is now dated {as_of.isoformat()} ({records:,} records)."
        ))
        if current is None:
            self.stdout.write(
                "This is the first list in this deployment. Restart the server once to "
                "switch OIG screening on; later months are picked up without a restart."
            )
        return self._maybe_rescreen(options)

    def _download(self, folder: Path, as_of_text: str):
        import requests  # noqa: PLC0415

        incoming = folder / "incoming.csv"
        try:
            with requests.get(settings.LEIE_URL, stream=True, timeout=60) as response:
                response.raise_for_status()
                header = response.headers.get("Last-Modified", "")
                with open(incoming, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1 << 16):
                        handle.write(chunk)
        except requests.RequestException as exc:
            if incoming.exists():
                incoming.unlink()
            raise CommandError(f"Could not download the LEIE: {exc}") from exc

        if as_of_text:
            return incoming, _parse_date(as_of_text)
        try:
            return incoming, parsedate_to_datetime(header).date()
        except (TypeError, ValueError) as exc:
            incoming.unlink()
            raise CommandError(
                "OIG sent no publication date. Run again with --as-of YYYY-MM-DD, "
                "taken from https://oig.hhs.gov/exclusions/exclusions_list.asp"
            ) from exc

    def _maybe_rescreen(self, options):
        if not options["rescreen"]:
            return
        from careers.services import rescreen_everyone  # noqa: PLC0415

        screened = rescreen_everyone(verifiers={"oig", "exclusion"})
        self.stdout.write(f"Re-screened {screened} people against the current list.")


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise CommandError(f"{text!r} is not a date in YYYY-MM-DD form.") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_records(path: Path, as_of: date) -> int:
    """Parsed with the agent's own reader, so a file it cannot read is refused here."""
    from kormic_agents.oig import LeieFile  # noqa: PLC0415

    try:
        return len(LeieFile(str(path), as_of=as_of).records())
    except Exception:  # noqa: BLE001 — anything unreadable is simply not a list
        return 0


def _prune(folder: Path, keep: str) -> None:
    older = sorted(
        (p for p in folder.glob("LEIE-*.csv") if p.name != keep),
        key=lambda p: p.name, reverse=True,
    )
    for path in older[KEEP - 1:]:
        path.unlink(missing_ok=True)
