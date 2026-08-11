"""Parser- und Filter-Tests.

SYNTHETIC deckt Spaltennamen ab, die andere Schulen benutzen; das Fixture
``subst_real.htm`` ist eine echte Seite der Edith-Stein-Schule. Ändert Untis das
Exportformat, schlagen diese Tests an — statt dass stillschweigend keine
Meldungen mehr kommen.
"""

from pathlib import Path

import pytest

from dsbwatch.matching import load_config, select
from dsbwatch.parse import Entry, decode, parse_page, parse_plan

REAL = Path(__file__).parent / "fixtures" / "subst_real.htm"

SYNTHETIC = """<html><head>
<meta http-equiv="content-type" content="text/html; charset=utf-8"></head><body>
<center>
<div class="mon_title">11.8.2026 Dienstag</div>
<table class="mon_list">
<tr class='list'>
  <th class="list">Klasse(n)</th><th class="list">Stunde</th><th class="list">(Fach)</th>
  <th class="list">Vertreter</th><th class="list">Raum</th><th class="list">Art</th>
  <th class="list">Vertretungs-Text</th>
</tr>
<tr class='list odd'>
  <td class="list">13</td><td class="list">7</td><td class="list">ma102</td>
  <td class="list"><strike>SH</strike>?MI</td><td class="list">Ch2</td>
  <td class="list">Vertretung</td><td class="list">Aufgaben liegen vor</td>
</tr>
<tr class='list even'>
  <td class="list">13</td><td class="list">3 - 4</td><td class="list">MA102</td>
  <td class="list">MI</td><td class="list">C205</td><td class="list">Entfall</td><td class="list">&nbsp;</td>
</tr>
<tr class='list odd'>
  <td class="list">13</td><td class="list">1 - 2</td><td class="list">rk107</td>
  <td class="list">RM</td><td class="list">B103</td><td class="list">Raum&auml;nderung</td><td class="list">&nbsp;</td>
</tr>
</table>
</center></body></html>"""

FRAMESET = """<html><frameset><frame name="hide" src="frames/navbar.htm">
<frame name="content" src="subst_001.htm"></frameset></html>"""


@pytest.fixture
def entries():
    return parse_page(SYNTHETIC, plan_title="Vertretungsplan 13")


@pytest.fixture
def real():
    return parse_page(decode(REAL.read_bytes()), plan_title="subst_001")


# --------------------------------------------------------------- synthetische Seite


def test_spalten_werden_ueber_die_kopfzeile_zugeordnet(entries):
    first = entries[0]
    assert (first.klasse, first.stunde, first.fach) == ("13", "7", "ma102")
    assert (first.raum, first.art) == ("Ch2", "Vertretung")
    assert first.text == "Aufgaben liegen vor"
    assert not first.extra, "keine Spalte darf unzugeordnet bleiben"


def test_ersetzung_wird_als_pfeil_dargestellt(entries):
    assert entries[0].lehrer == "SH → MI"


def test_datum_wird_aus_dem_titel_gelesen(entries):
    assert entries[0].plan_date == "11.8.2026 Dienstag"
    assert entries[0].iso_date == "2026-08-11"


def test_umlaute_und_nbsp(entries):
    assert entries[2].art == "Raumänderung"
    assert entries[2].text == ""


def test_fingerprint_aendert_sich_bei_jeder_feldaenderung():
    base = Entry(plan_date="11.8.2026", klasse="13", fach="ma102", raum="Ch2")
    assert base.fingerprint() != Entry(
        plan_date="11.8.2026", klasse="13", fach="ma102", raum="C9"
    ).fingerprint()
    assert base.fingerprint() == Entry(
        plan_date="11.8.2026", klasse="13", fach="ma102", raum="Ch2"
    ).fingerprint()


def test_kursfilter_unterscheidet_gross_und_kleinschreibung(tmp_path, entries):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "ignore_case: false\ncourses:\n"
        "  - label: Mathe\n    match: ['ma102']\n"
        "  - label: Religion\n    match: ['rk107']\n",
        encoding="utf-8",
    )
    labels = [label for label, _ in select(entries, load_config(config_file))]
    assert labels == ["Mathe", "Religion"], "MA102 ist ein fremder LK und darf nicht matchen"


def test_fehlende_config_meldet_alles(tmp_path, entries):
    assert len(select(entries, load_config(tmp_path / "gibtsnicht.yaml"))) == len(entries)


def test_frameset_wird_verfolgt():
    pages = {
        "https://light.dsbcontrol.de/x/V_DC_001.html": FRAMESET.encode(),
        "https://light.dsbcontrol.de/x/subst_001.htm": SYNTHETIC.encode(),
        "https://light.dsbcontrol.de/x/frames/navbar.htm": b"<html><body>nix</body></html>",
    }
    found = parse_plan(lambda url: pages[url], "https://light.dsbcontrol.de/x/V_DC_001.html")
    assert len(found) == 3


def test_decode_faellt_auf_cp1252_zurueck():
    assert "Raumänderung" in decode("Raumänderung".encode("cp1252"))


# ------------------------------------------------------------------- echte Schulseite


def test_echte_seite_wird_vollstaendig_geparst(real):
    assert len(real) == 6
    assert all(e.plan_date == "11.8.2026 Dienstag" for e in real)
    assert not any(e.extra for e in real), "alle acht Spalten müssen zugeordnet sein"


def test_echte_seite_iso_8859_1_umlaute(real):
    assert any("Schwimmen" in e.text for e in real)
    assert not any("�" in e.searchable for e in real), "kein kaputtes Encoding"


def test_raumaenderung_zeigt_alt_und_neu(real):
    sport = [e for e in real if e.fach == "SP202" and e.stunde == "3"]
    assert len(sport) == 1
    assert sport[0].summary() == "13 · 3. Std · SP202 — Raum-Vtr. · Raum SpGH, C012 → Sp-Spl"


def test_entfall_wiederholt_sich_nicht_in_der_zusammenfassung(real):
    ge107 = [e for e in real if e.fach == "ge107" and e.stunde == "1"][0]
    assert ge107.lehrer == "Entfall", "Untis schreibt 'Entfall' in die Lehrer-Spalte"
    assert ge107.summary() == "13 · 1. Std · ge107 — Entfall", "kein doppeltes Entfall, kein Raum '---'"


def test_echte_kurse_werden_gefunden(real):
    config = load_config(Path(__file__).parent.parent / "config.yaml")
    treffer = {entry.fach for _, entry in select(real, config)}
    assert "SP202" in treffer, "Sport-LK Raumänderung muss gemeldet werden"
    assert "MU102" not in treffer, "fremde Kurse dürfen nicht durchrutschen"
