"""Das tägliche Infoblatt (PNG) per OCR in filterbare Meldungen zerlegen.

Das Blatt ist zweispaltig aufgebaut:

    Alle:        Die Pavillonhöfe sind heute in den
                 Pausen nicht zu betreten
    KuK Jg. 5:   14:30 Uhr Päd. Konf. Jg. 5
    Große Halle 1. - 4. Stunde nicht verfügbar      <- volle Breite, gilt für alle

Links steht, wen es betrifft, rechts die Meldung. Zeilen über die volle Breite
haben keinen Empfänger und gelten allgemein. Diese Struktur ist der ganze Trick:
ohne sie ließe sich nicht entscheiden, ob eine Meldung einen angeht.

Das OCR-Modell kennt keine deutschen Umlaute — aus "Pavillonhöfe" wird
"Pavillonhofe", aus "Große" wird "GroBe". Verglichen wird deshalb grundsätzlich
über :func:`normalise`, das beide Seiten entschärft.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache

# Textbereiche unterhalb dieser Erkennungsgüte werden verworfen.
MIN_CONFIDENCE = 0.5

# Anteil der Blattbreite, bis zu dem die linke Spalte reicht.
LEFT_COLUMN = 0.25
# Bis zu dieser Breite (Anteil der Blattbreite) gilt ein linker Kasten als
# Empfänger-Etikett und nicht als Meldung über die volle Breite.
LABEL_MAX_WIDTH = 0.30
# Zwei Kästen gehören zur selben Zeile, wenn ihre Mitten näher beieinander
# liegen als dieser Anteil der Blatthöhe.
ROW_TOLERANCE = 0.015

LABEL_RE = re.compile(r"^[A-ZÄÖÜa-zäöü][\w.,&\s/-]{0,24}:$")

# Bewusst NICHT ae/oe/ue: Das OCR-Modell wirft die Punkte einfach weg
# ("Pavillonhöfe" -> "Pavillonhofe", "Päd." -> "Pad.") und liest ß als B
# ("Große" -> "GroBe"). Die Normalisierung muss dieselbe Verstümmelung
# nachvollziehen, sonst findet ein Suchbegriff aus der config seinen
# erkannten Gegenpart nie.
UMLAUTE = {
    "ä": "a", "ö": "o", "ü": "u", "ß": "b",
    "Ä": "a", "Ö": "o", "Ü": "u",
}


def normalise(text: str) -> str:
    """Kleinschreibung, Umlaute entschärft, Satzzeichen und Leerraum vereinheitlicht.

    Muss auf OCR-Text und auf Suchbegriffe gleichermaßen angewandt werden, sonst
    findet "Jg. 13" das erkannte "Jg 13" nicht.
    """
    for zeichen, ersatz in UMLAUTE.items():
        text = text.replace(zeichen, ersatz)
    text = text.lower()
    text = re.sub(r"[^\w]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class Notice:
    """Eine zusammenhängende Meldung des Infoblatts."""

    addressee: str  # "Alle", "Jg. 6", "" wenn über die volle Breite
    text: str
    sheet_title: str = ""

    def fingerprint(self) -> str:
        raw = f"{normalise(self.addressee)}|{normalise(self.text)}"
        return "info:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]

    def summary(self) -> str:
        return f"{self.addressee}: {self.text}" if self.addressee else self.text


@lru_cache(maxsize=1)
def _engine():
    """OCR-Modell einmal laden — der erste Aufruf kostet ein paar Sekunden."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:  # pragma: no cover - hängt an der Installation
        raise RuntimeError(
            "OCR-Bibliothek fehlt. Installieren mit: pip install rapidocr-onnxruntime"
        ) from exc
    return RapidOCR()


def read_boxes(image: bytes) -> list[tuple[float, float, float, float, str]]:
    """OCR ausführen. Liefert (x0, y0, y1, breite, text) je erkanntem Bereich."""
    import numpy as np

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("opencv fehlt — gehört zu rapidocr-onnxruntime") from exc

    bild = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if bild is None:
        raise ValueError("Bild konnte nicht gelesen werden.")

    ergebnis, _ = _engine()(bild)
    boxen = []
    for box, text, confidence in ergebnis or []:
        if float(confidence) < MIN_CONFIDENCE or not text.strip():
            continue
        xs = [punkt[0] for punkt in box]
        ys = [punkt[1] for punkt in box]
        boxen.append((min(xs), min(ys), max(ys), max(xs) - min(xs), text.strip()))
    return boxen, bild.shape[1], bild.shape[0]


def _mitte(kasten) -> float:
    return (kasten[1] + kasten[2]) / 2


