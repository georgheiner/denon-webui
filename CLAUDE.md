# Denon Web-UI

## Ziel
Eigene Web-Oberfläche zur Steuerung eines Denon AV-Receivers, weil die Bedienung über das Gerätedisplay sehr umständlich ist. Kein Jailbreak nötig: Steuerung über die offiziellen Netzwerkschnittstellen des Receivers.

## Gerät
- Modell: **Denon DRA-800H** (Stereo-Netzwerkreceiver mit HEOS, Firmware vom 18.02.2026)
- IP: **192.168.0.162** (feste IP im Router vergeben), LAN 100 Mbit/s
- Offene Ports: 23 (Telnet), 80, 443, 8080, 10443, 1255 (HEOS), 60006 (UPnP-Beschreibung)

### Verbindungstest (30.10.2026, Gerät im Standby)
| Befehl | Antwort |
|---|---|
| `PW?` | `PWSTANDBY` |
| `ZM?` | `ZMOFF` |
| `MV?` | `MV10`, `MVMAX 60`, dazu ungefragt `SYSDA PCM` |
| `MU?` | `MUOFF` |
| `SI?` | `SINET`, `SVOFF` |
| `MS?` | `MSSTEREO`, `PSDRC OFF`, `PSLFE 00`, `PSBAS 50`, `PSTRE 50`, `OPINFINS …`, `OPINFASP …`, `SYSMI Stereo` |
| `Z2?` | `Z2OFF`, `Z2NET`, `Z249`, `SVOFF` (Zone 2 vorhanden) |
| `SSFUN ?` | Eingangsnamen: DVD, Blu-ray, TV Audio, CBL/SAT, Amazon FireTV (MPLAY), Game, CD, Phono, `SSFUN END` |
| `SSSOD ?` | genutzte Eingänge: DVD, BD, TV, SAT/CBL, MPLAY, GAME, CD, PHONO, TUNER, NET |
| `PSBAS ?` / `PSTRE ?` | `PSBAS 50` / `PSTRE 50` (Bass/Höhen, 50 = neutral) |

Erkenntnisse:
- Lautstärke-Skala 0–98, Maximum am Gerät auf 60 begrenzt (`MVMAX 60`) → Slider daran ausrichten.
- Antworten kommen teils gebündelt und mit Zusatzzeilen → Parser zeilenweise, unabhängig von der Anfrage.
- Erste Verbindung nach längerer Ruhe antwortete nicht sofort → beim Verbindungsaufbau kurz warten bzw. Abfrage wiederholen.
- Eingangsnamen per `SSFUN ?` holen statt hart codieren; nur Eingänge mit `USE` aus `SSSOD ?` anzeigen.
- Stereo-Gerät: Klangmodi sind begrenzt (z. B. Stereo, Direct, Pure Direct), dafür Bass/Höhen interessant.
- Telnet-Zugriff vom Mac aus der Claude-Sandbox heraus scheitert ("No route to host"), Ping geht → Netzwerkbefehle außerhalb der Sandbox ausführen.

## Schnittstellen des Receivers
- **Telnet/TCP Port 23** – offizielles Denon AVR Control Protocol, Textbefehle mit `\r` abgeschlossen, z. B. `PWON`, `PWSTANDBY`, `MV50`, `MVUP`, `MUON`, `SIGAME`, `MSDOLBY DIGITAL`. Abfragen mit `?` (z. B. `PW?`, `MV?`, `SI?`, `MS?`). Der Receiver sendet Statusänderungen ungefragt → für Live-Status nutzen.
  - Achtung: viele Modelle erlauben nur **eine** gleichzeitige Telnet-Verbindung → genau eine Verbindung im Backend halten, Browser nur per WebSocket anbinden. Reconnect-Logik einbauen.
  - Befehle nicht zu schnell hintereinander senden (ca. 50–100 ms Abstand).
