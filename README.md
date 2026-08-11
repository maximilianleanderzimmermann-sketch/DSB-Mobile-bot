# DSB-Watcher

Überwacht den DSBmobile-Vertretungsplan und schickt eine Telegram-Nachricht, sobald sich bei
**deinen** Kursen etwas ändert — jede Änderung genau einmal. Läuft als GitHub-Actions-Cron,
also auch morgens um sechs, ohne dass der Rechner an ist.

## Einrichtung

### 1. Zugangsdaten lokal hinterlegen

```bash
cp .env.example .env
```

Dann `.env` öffnen und `DSB_USER` / `DSB_PASSWORD` eintragen. Die Datei steht in `.gitignore`
und wird nie eingecheckt.

### 2. Abhängigkeiten

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### 3. Schauen, was die Schule ausliefert

```bash
.venv/bin/python -m dsbwatch dump
```

Zeigt alle gefundenen Pläne, alle geparsten Zeilen und darunter, welche davon zu
`config.yaml` passen. `--raw` legt zusätzlich das Roh-JSON und das Roh-HTML in `tmp/` ab.

### 4. Telegram-Bot

1. In Telegram [@BotFather](https://t.me/BotFather) anschreiben → `/newbot` → Namen und einen
   auf `bot` endenden Benutzernamen vergeben → Token kopieren (Form: `8123456789:AAH…`).
2. Token in `.env` als `TELEGRAM_TOKEN` eintragen.
3. Deinen neuen Bot in Telegram suchen und ihm **einmal selbst schreiben** (`/start`) —
   vorher darf er dir nicht antworten und kennt deine Chat-ID nicht.
4. Chat-ID ermitteln und in `.env` als `TELEGRAM_CHAT_ID` eintragen:

```bash
.venv/bin/python -m dsbwatch chat-id
```

```bash
.venv/bin/python -m dsbwatch test-notify
```

### 5. In die Cloud

Repo (**privat**) auf GitHub anlegen, pushen, dann unter
*Settings → Secrets and variables → Actions* vier Secrets anlegen:
`DSB_USER`, `DSB_PASSWORD`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.

Der Workflow läuft danach werktags alle 15 Minuten zwischen ca. 6 und 19 Uhr und lässt sich
unter *Actions → Vertretungsplan prüfen → Run workflow* jederzeit von Hand starten.

## Kommandos

| Kommando | Zweck |
|---|---|
| `python -m dsbwatch dump` | Plan holen, parsen, anzeigen — zum Einrichten und Debuggen |
| `python -m dsbwatch dump --raw` | zusätzlich Roh-JSON/-HTML nach `tmp/` schreiben |
| `python -m dsbwatch check` | prüfen und bei Änderungen benachrichtigen (das macht der Cron) |
| `python -m dsbwatch check --dry-run` | zeigt die Nachricht, die rausginge — sendet nichts, speichert nichts |
| `python -m dsbwatch chat-id` | listet die Chats des Bots — liefert den Wert für `TELEGRAM_CHAT_ID` |
| `python -m dsbwatch test-notify` | Testnachricht an Telegram |

## Kurse anpassen

In `config.yaml`. Eine Zeile des Vertretungsplans löst eine Meldung aus, wenn **alle**
Begriffe unter `match` darin vorkommen — gesucht wird über Klasse, Fach, Lehrer, Raum und
Bemerkung gleichzeitig.

> **Groß-/Kleinschreibung ist relevant.** Im Jahrgangsraster gibt es Paare wie `ma102`
> (Grundkurs) und `MA102` (Leistungskurs). Deshalb steht `ignore_case: false`. Wer das auf
> `true` stellt, bekommt fremde Kurse mitgemeldet.

## Wie es funktioniert

1. `client.py` holt über `mobileapi.dsbcontrol.de/authid` ein Token und fragt damit **zwei**
   Endpoints ab:
   - `/dsbdocuments` → hier liegt der Untis-Vertretungsplan (`subst_001.htm` …)
   - `/dsbtimetables` → hier liegt das tägliche Infoblatt als PNG
2. `parse.py` lädt die HTML-Seite, folgt notfalls dem Frameset, und liest `table.mon_list`.
   Die Spaltenzuordnung kommt aus der **Kopfzeile**, nicht aus festen Indizes — jede Schule
   sortiert anders. Ersetzungen (`<s>alt</s>?neu`) werden zu „alt → neu".
3. `matching.py` filtert auf die eigenen Kurse.
4. `state.py` merkt sich einen Fingerabdruck je Zeile. Ändert sich ein Feld, entsteht ein
   neuer Fingerabdruck ⇒ es wird gemeldet. Unveränderte Zeilen bleiben still.
5. `notify.py` schickt eine gebündelte Nachricht pro Lauf.

## Das Infoblatt

Das tägliche Infoblatt kommt nur als Bild — keine Textebene, kein PDF dahinter. `infosheet.py`
liest es deshalb per Texterkennung (`rapidocr-onnxruntime`, reines pip-Paket, Modelle liegen
im Wheel) und macht sich die zweispaltige Struktur zunutze:

```
Alle:        Die Pavillonhöfe sind heute in den Pausen nicht zu betreten
KuK Jg. 5:   14:30 Uhr Päd. Konf. Jg. 5
Große Halle 1. - 4. Stunde nicht verfügbar          <- volle Breite, ohne Empfänger
```

Links steht, wen es betrifft. Gemeldet wird ein Block, wenn sein Empfänger unter
`info_sheet.for_me` steht. Zeilen über die volle Breite haben keinen Empfänger und kommen nur
mit, wenn `include_general: true` gesetzt ist.

Zwei Details, an denen es sonst scheitert:

- **Das Empfänger-Etikett steht vertikal mittig zu seinem mehrzeiligen Text** und liegt damit
  oft *unter* der ersten Textzeile. Zeilenweises Bündeln zerreißt Blöcke; jede Textzeile
  bekommt deshalb das Etikett, dessen Mitte ihr am nächsten liegt.
- **Das OCR-Modell kennt keine Umlaute** — aus „Pavillonhöfe" wird „Pavillonhofe", aus „Große"
  wird „GroBe". `normalise()` bildet genau diese Verstümmelung nach und wird auf OCR-Text
  *und* Suchbegriffe angewandt. Würde man stattdessen sauber nach `oe`/`ue` normalisieren,
  fände kein Begriff mit Umlaut je seinen erkannten Gegenpart.

Verglichen wird **Meldung für Meldung**, nicht die Prüfsumme des Bildes. Das Blatt wird im Lauf
des Tages mehrfach neu erzeugt; über die Prüfsumme gäbe das eine Nachricht nach der anderen.

### Zwei Fallen, die hier schon eingebaut sind

- **`ConType` lügt.** Die Doku sagt 4 = HTML, 6 = Bild. Diese Schule liefert das HTML als 6
  und das PNG als 4. Der Typ wird deshalb aus der Dateiendung bzw. den ersten Bytes bestimmt.
- **An jeder Detail-URL hängt ein Cache-Buster** (`?63921977…`), der sich bei *jedem* Abruf
  ändert. Als Identität eines Dokuments zählt nur der Teil vor dem `?` (`PlanRef.key`) —
  sonst gälte jedes Bild bei jedem Lauf als „geändert".

### Was *nicht* im State steht

Das Auth-Token wird **nicht** gespeichert. `state/state.json` landet im Repo, und das Token
ist ein Zugangsschlüssel; ein `/authid`-Request pro Lauf ist billiger als ein Secret, das
dauerhaft in der Versionsgeschichte liegt. Im State stehen nur Hashes, Bild-Prüfsummen und
der Fehlerzähler.

## Wenn etwas klemmt

Scheitert der Job dreimal in Folge (falsches Passwort, geändertes Untis-Format, DSB offline),
kommt **eine** Warnung per Telegram — danach Ruhe, bis es wieder läuft. So bleibt es nicht
unbemerkt, wenn stillschweigend keine Meldungen mehr kommen.

`state/state.json` wird vom Workflow nach jedem Lauf zurück ins Repo committet — Actions-Runner
haben kein bleibendes Dateisystem. Der Commit hält das Repo nebenbei aktiv; sonst schaltet
GitHub geplante Workflows nach 60 Tagen Inaktivität ab.

## Tests

```bash
.venv/bin/pip install pytest && .venv/bin/python -m pytest tests -q
```

Der Parser läuft gegen ein Untis-Fixture. Ändert die Schule das Exportformat, schlagen die
Tests an, statt dass stillschweigend keine Meldungen mehr kommen.
