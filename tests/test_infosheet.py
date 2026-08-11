"""Zerlegung und Filterung des Infoblatts.

Getestet wird ab den erkannten Textkästen — ohne OCR, damit die Tests schnell
bleiben und nicht am Modell hängen. Die Koordinaten stammen aus einem echten
Blatt (794x1123 Pixel); entscheidend ist die Eigenheit, dass ein
Empfänger-Etikett vertikal *mittig* zu seinem mehrzeiligen Text steht und
deshalb unter seiner ersten Textzeile liegen kann.
"""

import pytest

from dsbwatch.infosheet import InfoConfig, Notice, group, iso_date, normalise, relevant

BREITE, HOEHE = 794, 1123

# (x0, y0, y1, breite, text)
BOXEN = [
    (95, 99, 121, 300, "Dienstag, 11. August 2026"),
    (90, 268, 286, 46, "Alle:"),
    (272, 261, 279, 330, "Die Pavillonhofe sind heute in den"),
    (272, 287, 305, 240, "Pausen nicht zu betreten"),
    (93, 327, 349, 92, "KuK Jg. 5:"),
    (273, 313, 335, 250, "14:30 Uhr Pad. Konf. Jg. 5"),
    (92, 365, 387, 55, "Jg. 6:"),
    (274, 369, 391, 300, "Schwimmen startet 2. Schulwoche"),
    # HAB: das Etikett liegt UNTER seiner ersten Textzeile
    (273, 399, 421, 330, "Treffen aller Aufgabenbetreuer mit LI"),
    (92, 422, 444, 48, "HAB:"),
    (273, 425, 447, 330, "zur Orga und Info Do. 13. Aug. in der"),
    (94, 477, 499, 420, "GroBe Halle 1. - 4. Stunde nicht verfugbar"),
    (267, 614, 640, 200, "Sextaneraufnahme"),
    (584, 1062, 1080, 200, "10.08.2026 13:31by GM"),
]


@pytest.fixture
def meldungen():
    return group(BOXEN, BREITE, HOEHE, "Infoblatt")


def _finde(meldungen, empfaenger):
    return next(m for m in meldungen if m.addressee == empfaenger)


def test_etikett_unter_der_ersten_textzeile_wird_richtig_zugeordnet(meldungen):
    hab = _finde(meldungen, "HAB")
    assert hab.text.startswith("Treffen aller Aufgabenbetreuer")
    assert "zur Orga und Info" in hab.text
    assert "Treffen" not in _finde(meldungen, "Jg. 6").text


def test_mehrzeilige_bloecke_bleiben_zusammen(meldungen):
    assert _finde(meldungen, "Alle").text == (
        "Die Pavillonhofe sind heute in den Pausen nicht zu betreten"
    )


def test_volle_breite_hat_keinen_empfaenger(meldungen):
    allgemein = [m for m in meldungen if not m.addressee]
    assert any("Halle" in m.text for m in allgemein)


def test_fusszeile_faellt_weg(meldungen):
    assert not any("by GM" in m.text for m in meldungen)


def test_normalisierung_entschaerft_ocr_eigenheiten():
    """Das Modell wirft Umlautpunkte weg und liest ß als B — beide Seiten müssen
    dieselbe Verstümmelung durchlaufen, sonst greift kein Suchbegriff."""
    assert normalise("Jg. 13") == normalise("Jg 13")
    assert normalise("Päd. Konf.") == normalise("Pad Konf")
    assert normalise("Pavillonhöfe") == normalise("Pavillonhofe")
    assert normalise("Große") == normalise("GroBe")
    assert normalise("Jg. 13") != normalise("Jg. 12")


def test_filter_nimmt_nur_die_eigenen_empfaenger(meldungen):
    config = InfoConfig(
        enabled=True,
        for_me=("Alle", "Jg. 13"),
        include_general=False,
        ignore=("Sextaner", "Bereitschaft"),
    )
    treffer = relevant(meldungen, config)
    assert [m.addressee for m in treffer] == ["Alle"]


def test_allgemeine_zeilen_lassen_sich_zuschalten(meldungen):
    config = InfoConfig(enabled=True, for_me=("Alle",), include_general=True,
                        ignore=("Sextaner",))
    texte = [m.text for m in relevant(meldungen, config)]
    assert any("Halle" in t for t in texte)
    assert not any("Sextaneraufnahme" in t for t in texte), "ignore muss greifen"


def test_jahrgang_13_wird_erkannt_auch_ohne_punkt():
    config = InfoConfig(enabled=True, for_me=("Jg. 13",), include_general=False)
    treffer = relevant([Notice("Jg 13", "Gottesdienst 8 Uhr Pauluskirche")], config)
    assert len(treffer) == 1


def test_fremder_jahrgang_rutscht_nicht_durch():
    config = InfoConfig(enabled=True, for_me=("Jg. 13",), include_general=False)
    assert relevant([Notice("Jg. 6", "Schwimmen startet")], config) == []


def test_fingerprint_haengt_am_inhalt_nicht_an_der_schreibweise():
    a = Notice("Alle", "Die Pavillonhöfe sind gesperrt")
    b = Notice("Alle", "Die Pavillonhofe sind gesperrt")  # so liest es das OCR
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != Notice("Alle", "Die Pavillonhöfe sind offen").fingerprint()


def test_datum_aus_der_kopfzeile():
    assert iso_date("Dienstag, 11. August 2026") == "2026-08-11"
    assert iso_date("Mittwoch, 3. März 2027") == "2027-03-03"
    assert iso_date("kein Datum") == ""