- **HTTP Port 80/8080** – inoffiziell (`/goform/...`, `AppCommand.xml`), optional z. B. für Eingangsnamen.
- **HEOS CLI Port 1255** – nur bei HEOS-Modellen, für Streaming (später).
- Referenz: Python-Lib `denonavr` (Basis der Home-Assistant-Integration).

## Architektur
- Backend: **Python 3.12 + FastAPI + Uvicorn**, eigener asyncio-Telnet-Client (`asyncio.open_connection`): hält genau eine Verbindung, Befehls-Queue mit Abstand, Reconnect, parst Statusmeldungen in ein Zustandsobjekt, verteilt Änderungen per WebSocket. Liefert das statische Frontend aus.
- Frontend: reines HTML/CSS/JavaScript ohne Build-Schritt, mobilfreundliche Web-UI, Live-Status über WebSocket.
- Konfiguration (IP, Port) über Umgebungsvariablen.
- Auslieferung als Container mit `docker-compose.yml`, Multi-Arch (arm64 + amd64).
- Keine Auto-Discovery (funktioniert aus Docker auf dem Mac nicht zuverlässig) → IP fest konfiguriert.

## Projektstruktur und Betrieb
- `backend/app/protocol.py` – Zustandsmodell, Parser für Antwortzeilen, UI-Befehl → Telnet-Befehl (mit Validierung).
- `backend/app/client.py` – eine Telnet-Verbindung, Sendewarteschlange (100 ms Abstand, gleichartige Befehle wie absolute Lautstärke ersetzen sich), Reconnect mit Backoff, Heartbeat `PW?` alle 30 s. Die Pause geht erst nach einer echten Antwort wieder auf 1 s zurück (auch in `heos.py`): Am 30.10.2026 hing die Netzwerksteuerung des DRA-800H (nahm Telnet/HEOS an und schloss sofort, AirPlay-Steuerung vom iPhone ging auch nicht) – nur Netzstecker ziehen half. Vorher gab es viele Testverbindungen zu HEOS und Pause/Play über HEOS während AirPlay; ob das die Ursache war, ist unklar → Tests nur über die eine Verbindung des Backends.
- `backend/app/main.py` – FastAPI: `GET /api/state`, `POST /api/command`, `GET /api/health`, WebSocket `/ws`, liefert `frontend/` aus.
- `backend/app/heos.py` – HEOS CLI (Port 1255). Steuert die Wiedergabe (`play_state` play/pause/stop → `player/set_play_state`, `next`/`previous` → `player/play_next`/`play_previous`; wie die Transporttasten ⏮ ⏯ ⏭ der Fernbedienung RC-1235). Am Gerät bestätigt (iPhone per Bluetooth): funktioniert; die Hörbuch-App macht aus Weiter/Zurück ein Spulen, deshalb zeigt die UI ◀◀ ▶▶ („Zurückspulen“/„Vorspulen“) statt ⏮ ⏭. Liest: `register_for_change_events`, `get_players` (Player mit passender IP), `get_now_playing_media`, `get_play_state`; Ereignisse lösen Neuabfrage aus. Liefert `now_playing` {source, title, artist, album, state, image_url, position, duration}. Beim DRA-800H gemessen: iPhone per Bluetooth → `mid: "Bluetooth"`, `song`/`artist`/`album` gefüllt, `image_url` leer. **Fortschritt:** `event/player_now_playing_progress` (`cur_pos`/`duration` in ms). Bei **Bluetooth unbrauchbar**: `cur_pos` zählt seit Beginn der Bluetooth-Wiedergabe und läuft über Titelwechsel weiter (169 s direkt nach einem Wechsel), `duration` ist 0 oder 2000 → bei Bluetooth ignoriert; das Backend zählt ab einem miterlebten Titelwechsel selbst ab 0 (bei Pause angehalten), beim ersten Titel nach dem Verbinden ist die Position unbekannt (UI: „–:––“). Bei HEOS-Diensten (nicht Bluetooth) gilt die Meldung; Dauer nur, wenn ≥ Position. `position` wird beim Senden auf jetzt hochgerechnet, der Browser zählt dazwischen weiter. **Cover und Dauer** kommen per Bluetooth nie über HEOS (`image_url` leer) → iTunes-Suche (`COVER_LOOKUP=off` schaltet ab; schickt Interpret/Titel/Album an Apple), nur bei eindeutig passendem Namen (Klammerzusätze egal), Dauer nur bei passendem Titel: 1. Podcast, der wie der Interpret heißt (+ Folge mit gleichem Titel für die Dauer), 2. Songsuche „Interpret Titel“/„Interpret Album“ (gleicher Titel und Album, sonst gleiches Album, sonst gleicher Titel – der erste Treffer ist oft ein anderer Song, z. B. Haftbefehl „069“), 3. Künstler → Alben → gleiches Album → dessen Titel (die Suche „SSIO BWL“ liefert 0 Treffer, das Album schon; die Albumsuche der API findet manche Alben nicht). **AirPlay** (gemessen 30.10.2026): HEOS meldet `sid` 1024 mit einer Zahl als `mid` (z. B. `"5277906"`) → als „AirPlay“ angezeigt. Direkt nach dem Umschalten und nach jedem Neuverbinden des Backends sind alle Titelfelder leer (`mid: "1"`), Titel/Interpret/Album kommen erst mit dem nächsten Titel; UI zeigt solange „AirPlay – Titelinfos folgen mit dem nächsten Titel“ ohne Zeitleiste. Das eigene Cover des Denon (`http://<ip>:8015/local-server/albumart/<hash>`) ist nur halb gespeichert (untere Hälfte grau, auch bei wiederholtem Abruf) → verworfen, nachgeschlagen wie bei Bluetooth. Fortschritt: `cur_pos` ist nicht die Position im Titel, `duration=0` → Meldungen ohne gültige Dauer werden ignoriert. Pause/Play über HEOS wirken (am iPhone bestätigt), `get_play_state` meldet danach aber weiter „play“ → der Zustand wird aus der Bestätigung von `player/set_play_state` übernommen. Telnet `NSE` und HTTP-Statusseiten liefern beim DRA-800H keine Titeldaten. HEOS-CLI nimmt nicht beliebig viele Verbindungen an: Testabfragen von außen liefen zeitweise ins Leere → aus dem Container (`docker exec`) abfragen. **Wiedergabe-Karte = Mini-Player wie auf dem iOS-Sperrbildschirm** (Nutzerwunsch): kleines Cover links (pausiert etwas kleiner), Titel/Interpret, Zeile „gelaufen | Balken | −Rest“ (Balken immer sichtbar), darunter ◀◀ ⏯ ▶▶ ohne Kreise und rechts das Symbol der Zuspielung; Karte in der Durchschnittsfarbe des Covers getönt (Canvas, Cover-Server erlauben CORS).
- `backend/app/tv.py` – Philips-Fernseher über **jointSPACE** (HTTP-JSON, Port 1925, API v1, ohne Anmeldung), nur aktiv mit `TV_HOST`. Gerät: **37PFL9606K/02** (Chassis Q551.2E LA, Baujahr 2011, Software Q5551-0.14.99.0 = vermutlich letzte), IP **192.168.0.210** (WLAN, MAC 5c:33:8e:89:25:e7; per Kabel war es .153). Fragt alle 5 s `system`, `sources`, `sources/current` ab (meldet nichts selbst). Befehle `tv_key` (`standby/up/down/left/right/ok/back/home/options/ambilight` → `POST /1/input/key`) und `tv_source` (Code aus `sources` → `POST /1/sources/current`). Zustand `tv` {connected, model, source, sources[{code,name}]} bzw. `null` ohne `TV_HOST`. Im Standby ist das Netzwerk des Fernsehers aus → `connected: false` = „aus oder nicht erreichbar“; Einschalten über Netzwerk unmöglich, deshalb braucht Ausschalten in der UI einen zweiten Tipp. Am Gerät getestet: `VolumeDown`/`VolumeUp`, Eingang setzen. Lesbar außerdem `audio/volume` (0–60). **Ambilight** (`tv_ambilight`): `follow` → Modus `internal`; `#rrggbb` → Modus `manual` + `POST ambilight/cached` {r,g,b} (eine Farbe für alle LEDs; Helligkeit rechnet die UI in die Farbe ein); `scene:tiefsee|lava|natur|leselicht` → Verlauf je LED (Topologie links 4, oben 7, rechts 4, unten 0; angenommen im Uhrzeigersinn ab unten links – **Richtung am Gerät noch nicht angeschaut**). Beim Ziehen im Farbfeld sendet das Backend nur den neuesten Wert. Die eingebauten Atmosphären des Fernsehers (Natur, Lava, Tiefsee) und Lounge Light im Standby sind über jointSPACE nicht erreichbar → nachgebaut. `ambilight/processed` ist die Bildanalyse, nicht die Ausgabe; den An/Aus-Zustand der Taste `AmbilightOnOff` meldet der Fernseher nicht. Am Gerät getestet (30.10.2026): Eingang wechseln, Home/Pfeile/Zurück/Optionen, Ambilight-Taste, Farbe/Szene/Bild folgen – alles angenommen; was am Bildschirm/an den LEDs zu sehen war, noch nicht vom Nutzer bestätigt. Ausschalten nicht getestet. Eine schlanke Fremd-Firmware gibt es für das Modell nicht.
- Modellname kommt aus der UPnP-Beschreibung (Port 60006, `modelName` ohne „Denon “) → `model` im Zustand, Titel der Seite.
- `frontend/` – `index.html`, `styles.css` (Design-Tokens, Hell/Dunkel, reduzierte Bewegung/Transparenz, erhöhter Kontrast), `app.js` (WebSocket, optimistische Werte mit Abgleich, Bedienung). Kein Build-Schritt; Änderungen brauchen `docker compose up -d --build`, weil das Frontend ins Image kopiert wird.
- Lautstärke-Slider: relative Geste wie im iOS-Kontrollzentrum – Antippen allein ändert nichts (Schutz vor versehentlich laut), Ziehen folgt 1:1, weicher Widerstand an den Enden. ± mit Halten wiederholt. Tastatur: Pfeile ±0,5, Bild↑/↓ ±5.
- Fluchtlinien: Logo (viewBox auf die Buchstaben zugeschnitten), Modellname, Status, Laufband-Textanfang und Kartenkante stehen auf einer Linie; Power-Knopf rechts bündig mit den Karten – auch über 64rem Breite. Laufband per Web Animations API (CSS-Keyframes mit `var()` liefen in Safari/iOS nicht). Server schickt `Cache-Control: no-cache` für das Frontend, damit Safari nach Updates neu lädt.
- Gestaltungsregeln u. a. nach https://github.com/emilkowalski/skills/blob/main/skills/apple-design/SKILL.md (Reaktion beim Drücken, 1:1-Tracking, Materialien, Barrierefreiheit).
- Konfiguration in `.env` (Vorlage `.env.example`): `DENON_HOST`, `DENON_PORT`, `TV_HOST` (optional), `COVER_LOOKUP` (Standard `on`), `MAX_VOLUME` (Standard 45), `WEB_PORT`, `LOG_LEVEL`.
- Lautstärke-Obergrenze: `volume_max` im Zustand = min(Gerät `MVMAX`, `MAX_VOLUME`). Absolute Werte werden darauf begrenzt, `volume_step up` an der Grenze abgelehnt. Gilt nur für die Web-UI, nicht für Fernbedienung/Drehknopf/HEOS-App.
- Starten: `docker compose up -d --build` → http://localhost:8000
- Tests (im Container, da System-Python 3.9): `docker run --rm -v "$PWD/backend:/src" -w /src python:3.12-slim sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"`