def group(boxen, breite: float, hoehe: float, sheet_title: str = "") -> list[Notice]:
    """Erkannte Bereiche zu Meldungen zusammensetzen.

    Ein Empfänger-Etikett steht vertikal *mittig* zu seinem mehrzeiligen Text.
    Zeilenweises Bündeln reißt deshalb Blöcke auseinander; stattdessen bekommt
    jede Textzeile das Etikett, dessen Mitte ihr am nächsten liegt.
    """
    # Fußzeile ("10.08.2026 13:31 by GM") trägt nichts bei.
    boxen = [k for k in boxen if k[2] < hoehe * 0.95]
    if not boxen:
        return []

    links_bis = breite * LEFT_COLUMN
    etikett_max = breite * LABEL_MAX_WIDTH
    zeilenhoehe = sorted(k[2] - k[1] for k in boxen)[len(boxen) // 2] or hoehe * 0.02
    max_abstand = zeilenhoehe * 2.5

    etiketten = [
        k for k in boxen
        if k[0] < links_bis and k[3] <= etikett_max and LABEL_RE.match(k[4])
    ]
    etiketten.sort(key=_mitte)
    uebrig = [k for k in boxen if k not in etiketten]

    zugeordnet: dict[int, list] = {i: [] for i in range(len(etiketten))}
    allgemein: list = []
    for kasten in uebrig:
        if kasten[0] >= links_bis and etiketten:
            index = min(range(len(etiketten)),
                        key=lambda i: abs(_mitte(kasten) - _mitte(etiketten[i])))
            if abs(_mitte(kasten) - _mitte(etiketten[index])) <= max_abstand:
                zugeordnet[index].append(kasten)
                continue
        allgemein.append(kasten)

    meldungen: list[tuple[float, Notice]] = []

    for index, etikett in enumerate(etiketten):
        teile = sorted(zugeordnet[index], key=lambda k: (k[1], k[0]))
        if teile:
            meldungen.append((
                min(k[1] for k in teile),
                Notice(etikett[4].rstrip(":").strip(), " ".join(k[4] for k in teile), sheet_title),
            ))

    # Zeilen über die volle Breite: aufeinanderfolgende zu einem Absatz bündeln.
    allgemein.sort(key=lambda k: (k[1], k[0]))
    absatz: list = []
    for kasten in allgemein:
        if absatz and kasten[1] - max(k[2] for k in absatz) > zeilenhoehe * 0.9:
            meldungen.append((absatz[0][1], Notice("", " ".join(k[4] for k in absatz), sheet_title)))
            absatz = []
        absatz.append(kasten)
    if absatz:
        meldungen.append((absatz[0][1], Notice("", " ".join(k[4] for k in absatz), sheet_title)))

    meldungen.sort(key=lambda paar: paar[0])
    return [notice for _, notice in meldungen if notice.text]


DATUMSKOPF_RE = re.compile(
    r"^(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b.*\d{4}", re.IGNORECASE
)

MONATE = {
    "januar": 1, "februar": 2, "maerz": 3, "marz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}


def iso_date(kopfzeile: str) -> str:
    """"Dienstag, 11. August 2026" -> "2026-08-11". Leerer String, wenn unklar."""
    from datetime import date

    text = normalise(kopfzeile)
    treffer = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", text)
    if treffer and treffer.group(2) in MONATE:
        tag, monat, jahr = int(treffer.group(1)), MONATE[treffer.group(2)], int(treffer.group(3))
    else:
        treffer = re.search(r"(\d{1,2})\s+(\d{1,2})\s+(\d{4})", text)
        if not treffer:
            return ""
        tag, monat, jahr = (int(g) for g in treffer.groups())
    try:
        return date(jahr, monat, tag).isoformat()
    except ValueError:
        return ""


@dataclass(frozen=True)
class InfoConfig:
    """Was vom Infoblatt gemeldet wird."""

    enabled: bool = False
    for_me: tuple[str, ...] = ()
    include_general: bool = True
    ignore: tuple[str, ...] = ()


def parse(image: bytes, sheet_title: str = "") -> tuple[list[Notice], str]:
    """Liefert (Meldungen, Datum des Blatts)."""
    boxen, breite, hoehe = read_boxes(image)
    meldungen = group(boxen, breite, hoehe, sheet_title)

    datum = ""
    behalten: list[Notice] = []
    for notice in meldungen:
        if not datum and not notice.addressee and DATUMSKOPF_RE.match(notice.text.strip()):
            datum = notice.text.strip()
            continue
        behalten.append(notice)
    return behalten, datum


def relevant(meldungen: list[Notice], config: InfoConfig) -> list[Notice]:
    """Filtert auf das, was einen selbst angeht."""
    ignorieren = [normalise(t) for t in config.ignore if t.strip()]
    meine = [normalise(t) for t in config.for_me if t.strip()]

    treffer: list[Notice] = []
    for notice in meldungen:
        volltext = normalise(notice.summary())
        if any(begriff in volltext for begriff in ignorieren):
            continue
        if notice.addressee:
            empfaenger = normalise(notice.addressee)
            if any(begriff in empfaenger for begriff in meine):
                treffer.append(notice)
        elif config.include_general:
            treffer.append(notice)
    return treffer
