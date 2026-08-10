"""Persistenter Zustand: was wurde schon gemeldet, wie oft ist der Job gescheitert.

Auf GitHub-Actions-Runnern gibt es kein bleibendes Dateisystem — der Workflow
committet state/state.json deshalb nach jedem Lauf zurück ins Repo.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# Einträge ohne erkennbares Plandatum so lange behalten, bevor sie verfallen.
UNDATED_TTL_DAYS = 14

# Bewusst OHNE Auth-Token: state.json wird ins Repo committet, und das Token ist
# ein Zugangsschlüssel. Ein /authid-Request pro Lauf ist billiger als ein Secret,
# das dauerhaft in der Versionsgeschichte liegt.
DEFAULT_STATE: dict[str, Any] = {
    "version": 1,
    "seen": {},          # fingerprint -> "keep until" (ISO-Datum)
    "image_hashes": {},  # Plan-URL -> SHA der Bildbytes
    "fail_count": 0,
    "last_error": None,
    "alerted": False,    # wurde für die aktuelle Fehlerserie schon gewarnt?
}


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        # deepcopy, sonst teilen sich alle Instanzen die verschachtelten Dicts.
        self.data: dict[str, Any] = deepcopy(DEFAULT_STATE)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # kaputter State ist kein Grund, den Lauf abzubrechen
        if isinstance(loaded, dict):
            self.data = {**deepcopy(DEFAULT_STATE), **loaded}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # --- Dedup ---------------------------------------------------------
    def is_new(self, fingerprint: str) -> bool:
        return fingerprint not in self.data["seen"]

    def mark_seen(self, fingerprint: str, iso_date: str, today: date | None = None) -> None:
        today = today or date.today()
        fallback = (today + timedelta(days=UNDATED_TTL_DAYS)).isoformat()
        self.data["seen"][fingerprint] = iso_date or fallback

    def prune(self, today: date | None = None) -> int:
        """Wirft Einträge weg, deren Plandatum vorbei ist. Gibt die Anzahl zurück."""
        today_iso = (today or date.today()).isoformat()
        seen = self.data["seen"]
        stale = [key for key, keep_until in seen.items() if str(keep_until) < today_iso]
        for key in stale:
            del seen[key]
        return len(stale)

    # --- Bild-Pläne ----------------------------------------------------
    def image_changed(self, url: str, digest: str) -> bool:
        return self.data["image_hashes"].get(url) != digest

    def mark_image(self, url: str, digest: str) -> None:
        self.data["image_hashes"][url] = digest

    # --- Fehlerzähler --------------------------------------------------
    def record_failure(self, message: str) -> int:
        self.data["fail_count"] = int(self.data.get("fail_count") or 0) + 1
        self.data["last_error"] = message
        return self.data["fail_count"]

    def record_success(self) -> None:
        self.data["fail_count"] = 0
        self.data["last_error"] = None
        self.data["alerted"] = False

    @property
    def already_alerted(self) -> bool:
        return bool(self.data.get("alerted"))

    @already_alerted.setter
    def already_alerted(self, value: bool) -> None:
        self.data["alerted"] = value