### WebSocket-/REST-Protokoll
- Server → Browser: `{"type":"state","state":{...}}` beim Verbinden und bei jeder Änderung (immer der komplette Zustand).
- Browser → Server: `{"cmd":..., "value":...}`; Fehler kommen als `{"type":"error","message":...}`.
- Befehle: `tv_key`, `tv_source` (siehe `tv.py`), `power` (bool), `mute` (bool), `volume` (Zahl, 0,5er-Schritte, begrenzt auf `volume_max`), `volume_step` (`"up"`/`"down"`), `input` (Code aus `inputs`), `sound_mode` (Code aus `sound_modes`), `bass`/`treble` (−6…+6 dB), `balance` (−12 links … +12 rechts).
- Zustand: `connected`, `power`, `zone_on`, `volume`, `volume_max`, `mute`, `input`, `inputs[{code,name}]`, `sound_mode`, `sound_modes[{code,name}]`, `bass`, `treble`, `balance`, `balance_max`.
- `.layout` hat `grid-template-columns: minmax(0, 1fr)`: sonst macht ein langer Titel ohne Umbruch die Karte breiter als den Bildschirm und das iPhone zoomt die Seite heraus.
- Kartenreihenfolge: Lautstärke, Wiedergabe (nur sichtbar bei Eingang NET mit HEOS-Titel), Klang, Quelle (Nutzer ist meist auf HEOS), Fernseher (unabhängig vom Receiver-Power), Einstellungen.
- Einstellungen: Fernsteuerung des Bildschirmmenüs (Bild nur über HDMI am Fernseher, wird nicht übertragen). Befehl `menu` mit `open`/`close` (`MNMEN ON/OFF`, Zustand `menu_open` aus `MNMEN?`) und Tasten `up/down/left/right/enter/back/option/info` (`MNCUP/MNCDN/MNCLT/MNCRT/MNENT/MNRTN/MNOPT/MNINF`, jeder Druck einzeln). **Menütasten noch nicht am Gerät ausprobiert** (nur `MNMEN?` → `MNMEN OFF` gelesen). Infos: Firmware aus `SSINFFRM ?` (`SSINFFRMAVR 5300-2172-7016-8080`), HEOS-Version aus `get_players`, Update-Status aus HEOS `player/check_update` (`update_none`/`update_exist`, beim Verbinden und alle 6 h). Bluetooth-Koppeln hat keinen eigenen Befehl gefunden → über das Menü. Später optional: HDMI-Capture-Stick, um das Menübild in die Web-UI zu streamen. Desktop: zwei Spalten per CSS `columns`.
- Kopf: Denon-Logo (inline SVG aus Wikimedia Commons, Public Domain, `currentColor`), darunter Modellname, Status „Eingeschaltet · <Quelle>“ (bei NET die HEOS-Zuspielung, z. B. Bluetooth), am unteren Rand der Kopfleiste ein Laufband „Interpret – Titel“ über die volle Breite wie ein Nachrichtenticker: läuft immer (~45 px/s, nahtlos), bei Pause angehalten und grau. Läuft auf Nutzerwunsch auch bei „Bewegung reduzieren“, dort halb so schnell.
- Mute-Knopf: feste Beschriftung „Stumm“, grau = aus, rot = aktiv (Nutzerwunsch; wechselnde Beschriftung „Ton an“/„Stumm“ war verwirrend).

