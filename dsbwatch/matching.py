"""Entscheidet, welche Plan-Zeilen die eigenen Kurse betreffen."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dsbwatch.infosheet import InfoConfig
from dsbwatch.parse import Entry


@dataclass(frozen=True)
class Rule:
    label: str
    match: tuple[str, ...]
    exclude: tuple[str, ...] = ()

    def applies_to(self, haystack: str) -> bool:
        if not self.match:
            return False
        if any(term in haystack for term in self.exclude):
            return False
        return all(term in haystack for term in self.match)


@dataclass
class Config:
    rules: list[Rule] = field(default_factory=list)
    ignore_case: bool = True
    notify_all: bool = False
    notify_on_images: bool = True
    info: InfoConfig = field(default_factory=InfoConfig)

    def label_for(self, entry: Entry) -> str | None:
        """Label der ersten passenden Regel, sonst None."""
        haystack = entry.searchable
        if self.ignore_case:
            haystack = haystack.lower()
        for rule in self.rules:
            if rule.applies_to(haystack):
                return rule.label
        return None


def _terms(values, ignore_case: bool) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    return tuple(v.lower() if ignore_case else v for v in cleaned)


def load_config(path: str | Path) -> Config:
    """Liest config.yaml. Fehlt die Datei, wird alles gemeldet (nichts wird gefiltert)."""
    path = Path(path)
    if not path.exists():
        return Config(notify_all=True)

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    ignore_case = bool(data.get("ignore_case", True))
    notify_all = bool(data.get("notify_all", False))
    notify_on_images = bool(data.get("notify_on_images", True))

    rules: list[Rule] = []
    for index, raw in enumerate(data.get("courses") or [], start=1):
        if isinstance(raw, str):  # Kurzform: nur ein Suchbegriff
            raw = {"label": raw, "match": [raw]}
        match = _terms(raw.get("match"), ignore_case)
        if not match:
            raise ValueError(f"config.yaml: Kurs #{index} hat kein 'match'.")
        rules.append(
            Rule(
                label=str(raw.get("label") or " + ".join(match)),
                match=match,
                exclude=_terms(raw.get("exclude"), ignore_case),
            )
        )

    for term in data.get("always_notify") or []:
        rules.append(Rule(label="Allgemein", match=_terms([term], ignore_case)))

    if not rules:
        notify_all = True

    roh_info = data.get("info_sheet") or {}
    info = InfoConfig(
        enabled=bool(roh_info.get("enabled", False)),
        for_me=tuple(str(t) for t in (roh_info.get("for_me") or ())),
        include_general=bool(roh_info.get("include_general", True)),
        ignore=tuple(str(t) for t in (roh_info.get("ignore") or ())),
    )

    return Config(
        rules=rules,
        ignore_case=ignore_case,
        notify_all=notify_all,
        notify_on_images=notify_on_images,
        info=info,
    )


def select(entries: list[Entry], config: Config) -> list[tuple[str, Entry]]:
    """Liefert (Label, Eintrag) für alle Zeilen, die einen der eigenen Kurse betreffen."""
    if config.notify_all:
        return [("", entry) for entry in entries]
    hits: list[tuple[str, Entry]] = []
    for entry in entries:
        label = config.label_for(entry)
        if label is not None:
            hits.append((label, entry))
    return hits
