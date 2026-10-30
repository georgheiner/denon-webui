"""Denon AVR Control Protocol: Statusmodell, Parser für Antwortzeilen und Befehlsaufbau.

Getestet gegen den DRA-800H (siehe CLAUDE.md). Jede Zeile vom Receiver endet mit
``\\r``; Antworten kommen teils gebündelt und auch ungefragt, deshalb wird jede Zeile
für sich ausgewertet, ohne Bezug auf die vorherige Anfrage.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Abfragen, die nach jedem Verbindungsaufbau den kompletten Status holen.
STATUS_QUERIES = ["PW?", "ZM?", "MV?", "MU?", "SI?", "MS?", "PSBAS ?", "PSTRE ?", "BL?", "MNMEN?", "SSFUN ?", "SSSOD ?", "SSINFFRM ?"]

# Standardnamen für Eingänge, die das Gerät per SSFUN nicht umbenannt meldet.
DEFAULT_INPUT_NAMES = {
    "DVD": "DVD",
    "BD": "Blu-ray",
    "TV": "TV Audio",
    "SAT/CBL": "CBL/SAT",
    "MPLAY": "Media Player",
    "GAME": "Game",
    "CD": "CD",
    "PHONO": "Phono",
    "TUNER": "Tuner",
    "NET": "HEOS Music",
    "AUX1": "AUX",
    "BT": "Bluetooth",
}

# Klangmodi eines Stereo-Receivers. Anzeigename → Befehlsparameter.
SOUND_MODES = {
    "STEREO": "Stereo",
    "DIRECT": "Direct",
    "PURE DIRECT": "Pure Direct",
}

VOLUME_MIN = 0.0
VOLUME_ABS_MAX = 98.0
TONE_MIN, TONE_MAX = -6, 6  # dB; Gerät nutzt 44..56 mit 50 = 0 dB
BALANCE_MAX = 12  # Schritte je Seite; Gerät meldet BL0, BLL3, BLR2

# Bildschirmmenü (nur über HDMI sichtbar) – Tasten wie auf der Fernbedienung
MENU_KEYS = {
    "up": "MNCUP",
    "down": "MNCDN",
    "left": "MNCLT",
    "right": "MNCRT",
    "enter": "MNENT",
    "back": "MNRTN",
    "option": "MNOPT",
    "info": "MNINF",
}


@dataclass
class NowPlaying:
    """Was über HEOS läuft (Eingang NET), z. B. vom iPhone per Bluetooth."""
    source: str | None = None  # "Bluetooth", "AirPlay" oder None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    state: str | None = None  # "play", "pause", "stop"


@dataclass
class TvState:
    """Fernseher über jointSPACE (Philips, Port 1925). Im Standby ist sein Netzwerk aus,
    deshalb heißt ``connected == False`` „aus oder nicht erreichbar“."""
    connected: bool = False
    model: str | None = None  # z. B. "37PFL9606K/02"
    source: str | None = None  # z. B. "hdmi3"
    sources: list[dict[str, str]] = field(default_factory=list)  # [{code, name}]
    ambilight_mode: str | None = None  # "internal" (folgt dem Bild), "manual", "expert"
    ambilight: str | None = None  # zuletzt gesetzt: "#rrggbb" oder "scene:<name>"


@dataclass
class ReceiverState:
    model: str | None = None  # aus der UPnP-Gerätebeschreibung, z. B. "DRA-800H"
    connected: bool = False
    power: bool | None = None
    zone_on: bool | None = None
    volume: float | None = None
    volume_max: float = VOLUME_ABS_MAX  # vom Gerät gemeldet (MVMAX)
    volume_limit: float = VOLUME_ABS_MAX  # eigene Obergrenze aus MAX_VOLUME
    mute: bool | None = None
    input: str | None = None
    sound_mode: str | None = None
    bass: int | None = None
    treble: int | None = None
    balance: int | None = None  # negativ = links, positiv = rechts
    now_playing: NowPlaying | None = None
    menu_open: bool | None = None
    firmware: str | None = None  # aus SSINFFRM, z. B. "5300-2172-7016-8080"
    heos_version: str | None = None
    update_available: bool | None = None  # laut HEOS player/check_update
    tv: TvState | None = None  # nur wenn TV_HOST gesetzt ist
    input_names: dict[str, str] = field(default_factory=dict)
    inputs_in_use: list[str] = field(default_factory=list)

    @property
    def effective_volume_max(self) -> float:
        return min(self.volume_max, self.volume_limit)

    @property
    def inputs(self) -> list[dict[str, str]]:
        codes = self.inputs_in_use or list(self.input_names)
        return [
            {"code": code, "name": self.input_names.get(code) or DEFAULT_INPUT_NAMES.get(code, code)}
            for code in codes
        ]

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "connected": self.connected,
            "power": self.power,
            "zone_on": self.zone_on,
            "volume": self.volume,
            "volume_max": self.effective_volume_max,
            "mute": self.mute,
            "input": self.input,
            "inputs": self.inputs,
            "sound_mode": self.sound_mode,
            "sound_modes": [{"code": c, "name": n} for c, n in SOUND_MODES.items()],
            "bass": self.bass,
            "treble": self.treble,
            "balance": self.balance,
            "balance_max": BALANCE_MAX,
            "now_playing": asdict(self.now_playing) if self.now_playing else None,
            "menu_open": self.menu_open,
            "firmware": self.firmware,
            "heos_version": self.heos_version,
            "update_available": self.update_available,
            "tv": asdict(self.tv) if self.tv else None,
        }


def parse_volume(raw: str) -> float | None:
    """``"10"`` → 10.0, ``"455"`` → 45.5, ``"05"`` → 5.0."""
    raw = raw.strip()
    if not raw.isdigit():
        return None
    if len(raw) == 3:
        return int(raw[:2]) + 0.5
    return float(int(raw))


def format_volume(value: float) -> str:
    """Gegenstück zu :func:`parse_volume`; rundet auf 0,5er-Schritte."""
    halves = round(value * 2)
    whole, half = divmod(halves, 2)
    return f"{whole:02d}5" if half else f"{whole:02d}"


def _parse_tone(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) - 50 if raw.isdigit() else None


def _parse_balance(raw: str) -> int | None:
    """``"0"`` → 0, ``"L3"`` → -3, ``"R2"`` → 2."""
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    if raw[:1] in ("L", "R") and raw[1:].isdigit():
        return -int(raw[1:]) if raw[0] == "L" else int(raw[1:])
    return None


def format_balance(value: int) -> str:
    return "0" if value == 0 else f"{'L' if value < 0 else 'R'}{abs(value)}"


def apply_line(state: ReceiverState, line: str) -> bool:
    """Wertet eine Antwortzeile aus. Gibt True zurück, wenn sich der Zustand geändert hat."""
    line = line.strip("\r\n")
    if not line:
        return False
    before = state.to_dict()

    if line.startswith("PW"):
        state.power = line[2:] == "ON"
    elif line.startswith("ZM"):
        state.zone_on = line[2:] == "ON"
    elif line.startswith("MVMAX"):
        vmax = parse_volume(line[5:])
        if vmax is not None:
            state.volume_max = vmax
    elif line.startswith("MV"):
        vol = parse_volume(line[2:])
        if vol is not None:
            state.volume = vol
    elif line.startswith("MU"):
        state.mute = line[2:] == "ON"
    elif line.startswith("MNMEN"):
        state.menu_open = line[5:].strip() == "ON"
    elif line.startswith("SSINFFRM"):
        rest = line[8:].strip()
        if rest and rest != "END":
            # "AVR 5300-2172-…" – Bauteilkennung vorne abtrennen
            state.firmware = rest.removeprefix("AVR").strip() or rest
    elif line.startswith("SSFUN"):
        rest = line[5:].strip()
        if rest and rest != "END":
            code, _, name = rest.partition(" ")
            state.input_names[code] = name.strip() or code
    elif line.startswith("SSSOD"):
        rest = line[5:].strip()
        if rest == "END":
            pass
        elif rest:
            code, _, flag = rest.rpartition(" ")
            if flag.strip() == "USE" and code not in state.inputs_in_use:
                state.inputs_in_use.append(code)
            elif flag.strip() == "DEL" and code in state.inputs_in_use:
                state.inputs_in_use.remove(code)
    elif line.startswith("SI"):
        state.input = line[2:].strip()
    elif line.startswith("MS"):
        state.sound_mode = line[2:].strip()
    elif line.startswith("PSBAS "):
        state.bass = _parse_tone(line[6:])
    elif line.startswith("PSTRE "):
        state.treble = _parse_tone(line[6:])
    elif line.startswith("BL"):
        bal = _parse_balance(line[2:])
        if bal is not None:
            state.balance = bal
    else:
        return False

    return state.to_dict() != before


class CommandError(ValueError):
    """Ungültiger Befehl oder Wert aus der Web-UI."""


def build_command(state: ReceiverState, cmd: str, value) -> tuple[str | None, str]:
    """Übersetzt einen UI-Befehl in (Coalesce-Schlüssel, Telnet-Befehl).

    Befehle mit gleichem Schlüssel ersetzen sich in der Sendewarteschlange, damit z. B.
    ein schnell bewegter Lautstärke-Slider nicht Dutzende Befehle aufstaut. Schlüssel
    ``None`` heißt: nie ersetzen.
    """
    if cmd == "power":
        return "PW", "PWON" if _as_bool(value) else "PWSTANDBY"
    if cmd == "mute":
        return "MU", "MUON" if _as_bool(value) else "MUOFF"
    if cmd == "volume":
        try:
            vol = float(value)
        except (TypeError, ValueError):
            raise CommandError("Lautstärke muss eine Zahl sein") from None
        vol = max(VOLUME_MIN, min(vol, state.effective_volume_max))
        return "MV", "MV" + format_volume(vol)
    if cmd == "volume_step":
        if value not in ("up", "down"):
            raise CommandError("volume_step erwartet 'up' oder 'down'")
        if value == "up" and (state.volume is None or state.volume + 0.5 > state.effective_volume_max):
            raise CommandError(f"Obergrenze {state.effective_volume_max:g} erreicht")
        # Einzelschritte dürfen sich nicht gegenseitig ersetzen → kein Schlüssel.
        return None, "MVUP" if value == "up" else "MVDOWN"
    if cmd == "input":
        known = {i["code"] for i in state.inputs}
        if value not in known:
            raise CommandError(f"Unbekannter Eingang: {value!r}")
        return "SI", f"SI{value}"
    if cmd == "sound_mode":
        if value not in SOUND_MODES:
            raise CommandError(f"Unbekannter Klangmodus: {value!r}")
        return "MS", f"MS{value}"
    if cmd in ("bass", "treble"):
        try:
            db = int(value)
        except (TypeError, ValueError):
            raise CommandError(f"{cmd} muss eine ganze Zahl sein") from None
        db = max(TONE_MIN, min(db, TONE_MAX))
        prefix = "PSBAS" if cmd == "bass" else "PSTRE"
        return prefix, f"{prefix} {db + 50}"
    if cmd == "menu":
        if value == "open":
            return "MNMEN", "MNMEN ON"
        if value == "close":
            return "MNMEN", "MNMEN OFF"
        if value not in MENU_KEYS:
            raise CommandError(f"Unbekannte Menütaste: {value!r}")
        return None, MENU_KEYS[value]  # jeder Tastendruck zählt, nie zusammenfassen
    if cmd == "balance":
        try:
            bal = int(value)
        except (TypeError, ValueError):
            raise CommandError("Balance muss eine ganze Zahl sein") from None
        bal = max(-BALANCE_MAX, min(bal, BALANCE_MAX))
        return "BL", "BL" + format_balance(bal)
    raise CommandError(f"Unbekannter Befehl: {cmd!r}")


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("on", "true", "1"):
        return True
    if isinstance(value, str) and value.lower() in ("off", "false", "0"):
        return False
    if isinstance(value, int):
        return bool(value)
    raise CommandError(f"Erwartet an/aus, bekommen: {value!r}")