### Schaltbefehle am DRA-800H getestet (30.10.2026, über das Backend, Lautstärke nie über 10)
- Funktionieren alle: `PWON`/`PWSTANDBY`, `MUON`, `MV` absolut (inkl. 0,5er-Schritte), `MVUP`/`MVDOWN` (je 0,5), `MSDIRECT`, `MSPURE DIRECT`, `MSSTEREO`, `PSBAS nn`, `PSTRE nn`.
- **Im Standby werden Mute und Lautstärke ignoriert** (kein Fehler, keine Antwort) → UI soll diese Bedienelemente bei `power == false` deaktivieren.
- Nach `PWON` dauert es ca. 3 s, bis Befehle angenommen werden.
- **Balance:** Abfrage `BL?`, Meldung/Befehl `BL0` (Mitte), `BLL3` (3 Schritte links), `BLR2` (rechts); `BLR12` angenommen, Bereich vermutlich ±12. Wird ungefragt gemeldet, wenn am Gerät verstellt. `PSBAL`/`CV`-Kanalpegel gibt es nicht (CV meldet nur `CVSW`). `SSLEV ?` liefert Kanalpegel aus dem Setup (FL/FR … je 50).
- **Jede Lautstärkeänderung hebt Mute auf** (Gerätverhalten) → UI muss den Mute-Status nach Lautstärkeänderung aus dem Live-Status übernehmen, nicht optimistisch beibehalten.

