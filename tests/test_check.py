"""Durchlauf des check-Kommandos ohne Netz: Dedup, Gruppierung, Fehlerzähler."""

import pytest

from dsbwatch import cli, client
from dsbwatch.matching import load_config
from dsbwatch.state import State
from tests.test_parse import SYNTHETIC

BASE = "https://light.dsbcontrol.de/DSBlightWebsite/Data/schule/doc"
PLAN_URL = f"{BASE}/subst_001.htm?639219777200047905"
BILD_URL = f"{BASE}/aushang_000.png?639219777201620190"

# ConType 6 obwohl es HTML ist — genau so liefert es die Schule.
DOKUMENTE = [
    {
        "Title": "subst_001", "Date": "10.08.2026 11:23", "ConType": 2, "Detail": "",
        "Childs": [
            {"Title": "subst_001", "Date": "10.08.2026 11:23", "ConType": 6,
             "Detail": PLAN_URL, "Childs": []}
        ],
    }
]

# ConType 4 obwohl es ein PNG ist — ebenfalls Realität.
AUSHAENGE = [
    {"Title": "Informationen", "Date": "10.08.2026 13:31", "ConType": 4,
     "Detail": BILD_URL, "Childs": []}
]


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "ignore_case: false\ncourses:\n  - label: Mathe\n    match: ['ma102']\n",
        encoding="utf-8",
    )
    return load_config(path)


def _endpoints(dokumente=DOKUMENTE, aushaenge=()):
    def get_endpoint(token, endpoint):
        return list(dokumente) if endpoint == "dsbdocuments" else list(aushaenge)

    return get_endpoint


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(client, "get_token", lambda user, pw: "token-123")
    monkeypatch.setattr(client, "get_endpoint", _endpoints())
    monkeypatch.setattr(client, "fetch_bytes", lambda url: SYNTHETIC.encode("utf-8"))


# ------------------------------------------------------------------ Typ-Erkennung


def test_contype_wird_ignoriert_der_dateityp_entscheidet(offline):
    plans = client.get_plans("token-123")
    assert [p.kind for p in plans] == ["html"]
    assert plans[0].contype == 6, "ConType sagt Bild, die .htm-Endung sagt HTML"


def test_cache_buster_gehoert_nicht_zur_identitaet():
    a = client.PlanRef("t", "d", 6, f"{BASE}/subst_001.htm?111")
    b = client.PlanRef("t", "d", 6, f"{BASE}/subst_001.htm?222")
    assert a.key == b.key
    assert a.filename == "subst_001.htm"


def test_dokumente_aus_beiden_endpoints(monkeypatch):
    monkeypatch.setattr(client, "get_endpoint", _endpoints(aushaenge=AUSHAENGE))
    plans = client.get_plans("t")
    assert sorted(p.kind for p in plans) == ["html", "image"]
    assert {p.source for p in plans} == {"dsbdocuments", "dsbtimetables"}


def test_sniffing_wenn_die_url_nichts_verraet():
    assert client.sniff_kind(b"\x89PNG\r\n\x1a\n") == "image"
    assert client.sniff_kind(b"<html><table class='mon_list'>") == "html"
    assert client.kind_from_url(f"{BASE}/irgendwas?123") == "unknown"


# ------------------------------------------------------------------------ Ablauf


def test_erster_lauf_meldet_zweiter_schweigt(tmp_path, config, offline):
    state = State(tmp_path / "state.json")

    message, photos = cli._check(state, config, dry_run=False)
    assert message is not None
    assert "ma102" in message
    assert "MA102" not in message, "fremder LK darf nicht mitkommen"
    assert "11.8.2026 Dienstag" in message
    assert photos == []

    again, _ = cli._check(state, config, dry_run=False)
    assert again is None, "unveränderte Zeilen dürfen kein zweites Mal melden"


def test_dry_run_schreibt_keinen_state(tmp_path, config, offline):
    state = State(tmp_path / "state.json")
    cli._check(state, config, dry_run=True)
    assert state.data["seen"] == {}


def test_geaenderte_zeile_meldet_erneut(tmp_path, config, offline, monkeypatch):
    state = State(tmp_path / "state.json")
    cli._check(state, config, dry_run=False)

    geaendert = SYNTHETIC.replace('<td class="list">Ch2</td>', '<td class="list">C9</td>')
    monkeypatch.setattr(client, "fetch_bytes", lambda url: geaendert.encode("utf-8"))

    message, _ = cli._check(state, config, dry_run=False)
    assert message is not None and "C9" in message


def test_leerer_plan_gilt_als_fehler(tmp_path, config, monkeypatch):
    monkeypatch.setattr(client, "get_token", lambda user, pw: "token-123")
    monkeypatch.setattr(client, "get_endpoint", _endpoints())
    monkeypatch.setattr(client, "fetch_bytes", lambda url: b"<html><body>nix</body></html>")

    state = State(tmp_path / "state.json")
    with pytest.raises(client.DsbError, match="keine einzige Zeile"):
        cli._check(state, config, dry_run=False)


