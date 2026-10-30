import pytest

from app.protocol import CommandError, ReceiverState, apply_line, build_command, format_volume, parse_volume

# Echte Antworten des DRA-800H aus dem Verbindungstest (siehe CLAUDE.md).
DRA800H_STATUS = [
    "PWSTANDBY", "ZMOFF", "MV10", "MVMAX 60", "SYSDA PCM                  ", "MUOFF", "SINET", "SVOFF",
    "MSSTEREO", "PSDRC OFF", "PSLFE 00", "PSBAS 50", "PSTRE 50", "OPINFINS 11111111111111000000",
    "SYSMI Stereo", "Z2OFF", "Z2NET", "Z249",
    "SSFUNDVD DVD              ", "SSFUNBD Blu-ray          ", "SSFUNTV TV Audio         ",
    "SSFUNSAT/CBL CBL/SAT          ", "SSFUNMPLAY Amazon FireTV    ", "SSFUNGAME Game             ",
    "SSFUNCD CD               ", "SSFUNPHONO Phono            ", "SSFUN END",
    "SSSODDVD USE", "SSSODBD USE", "SSSODTV USE", "SSSODSAT/CBL USE", "SSSODMPLAY USE",
    "SSSODGAME USE", "SSSODCD USE", "SSSODPHONO USE", "SSSODTUNER USE", "SSSODNET USE", "SSSOD END",
]


def loaded_state() -> ReceiverState:
    state = ReceiverState()
    for line in DRA800H_STATUS:
        apply_line(state, line + "\r")
    return state


def test_dra800h_status_parsed():
    s = loaded_state().to_dict()
    assert s["power"] is False
    assert s["zone_on"] is False
    assert s["volume"] == 10.0
    assert s["volume_max"] == 60.0
    assert s["mute"] is False
    assert s["input"] == "NET"
    assert s["sound_mode"] == "STEREO"
    assert s["bass"] == 0 and s["treble"] == 0


def test_inputs_use_device_names_and_order():
    inputs = loaded_state().inputs
    assert [i["code"] for i in inputs] == [
        "DVD", "BD", "TV", "SAT/CBL", "MPLAY", "GAME", "CD", "PHONO", "TUNER", "NET",
    ]
    names = {i["code"]: i["name"] for i in inputs}
    assert names["MPLAY"] == "Amazon FireTV"
    assert names["SAT/CBL"] == "CBL/SAT"
    assert names["TUNER"] == "Tuner"  # nicht per SSFUN gemeldet → Standardname


def test_deleted_input_disappears():
    state = loaded_state()
    apply_line(state, "SSSODCD DEL")
    assert "CD" not in [i["code"] for i in state.inputs]


@pytest.mark.parametrize("raw,value", [("10", 10.0), ("455", 45.5), ("05", 5.0), ("00", 0.0), ("98", 98.0)])
def test_volume_roundtrip(raw, value):
    assert parse_volume(raw) == value
    assert format_volume(value) == raw


def test_format_volume_rounds_to_half_steps():
    assert format_volume(32.2) == "32"
    assert format_volume(32.3) == "325"
    assert format_volume(32.8) == "33"


def test_apply_line_reports_changes_only():
    state = loaded_state()
    assert apply_line(state, "MV10\r") is False
    assert apply_line(state, "MV11\r") is True
    assert apply_line(state, "SYSDA PCM\r") is False  # unbekannt → ignoriert


def test_build_commands():
    state = loaded_state()
    assert build_command(state, "power", True) == ("PW", "PWON")
    assert build_command(state, "power", False) == ("PW", "PWSTANDBY")
    assert build_command(state, "mute", "on") == ("MU", "MUON")
    assert build_command(state, "volume", 45.5) == ("MV", "MV455")
    assert build_command(state, "volume", 80) == ("MV", "MV60")  # auf MVMAX begrenzt
    assert build_command(state, "volume", -3) == ("MV", "MV00")
    assert build_command(state, "volume_step", "up") == (None, "MVUP")
    assert build_command(state, "input", "SAT/CBL") == ("SI", "SISAT/CBL")
    assert build_command(state, "sound_mode", "PURE DIRECT") == ("MS", "MSPURE DIRECT")
    assert build_command(state, "bass", 3) == ("PSBAS", "PSBAS 53")
    assert build_command(state, "treble", -10) == ("PSTRE", "PSTRE 44")