## Design (Apple-Stil, angelehnt an die Human Interface Guidelines)
- Reine Web-UI im Browser (keine App, keine Installation), aufgerufen über die Adresse des Backends. Optisch im Apple-Stil, nutzbar auf iPhone, iPad und Desktop.
- Schrift: System-Font-Stack (`-apple-system, BlinkMacSystemFont, "SF Pro", system-ui, sans-serif`), klare Hierarchie (Large Title, Headline, Body, Caption).
- Farben: iOS-System-Farben als CSS-Variablen (Akzent `systemBlue`, Grau-Abstufungen für Hintergründe), automatisch Hell/Dunkel über `prefers-color-scheme`.
- Layout: gruppierte Karten mit großen Eckradien (ca. 12–16 px), großzügige Abstände, Safe Areas beachten (`env(safe-area-inset-*)`).
- Bedienung: Touch-Flächen mindestens 44×44 pt, iOS-typische Schalter (Power, Mute), großer Lautstärke-Slider, Segmented Controls bzw. Listen für Eingänge und Klangmodi.
- Material: Leisten mit Blur (`backdrop-filter`), dezente Schatten, sanfte Animationen; `prefers-reduced-motion` respektieren.
- Feedback: Bedienelemente reagieren sofort (optimistisch) und gleichen sich mit dem Live-Status ab; Verbindungsstatus sichtbar.
- Keine Apple-Logos, -Icons oder -Markennamen verwenden, nur der Stil.

