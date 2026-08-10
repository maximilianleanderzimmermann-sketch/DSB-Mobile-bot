"""Untis-Vertretungsplan-HTML in strukturierte Zeilen überführen.

Untis exportiert je Tag einen Titel-Block (``div.mon_title``) und darunter eine
Tabelle ``table.mon_list``. Welche Spalten diese Tabelle hat und in welcher
Reihenfolge, legt jede Schule selbst fest — deshalb wird die Kopfzeile gelesen
und daraus ein Mapping gebaut, statt feste Spaltenindizes anzunehmen.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

try:  # lxml ist schneller, html.parser tut es aber auch
    import lxml  # noqa: F401

    _PARSER = "lxml"
except ImportError:  # pragma: no cover - hängt an der Installation
    _PARSER = "html.parser"

DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")
SUBST_LINK_RE = re.compile(r"(subst|frames|navbar|index)[^/]*\.html?$", re.IGNORECASE)

# Kopfzeilen-Beschriftung -> internes Feld. Kleingeschrieben, ohne Klammern/Punkte.
HEADER_ALIASES: dict[str, str] = {
    "klasse": "klasse",
    "klassen": "klasse",
    "klasse(n)": "klasse",
    "kurs": "klasse",
    "kurse": "klasse",
    "stunde": "stunde",
    "std": "stunde",
    "stunden": "stunde",
    "position": "stunde",
    "fach": "fach",
    "faecher": "fach",
    "vertretungs-fach": "fach",
    "lehrer": "lehrer",
    "lehrkraft": "lehrer",
    "vertreter": "lehrer",
    "vertretung": "lehrer",
    "vertreter-lehrer": "lehrer",
    # Schreibweise der Edith-Stein-Schule: "Vertr. von" / "(Le.) nach"
    "le.-nach": "lehrer",
    "le-nach": "lehrer",
    "vertr.-von": "vertr_von",
    "vertr-von": "vertr_von",
    "statt-lehrer": "vertr_von",
    "raum": "raum",
    "raeume": "raum",
    "art": "art",
    "vertretungs-art": "art",
    "status": "art",
    "text": "text",
    "bemerkung": "text",
    "bemerkungen": "text",
    "mitteilung": "text",
    "vertretungs-text": "text",
    "entfall": "art",
}

FIELDS = ("klasse", "stunde", "fach", "lehrer", "vertr_von", "raum", "art", "text")

# Untis schreibt "---" in Zellen, die durch den Entfall gegenstandslos werden.
EMPTY_MARKERS = {"---", "--", "?", ""}


@dataclass(frozen=True)
class Entry:
    """Eine Zeile des Vertretungsplans."""

    plan_date: str = ""  # Rohtext des Titels, z.B. "10.8.2026 Montag"
    klasse: str = ""
    stunde: str = ""
    fach: str = ""
    lehrer: str = ""
    vertr_von: str = ""
    raum: str = ""
    art: str = ""
    text: str = ""
    extra: tuple[str, ...] = ()
    plan_title: str = ""
    source_url: str = ""

    @property
    def searchable(self) -> str:
        """Alles, wogegen ein Kursfilter matchen darf."""
        parts = [self.plan_title, self.klasse, self.fach, self.lehrer, self.vertr_von,
                 self.raum, self.art, self.text]
        return " | ".join(p for p in (*parts, *self.extra) if p)

    @property
    def iso_date(self) -> str:
        """Plandatum als YYYY-MM-DD, oder "" wenn im Titel keins steht."""
        match = DATE_RE.search(self.plan_date)
        if not match:
            return ""
        day, month, year = (int(g) for g in match.groups())
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return ""

    def fingerprint(self) -> str:
        """Stabiler Schlüssel. Ändert sich ein Feld, ist es ein neuer Eintrag."""
        raw = "|".join(
            [self.iso_date or self.plan_date, self.klasse, self.stunde, self.fach,
             self.lehrer, self.vertr_von, self.raum, self.art, self.text, *self.extra]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def summary(self) -> str:
        """Einzeiler für die Benachrichtigung."""
        head = " · ".join(p for p in (self.klasse, _stunde_label(self.stunde), self.fach) if p)

        details: list[str] = []
        if self.art:
            details.append(self.art)
        if self.raum not in EMPTY_MARKERS:
            details.append(_raum_label(self.raum))
        # "(Le.) nach" trägt bei Entfall nur "Entfall" — das steht schon unter Art.
        if self.lehrer not in EMPTY_MARKERS and self.lehrer != self.art:
            details.append(_lehrer_label(self.lehrer))
        if self.vertr_von not in EMPTY_MARKERS and self.vertr_von != self.art:
            details.append(f"statt {self.vertr_von}")
        if self.text:
            details.append(self.text)
        details.extend(e for e in self.extra if e not in EMPTY_MARKERS and e != self.art)

        tail = " · ".join(details)
        return f"{head} — {tail}" if head and tail else head or tail


def _stunde_label(value: str) -> str:
    return f"{value}. Std" if value and re.fullmatch(r"[\d\-/. ]+", value) else value


def _raum_label(value: str) -> str:
    return f"Raum {value}" if value else ""


def _lehrer_label(value: str) -> str:
    return f"bei {value}" if value else ""


def decode(raw: bytes) -> str:
    """Untis-Exporte deklarieren ihr Encoding unzuverlässig — Kandidaten durchprobieren."""
    match = re.search(rb"charset=[\"']?([\w-]+)", raw[:4096], re.IGNORECASE)
    declared = match.group(1).decode("ascii", "ignore") if match else None
    for candidate in (declared, "utf-8", "cp1252"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("cp1252", errors="replace")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _normalise_header(text: str) -> str:
    key = _clean(text).lower().rstrip(":.")
    key = key.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    key = key.replace("(", "").replace(")", "").replace(" ", "-")
    return key


def _cell_text(cell) -> str:
    """Zellentext.

    Untis schreibt Ersetzungen als ``<s>alt</s>?neu`` — daraus wird "alt → neu",
    damit in der Benachrichtigung sofort sichtbar ist, was sich geändert hat.
    """
    struck_tags = cell.find_all(["strike", "s", "del"])
    struck = " ".join(filter(None, (_clean(tag.get_text()) for tag in struck_tags)))
    for tag in struck_tags:
        tag.decompose()
    rest = _clean(cell.get_text(" ")).lstrip("?").strip()

    if struck and rest:
        return f"{struck} → {rest}"
    return rest or struck


def _header_map(row) -> dict[int, str] | None:
    """Baut Spaltenindex -> Feldname, wenn die Zeile wie eine Kopfzeile aussieht."""
    cells = row.find_all(["th", "td"])
    if not cells:
        return None
    mapping: dict[int, str] = {}
    for index, cell in enumerate(cells):
        field_name = HEADER_ALIASES.get(_normalise_header(cell.get_text()))
        if field_name and field_name not in mapping.values():
            mapping[index] = field_name
    # Mindestens zwei erkannte Spalten, sonst ist es vermutlich eine Datenzeile.
    return mapping if len(mapping) >= 2 else None


def _parse_table(table, plan_date: str, plan_title: str, source_url: str) -> list[Entry]:
    rows = table.find_all("tr")
    if not rows:
        return []

    mapping: dict[int, str] | None = None
    entries: list[Entry] = []
    for row in rows:
        if mapping is None:
            mapping = _header_map(row)
            if mapping is not None:
                continue  # Kopfzeile selbst ist kein Eintrag

        cells = row.find_all("td")
        if not cells:
            continue
        values = [_cell_text(cell) for cell in cells]
        if not any(values):
            continue

        fields = {name: "" for name in FIELDS}
        extra: list[str] = []
        for index, value in enumerate(values):
            name = (mapping or {}).get(index)
            if name:
                fields[name] = value
            elif value:
                extra.append(value)

        entries.append(
            Entry(
                plan_date=plan_date,
                extra=tuple(extra),
                plan_title=plan_title,
                source_url=source_url,
                **fields,
            )
        )
    return entries


def parse_page(html: str, plan_title: str = "", source_url: str = "") -> list[Entry]:
    """Alle Einträge einer Untis-Seite; mehrere Tage pro Seite werden unterstützt."""
    soup = BeautifulSoup(html, _PARSER)
    entries: list[Entry] = []
    current_date = ""
    for node in soup.select(".mon_title, table.mon_list"):
        if node.name == "table":
            entries.extend(_parse_table(node, current_date, plan_title, source_url))
        else:
            current_date = _clean(node.get_text())
    return entries


def find_followup_urls(html: str, base_url: str) -> list[str]:
    """Frameset oder Übersichtsseite? Dann die verlinkten Plan-Seiten einsammeln."""
    soup = BeautifulSoup(html, _PARSER)
    urls: list[str] = []
    for tag in soup.find_all(["frame", "iframe"]):
        src = tag.get("src")
        if src:
            urls.append(urljoin(base_url, src))
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if SUBST_LINK_RE.search(href.split("?")[0]):
            urls.append(urljoin(base_url, href))
    deduped: list[str] = []
    for url in urls:
        if url != base_url and url not in deduped:
            deduped.append(url)
    return deduped


def parse_plan(fetch, url: str, plan_title: str = "", max_depth: int = 2) -> list[Entry]:
    """Lädt ``url`` und folgt Framesets, bis Einträge gefunden werden.

    ``fetch`` ist eine Funktion url -> bytes (in Tests leicht zu ersetzen).
    """
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(url, 0)]
    entries: list[Entry] = []

    while queue:
        current, depth = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        html = decode(fetch(current))
        found = parse_page(html, plan_title=plan_title, source_url=current)
        if found:
            entries.extend(found)
            continue
        if depth < max_depth:
            queue.extend((next_url, depth + 1) for next_url in find_followup_urls(html, current))

    return entries


def today() -> date:
    return datetime.now().date()
