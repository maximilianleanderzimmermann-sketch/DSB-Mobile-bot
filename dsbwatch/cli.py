"""Kommandozeile: dump | check | test-notify."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from dsbwatch import client, infosheet, notify, parse
from dsbwatch.matching import Config, load_config, select
from dsbwatch.state import State

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"
DEFAULT_STATE = ROOT / "state" / "state.json"
DUMP_DIR = ROOT / "tmp"

FAILURES_BEFORE_ALERT = 3


# --------------------------------------------------------------------------- Hilfen


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _telegram(required: bool = True) -> notify.Telegram | None:
    token, chat_id = _env("TELEGRAM_TOKEN"), _env("TELEGRAM_CHAT_ID")
    fehlend = [n for n, v in (("TELEGRAM_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id)) if not v]
    if fehlend:
        if required:
            raise notify.NotifyError(
                f"{' und '.join(fehlend)} ist leer. Lokal: .env — auf GitHub: "
                "Settings → Secrets and variables → Actions."
            )
        return None
    return notify.Telegram(token, chat_id)


def _print_env_report() -> None:
    """Zeigt im CI-Log, welche Variablen ankommen — ohne die Werte zu verraten."""
    print("\nZustand der Umgebungsvariablen:", file=sys.stderr)
    for name in ("DSB_USER", "DSB_PASSWORD", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        value = _env(name)
        status = f"gesetzt ({len(value)} Zeichen)" if value else "LEER"
        print(f"  {name:<18} {status}", file=sys.stderr)


def _fetch_plans() -> list[client.PlanRef]:
    """Token holen und damit die Dokumentliste. Das Token wird nirgends abgelegt."""
    token = client.get_token(_env("DSB_USER"), _env("DSB_PASSWORD"))
    return client.get_plans(token)


def _cached_fetch():
    """Jede URL nur einmal pro Lauf laden."""
    cache: dict[str, bytes] = {}

    def fetch(url: str) -> bytes:
        if url not in cache:
            cache[url] = client.fetch_bytes(url)
        return cache[url]

    return fetch


def _collect(
    plans: list[client.PlanRef],
) -> tuple[list[parse.Entry], list[tuple[client.PlanRef, bytes]], int]:
    """Parst alle HTML-Dokumente. Gibt (Einträge, Bilder, Anzahl HTML-Dokumente) zurück."""
    fetch = _cached_fetch()
    entries: list[parse.Entry] = []
    images: list[tuple[client.PlanRef, bytes]] = []
    html_count = 0
    kaputt = 0

    for plan in plans:
        # Ein einzelnes Dokument darf den Lauf nicht mitreissen: DSB tauscht
        # Dokumente aus, waehrend wir sie holen, dann laeuft die URL auf 404.
        # Frueher blockierte das auch die Vertretungsmeldungen.
        try:
            data = fetch(plan.url)
            # ConType lügt an dieser Schule — im Zweifel an den ersten Bytes erkennen.
            kind = plan.kind if plan.kind != client.KIND_UNKNOWN else client.sniff_kind(data)
            if kind == client.KIND_IMAGE:
                images.append((plan, data))
            elif kind == client.KIND_HTML:
                html_count += 1
                entries.extend(parse.parse_plan(fetch, plan.url, plan_title=plan.title))
        except client.DsbError as exc:
            kaputt += 1
            print(f"{plan.filename} uebersprungen: {exc}", file=sys.stderr)

    if kaputt and not entries and not images:
        raise client.DsbError(f"Kein einziges von {kaputt} Dokument(en) war ladbar.")

    return entries, images, html_count


def _group(hits: list[tuple[str, parse.Entry]]) -> dict[str, list[tuple[str, str]]]:
    groups: dict[str, list[tuple[str, str]]] = {}
    for label, entry in hits:
        groups.setdefault(entry.plan_date, []).append((label, entry.summary()))
    return groups


# --------------------------------------------------------------------------- dump


def cmd_dump(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    plans = _fetch_plans()

    print(f"\n{len(plans)} Dokument(e) gefunden:\n")
    for plan in plans:
        print(f"  [{plan.kind:<7}] {plan.title or '(ohne Titel)'}  ({plan.date})  via /{plan.source}")
        print(f"            {plan.filename}")

    entries, images, html_count = _collect(plans)

    if args.raw:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        fetch = _cached_fetch()
        for index, plan in enumerate(plans):
            target = DUMP_DIR / f"{index:02d}_{plan.filename}"
            target.write_bytes(fetch(plan.url))
            print(f"Roh -> {target}")

    if images:
        print(f"\n{len(images)} Dokument(e) als Bild — dort ist kein Zeilen-Filter möglich:")
        for plan, data in images:
            print(f"  • {plan.title} ({len(data)} Bytes)")

    if not entries:
        print(f"\nKeine Zeilen geparst ({html_count} HTML-Dokument(e)).")
        return 0

    print(f"\n{len(entries)} Zeilen geparst:\n")
    header = ("Datum", "Klasse", "Std", "Fach", "Raum", "Art", "Lehrer", "Text")
    widths = (18, 10, 7, 12, 22, 16, 14, 30)
    print("  " + "  ".join(h.ljust(w) for h, w in zip(header, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for entry in entries:
        row = (entry.plan_date, entry.klasse, entry.stunde, entry.fach,
               entry.raum, entry.art, entry.lehrer, entry.text)
        print("  " + "  ".join(str(v)[:w].ljust(w) for v, w in zip(row, widths)))
        if entry.extra:
            print(f"    ↳ nicht zugeordnete Spalten: {entry.extra}")

    hits = select(entries, config)
    print(f"\nDavon betreffen {len(hits)} Zeile(n) deine Kurse aus {args.config}:")
    if config.notify_all:
        print("  (config.yaml ist leer/fehlt — es würde ALLES gemeldet)")
    for label, entry in hits:
        print(f"  • [{label or '—'}] {entry.plan_date}: {entry.summary()}")
    return 0


# --------------------------------------------------------------------------- check


def _check(
    state: State, config: Config, dry_run: bool
) -> tuple[str | None, list[tuple[bytes, str]]]:
    """Kern des Laufs. Gibt (Nachrichtentext oder None, [(Bild, Bildunterschrift)]) zurück."""
    plans = _fetch_plans()
    entries, images, html_count = _collect(plans)

    if html_count and not entries:
        raise client.DsbError(
            f"{html_count} HTML-Dokument(e) geladen, aber keine einzige Zeile geparst — "
            "hat Untis das Format geändert?"
        )

    today = date.today()
    fresh: list[tuple[str, parse.Entry]] = []
    for label, entry in select(entries, config):
        if state.is_new(entry.fingerprint()):
            fresh.append((label, entry))
            if not dry_run:
                state.mark_seen(entry.fingerprint(), entry.iso_date, today)

    photos: list[tuple[bytes, str]] = []
    info_neu: list[infosheet.Notice] = []
    info_datum = ""

    for plan, data in images:
        if config.info.enabled:
            try:
                meldungen, datum = infosheet.parse(data, plan.title)
            except Exception as exc:  # OCR darf den Vertretungsplan nicht mitreißen
                print(f"Infoblatt konnte nicht gelesen werden: {exc}", file=sys.stderr)
                continue
            info_datum = datum or info_datum
            for notice in infosheet.relevant(meldungen, config.info):
                if state.is_new(notice.fingerprint()):
                    info_neu.append(notice)
                    if not dry_run:
                        state.mark_seen(notice.fingerprint(), infosheet.iso_date(datum), today)
        elif config.notify_on_images:
            digest = hashlib.sha1(data).hexdigest()
            # plan.key statt plan.url: an der URL hängt ein wechselnder Cache-Buster.
            if state.image_changed(plan.key, digest):
                photos.append((data, f"📎 {plan.title or 'Aushang'} — neu oder geändert"))
                if not dry_run:
                    state.mark_image(plan.key, digest)

    teile = []
    if fresh:
        teile.append(notify.format_changes(_group(fresh)))
    if info_neu:
        teile.append(notify.format_notices(info_datum, info_neu))
    return ("\n\n".join(teile) or None), photos


def cmd_check(args: argparse.Namespace) -> int:
    state = State(args.state)
    config = load_config(args.config)
    telegram = _telegram(required=not args.dry_run)

    try:
        message, photos = _check(state, config, args.dry_run)
    except (client.DsbError, OSError) as exc:
        count = state.record_failure(str(exc))
        print(f"Fehlgeschlagen ({count}. Mal in Folge): {exc}", file=sys.stderr)
        if count >= FAILURES_BEFORE_ALERT and not state.already_alerted and not args.dry_run:
            try:
                telegram = telegram or _telegram()
                telegram.send(notify.format_failure(str(exc), count))
                state.already_alerted = True
            except notify.NotifyError as notify_exc:
                print(f"Fehler-Alarm konnte nicht gesendet werden: {notify_exc}", file=sys.stderr)
        state.save()
        return 0  # kein roter Workflow-Lauf — gewarnt wird über Telegram

    if args.dry_run:
        print(message or "Keine neuen Änderungen.")
        for _, caption in photos:
            print(f"[Bild] {caption}")
        print("\n(--dry-run: nichts gesendet, State nicht geschrieben)")
        return 0

    if message:
        telegram.send(message)
        print(message)
    for image, caption in photos:
        telegram.send_photo(image, caption)
    if not message and not photos:
        print("Keine neuen Änderungen.")

    state.record_success()
    removed = state.prune()
    if removed:
        print(f"{removed} veraltete Einträge aus dem State entfernt.")
    state.save()
    return 0


# --------------------------------------------------------------------------- test-notify


def cmd_chat_id(args: argparse.Namespace) -> int:
    token = _env("TELEGRAM_TOKEN")
    me = notify.get_me(token)
    username = me.get("username", "?")
    print(f"Token gehört zu: @{username} ({me.get('first_name', '')})\n")

    chats = notify.find_chats(token)
    if not chats:
        print("Telegram kennt noch keinen Chat für diesen Bot.\n")
        print(f"  1. In Telegram nach  @{username}  suchen — nicht @BotFather anschreiben,")
        print("     sondern deinen eigenen Bot.")
        print("  2. Den Chat öffnen und auf START tippen (oder /start senden).")
        print("  3. Dieses Kommando erneut aufrufen.\n")

        webhook = notify.get_webhook_info(token).get("url")
        if webhook:
            print(f"Achtung: Es ist ein Webhook gesetzt ({webhook}). Solange der aktiv ist,")
            print("liefert getUpdates nichts. Entfernen mit:")
            print(f"  https://api.telegram.org/bot<TOKEN>/deleteWebhook")
        return 1
    print("Gefundene Chats — die Zahl links gehört in TELEGRAM_CHAT_ID:\n")
    for chat_id, name in chats:
        print(f"  {chat_id}   {name}")
    return 0


def cmd_test_notify(args: argparse.Namespace) -> int:
    _telegram().send(
        "✅ <b>DSB-Watcher</b>\nTestnachricht — die Verbindung steht.\n"
        "Ab jetzt kommen hier Änderungen zu deinen Kursen an."
    )
    print("Testnachricht gesendet.")
    return 0


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(prog="dsbwatch", description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Pfad zur config.yaml")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="Pfad zur state.json")
    sub = parser.add_subparsers(dest="command", required=True)

    dump = sub.add_parser("dump", help="Plan holen, parsen und anzeigen")
    dump.add_argument("--raw", action="store_true", help="Rohdateien nach tmp/ schreiben")
    dump.set_defaults(func=cmd_dump)

    check = sub.add_parser("check", help="Auf Änderungen prüfen und ggf. benachrichtigen")
    check.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts senden/speichern")
    check.set_defaults(func=cmd_check)

    chat = sub.add_parser("chat-id", help="Chat-ID für TELEGRAM_CHAT_ID ermitteln")
    chat.set_defaults(func=cmd_chat_id)

    test = sub.add_parser("test-notify", help="Testnachricht an Telegram schicken")
    test.set_defaults(func=cmd_test_notify)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (client.DsbError, notify.NotifyError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        _print_env_report()
        return 1