def test_token_landet_nicht_im_state(tmp_path, config, offline):
    """state.json wird ins Repo committet — dort darf kein Zugangsschlüssel liegen."""
    state = State(tmp_path / "state.json")
    cli._check(state, config, dry_run=False)
    state.save()

    gespeichert = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "token" not in gespeichert
    assert "token-123" not in gespeichert


def test_falsche_zugangsdaten_schlagen_durch(tmp_path, config, monkeypatch):
    def get_token(user, pw):
        raise client.AuthError("Benutzername oder Passwort falsch?")

    monkeypatch.setattr(client, "get_token", get_token)
    with pytest.raises(client.AuthError):
        cli._check(State(tmp_path / "state.json"), config, dry_run=False)


def test_fehlerzaehler_und_pruning(tmp_path):
    from datetime import date

    state = State(tmp_path / "state.json")
    assert state.record_failure("kaputt") == 1
    assert state.record_failure("kaputt") == 2
    state.record_success()
    assert state.data["fail_count"] == 0

    state.mark_seen("alt", "2026-08-01")
    state.mark_seen("neu", "2026-08-20")
    assert state.prune(today=date(2026, 8, 10)) == 1
    assert "neu" in state.data["seen"] and "alt" not in state.data["seen"]


def test_bild_meldet_nur_bei_aenderung_trotz_wechselndem_cache_buster(tmp_path, config, monkeypatch):
    monkeypatch.setattr(client, "get_token", lambda user, pw: "t")
    monkeypatch.setattr(client, "get_endpoint", _endpoints(dokumente=(), aushaenge=AUSHAENGE))
    monkeypatch.setattr(client, "fetch_bytes", lambda url: b"\x89PNG\r\n\x1a\nv1")

    state = State(tmp_path / "state.json")
    _, photos = cli._check(state, config, dry_run=False)
    assert len(photos) == 1

    # gleiche Datei, neuer Cache-Buster in der URL -> darf NICHT erneut melden
    neue_url = [{**AUSHAENGE[0], "Detail": f"{BASE}/aushang_000.png?999999"}]
    monkeypatch.setattr(client, "get_endpoint", _endpoints(dokumente=(), aushaenge=neue_url))
    _, photos = cli._check(state, config, dry_run=False)
    assert photos == []

    monkeypatch.setattr(client, "fetch_bytes", lambda url: b"\x89PNG\r\n\x1a\nv2")
    _, photos = cli._check(state, config, dry_run=False)
    assert len(photos) == 1


def test_bilder_koennen_abgeschaltet_werden(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "ignore_case: false\nnotify_on_images: false\n"
        "courses:\n  - label: Mathe\n    match: ['ma102']\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(client, "get_token", lambda user, pw: "t")
    monkeypatch.setattr(client, "get_endpoint", _endpoints(dokumente=(), aushaenge=AUSHAENGE))
    monkeypatch.setattr(client, "fetch_bytes", lambda url: b"\x89PNG\r\n\x1a\nv1")

    _, photos = cli._check(State(tmp_path / "state.json"), load_config(path), dry_run=False)
    assert photos == []


def test_ein_kaputtes_dokument_blockiert_die_kursmeldungen_nicht(tmp_path, config, monkeypatch):
    """DSB tauscht Dokumente aus, waehrend wir sie holen — dann laeuft die URL auf 404.
    Frueher riss das den ganzen Lauf mit, inklusive der Vertretungsmeldungen."""
    monkeypatch.setattr(client, "get_token", lambda user, pw: "t")
    monkeypatch.setattr(client, "get_endpoint", _endpoints(aushaenge=AUSHAENGE))

    def fetch(url):
        if url.endswith(".png") or "aushang" in url:
            raise client.DsbError("404 Client Error: Not Found")
        return SYNTHETIC.encode("utf-8")

    monkeypatch.setattr(client, "fetch_bytes", fetch)

    message, photos = cli._check(State(tmp_path / "state.json"), config, dry_run=False)
    assert message is not None and "ma102" in message, "Kursmeldung muss trotzdem rausgehen"
    assert photos == []


def test_faellt_alles_aus_gilt_der_lauf_als_gescheitert(tmp_path, config, monkeypatch):
    monkeypatch.setattr(client, "get_token", lambda user, pw: "t")
    monkeypatch.setattr(client, "get_endpoint", _endpoints(aushaenge=AUSHAENGE))
    monkeypatch.setattr(client, "fetch_bytes",
                        lambda url: (_ for _ in ()).throw(client.DsbError("404")))

    with pytest.raises(client.DsbError, match="Kein einziges"):
        cli._check(State(tmp_path / "state.json"), config, dry_run=False)