## Umgebung
- Entwicklung: Mac (Apple Silicon, 192.168.0.4, gleiches Netz), Claude Code lokal. System-Python ist nur 3.9 → im Container Python 3.12 verwenden.
- Docker: **OrbStack 2.2.5** (Docker 29.8.2, buildx für arm64 + amd64). OrbStack braucht die macOS-Freigabe „Lokales Netzwerk“, sonst erreichen Container den Receiver nicht (ist erteilt, Telnet aus Container getestet). `docker` liegt in `/usr/local/bin`; Docker-/Netzwerkbefehle außerhalb der Claude-Sandbox ausführen.
- Ziel später: Raspberry Pi (arm64, Linux), 24/7-Betrieb.

## Umfang Version 1
Power, Lautstärke (Slider + ±), Mute, Eingangswahl, Klangmodus, Bass/Höhen – jeweils mit Live-Status.

## Später
Zonen (Zone 2/3), HEOS-Streaming/Cover-Art, Setup-Menüs.

## Vorgehen
1. ~~Zuerst Verbindung direkt testen, Antworten des konkreten Modells prüfen und hier dokumentieren.~~ Erledigt, siehe oben.
2. ~~Backend~~ und ~~Container~~ stehen (Tests grün, live gegen den Receiver verbunden). Schaltbefehle am Gerät geprüft. Frontend v1 steht (Darstellung auf Mobil/Desktop, Hell/Dunkel geprüft; Bedienung über die UI selbst noch nicht am Gerät getestet). Als Nächstes: Bedienung am iPhone ausprobieren, dann Raspberry Pi.
3. Bei Live-Tests am Gerät: Lautstärke nie über den aktuellen Wert anheben, danach den Ausgangszustand wiederherstellen (Nutzerwunsch, damit es nicht laut wird).