def test_volume_limit_caps_absolute_and_steps():
    state = loaded_state()
    state.volume_limit = 45
    assert state.to_dict()["volume_max"] == 45  # min(Gerät 60, Grenze 45)
    assert build_command(state, "volume", 98) == ("MV", "MV45")
    assert build_command(state, "volume", 44.5) == ("MV", "MV445")

    state.volume = 44.5
    assert build_command(state, "volume_step", "up") == (None, "MVUP")
    state.volume = 45
    with pytest.raises(CommandError, match="Obergrenze"):
        build_command(state, "volume_step", "up")
    assert build_command(state, "volume_step", "down") == (None, "MVDOWN")


def test_volume_step_up_rejected_while_volume_unknown():
    with pytest.raises(CommandError):
        build_command(ReceiverState(), "volume_step", "up")


def test_device_max_wins_when_lower_than_limit():
    state = loaded_state()  # Gerät meldet MVMAX 60
    state.volume_limit = 80
    assert state.to_dict()["volume_max"] == 60


def test_model_name_from_upnp(monkeypatch):
    import io
    from app import client

    xml = b"""<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>
      <friendlyName>Denon DRA-800H</friendlyName><modelName>Denon DRA-800H</modelName></device></root>"""
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(xml))
    assert client.fetch_model_name("192.0.2.1") == "DRA-800H"


@pytest.mark.parametrize("line,value", [("BL0", 0), ("BLL3", -3), ("BLR2", 2), ("BLR12", 12)])
def test_balance_parsed(line, value):
    state = ReceiverState()
    assert apply_line(state, line + "\r")
    assert state.balance == value


def test_balance_commands():
    state = loaded_state()
    assert build_command(state, "balance", 0) == ("BL", "BL0")
    assert build_command(state, "balance", -3) == ("BL", "BLL3")
    assert build_command(state, "balance", 2) == ("BL", "BLR2")
    assert build_command(state, "balance", 40) == ("BL", "BLR12")


@pytest.mark.parametrize("cmd,value", [
    ("balance", "links"),
    ("input", "XYZ"), ("sound_mode", "DOLBY"), ("volume", "laut"), ("volume_step", "left"),
    ("power", "vielleicht"), ("selbstzerstörung", None),
])
def test_invalid_commands_rejected(cmd, value):
    with pytest.raises(CommandError):
        build_command(loaded_state(), cmd, value)


def test_menu_and_firmware_parsed():
    state = ReceiverState()
    apply_line(state, "MNMEN OFF\r")
    apply_line(state, "SSINFFRMAVR 5300-2172-7016-8080\r")
    apply_line(state, "SSINFFRM END\r")
    assert state.menu_open is False
    assert state.firmware == "5300-2172-7016-8080"
    apply_line(state, "MNMEN ON\r")
    assert state.to_dict()["menu_open"] is True


@pytest.mark.parametrize("key,raw", [
    ("open", ("MNMEN", "MNMEN ON")), ("close", ("MNMEN", "MNMEN OFF")),
    ("up", (None, "MNCUP")), ("down", (None, "MNCDN")), ("left", (None, "MNCLT")), ("right", (None, "MNCRT")),
    ("enter", (None, "MNENT")), ("back", (None, "MNRTN")), ("option", (None, "MNOPT")), ("info", (None, "MNINF")),
])
def test_menu_commands(key, raw):
    assert build_command(ReceiverState(), "menu", key) == raw


def test_unknown_menu_key_rejected():
    with pytest.raises(CommandError):
        build_command(ReceiverState(), "menu", "selbstzerstörung")
