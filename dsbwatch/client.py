"""Zugriff auf die App-API von DSBmobile (mobileapi.dsbcontrol.de).

/authid liefert gegen Benutzer+Passwort ein Token (UUID, pro Konto konstant),
darüber liefern zwei Endpoints Inhalte:

    /dsbdocuments   -> hier liegt an dieser Schule der Untis-Vertretungsplan
                       (subst_001.htm, subst_002.htm, ...)
    /dsbtimetables  -> hier liegen die Aushänge/Infoblätter als PNG

Das ConType-Feld ist unbrauchbar: die Schule deklariert das HTML als 6 ("Bild")
und das PNG als 4 ("HTML"). Der Typ wird deshalb aus der URL bzw. aus den ersten
Bytes bestimmt, nicht aus ConType.

Achtung bei URLs: an jeder Detail-URL hängt ein Cache-Buster (?63921977...), der
sich bei *jedem* Abruf ändert. Als Identität zählt deshalb nur der Teil vor dem
Fragezeichen — siehe PlanRef.key.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests

BASE_URL = "https://mobileapi.dsbcontrol.de"
BUNDLE_ID = "de.heinekingmedia.dsbmobile"
APP_VERSION = "36"
OS_VERSION = "30"
USER_AGENT = "DSBmobile/36 (Android 11)"

ENDPOINTS = ("dsbdocuments", "dsbtimetables")

TIMEOUT = 20
RETRIES = 3

HTML_EXTS = (".htm", ".html")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")

KIND_HTML = "html"
KIND_IMAGE = "image"
KIND_UNKNOWN = "unknown"


class DsbError(RuntimeError):
    """Oberklasse für alle Fehler beim Reden mit DSBmobile."""


class AuthError(DsbError):
    """Zugangsdaten wurden abgelehnt oder das Token ist nicht mehr gültig."""


@dataclass(frozen=True)
class PlanRef:
    """Verweis auf ein einzelnes Dokument aus der Item-Liste."""

    title: str
    date: str
    contype: int
    url: str
    kind: str = KIND_UNKNOWN
    source: str = ""

    @property
    def key(self) -> str:
        """URL ohne Cache-Buster — das ist die stabile Identität des Dokuments."""
        return self.url.split("?")[0]

    @property
    def filename(self) -> str:
        return self.key.rsplit("/", 1)[-1]


def _request(url: str, params: dict[str, str] | None = None) -> requests.Response:
    """GET mit Timeout und Backoff. Wirft DsbError, wenn alle Versuche scheitern."""
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < RETRIES - 1:
                time.sleep(2**attempt)
    raise DsbError(f"Anfrage an {url} fehlgeschlagen: {last_error}") from last_error


def get_token(user: str, password: str) -> str:
    """Holt ein Auth-Token. Leere Antwort bedeutet: Zugangsdaten stimmen nicht."""
    if not user or not password:
        raise AuthError("DSB_USER oder DSB_PASSWORD ist nicht gesetzt (siehe .env.example).")

    response = _request(
        f"{BASE_URL}/authid",
        {
            "bundleid": BUNDLE_ID,
            "appversion": APP_VERSION,
            "osversion": OS_VERSION,
            "pushid": "",
            "user": user,
            "password": password,
        },
    )
    token = response.text.strip().strip('"').strip()
    if not token or token.startswith("{"):
        raise AuthError("DSBmobile hat kein Token geliefert — Benutzername oder Passwort falsch?")
    return token


def get_endpoint(token: str, endpoint: str) -> list[dict[str, Any]]:
    """Rohe Item-Liste eines Endpoints."""
    response = _request(f"{BASE_URL}/{endpoint}", {"authid": token})
    try:
        data = response.json()
    except ValueError as exc:
        raise DsbError(f"Unerwartete Antwort von /{endpoint}: {response.text[:200]!r}") from exc

    # Bei ungültigem Token antwortet die API mit einem Fehlerobjekt statt einer Liste.
    if isinstance(data, dict):
        raise AuthError(f"DSBmobile meldet: {data.get('Message', data)}")
    if not isinstance(data, list):
        raise DsbError(f"Unerwartetes Antwortformat von /{endpoint}: {type(data).__name__}")
    return data


def kind_from_url(url: str) -> str:
    path = url.split("?")[0].lower()
    if path.endswith(HTML_EXTS):
        return KIND_HTML
    if path.endswith(IMAGE_EXTS):
        return KIND_IMAGE
    return KIND_UNKNOWN


def sniff_kind(data: bytes) -> str:
    """Notnagel, wenn die URL keine Endung hat: an den ersten Bytes erkennen."""
    if data[:8].startswith((b"\x89PNG", b"GIF8", b"\xff\xd8")) or data[:4] == b"RIFF":
        return KIND_IMAGE
    head = data[:2048].lower()
    if b"<html" in head or b"mon_list" in head or b"<!doctype html" in head:
        return KIND_HTML
    return KIND_UNKNOWN


def iter_plans(items: list[dict[str, Any]], source: str = "") -> Iterator[PlanRef]:
    """Klopft den verschachtelten Item-Baum zu einer flachen Liste von Dokumenten flach."""

    def walk(nodes: Any) -> Iterator[PlanRef]:
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            yield from walk(node.get("Childs"))

            detail = (node.get("Detail") or "").strip()
            if not detail.lower().startswith(("http://", "https://")):
                continue
            try:
                contype = int(node.get("ConType") or 0)
            except (TypeError, ValueError):
                contype = 0
            yield PlanRef(
                title=(node.get("Title") or "").strip(),
                date=(node.get("Date") or "").strip(),
                contype=contype,
                url=detail,
                kind=kind_from_url(detail),
                source=source,
            )

    yield from walk(items)


def get_plans(token: str) -> list[PlanRef]:
    """Alle Dokumente aus allen Endpoints, dedupliziert über PlanRef.key."""
    plans: list[PlanRef] = []
    seen: set[str] = set()
    errors: list[str] = []

    for endpoint in ENDPOINTS:
        try:
            items = get_endpoint(token, endpoint)
        except AuthError:
            raise
        except DsbError as exc:
            errors.append(f"{endpoint}: {exc}")
            continue
        for plan in iter_plans(items, source=endpoint):
            if plan.key in seen:
                continue
            seen.add(plan.key)
            plans.append(plan)

    if errors and not plans:
        raise DsbError("Kein Endpoint erreichbar — " + " | ".join(errors))
    return plans


def fetch_bytes(url: str) -> bytes:
    """Lädt eine Detail-URL (HTML-Seite oder Bild) roh herunter."""
    return _request(url).content
