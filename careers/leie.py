"""
The OIG exclusion list (LEIE), as this deployment keeps it.

The `oig` agent in `kormic_agents` screens against whatever `LeieSource` it is
handed and knows nothing about where the file lives. This is that source for
careers: a folder of monthly downloads plus a `latest.json` naming the current
one and the date OIG published it.

**The publication date is recorded once, at download, and never guessed
later.** Every screen is stamped with it, so a wrong one is a false statement
about what was known and when. `fetch_leie` takes it from OIG's own
`Last-Modified` header — a fact about OIG's data, not about this machine — or
from `--as-of` when a person states it.

**A running server picks up a new month without a restart.** `LatestLeie`
re-reads `latest.json` when it changes and swaps the parsed list, so the
monthly cron job is the whole of the refresh.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, Optional

MANIFEST = "latest.json"


def manifest_path(folder: Path) -> Path:
    return Path(folder) / MANIFEST


def read_manifest(folder: Path) -> Optional[Dict[str, Any]]:
    """The current download, or None if there has never been one."""
    path = manifest_path(folder)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not data.get("file") or not data.get("as_of"):
        return None
    if not (Path(folder) / data["file"]).is_file():
        return None
    return data


def write_manifest(folder: Path, data: Dict[str, Any]) -> None:
    """
    Replace the manifest in one step. A server reading it mid-write must see
    the old month or the new one, never half of each.
    """
    path = manifest_path(folder)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temp, path)


class LatestLeie:
    """
    A `LeieSource` that always serves the current month.

    Checks the manifest's modification time on each screen — one stat call —
    and reloads only when it has changed. The 84,000-row parse happens once per
    month per process, not once per screen.
    """

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._lock = Lock()
        self._stamp: Optional[float] = None
        self._file: Any = None

    def available(self) -> bool:
        return read_manifest(self.folder) is not None

    def _current(self) -> Any:
        from kormic_agents.oig import LeieFile  # noqa: PLC0415

        try:
            stamp = manifest_path(self.folder).stat().st_mtime
        except OSError as exc:
            raise FileNotFoundError(
                f"No LEIE download in {self.folder}. Run `manage.py fetch_leie`."
            ) from exc

        with self._lock:
            if self._file is None or stamp != self._stamp:
                manifest = read_manifest(self.folder)
                if manifest is None:
                    raise FileNotFoundError(f"The LEIE manifest in {self.folder} is unreadable.")
                self._file = LeieFile(
                    str(self.folder / manifest["file"]),
                    as_of=date.fromisoformat(manifest["as_of"]),
                )
                self._stamp = stamp
            return self._file

    def as_of(self) -> date:
        return self._current().as_of()

    def records(self) -> Iterable[Any]:
        return self._current().records()
