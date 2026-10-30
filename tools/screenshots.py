"""Screenshots der Web-UI für die README, mit Demo-Daten.

Der WebSocket wird von Playwright ersetzt: es geht kein Befehl an den Receiver, und es
erscheint kein echter Titel. Aufruf siehe README, Abschnitt „Screenshots erneuern“.
"""

import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = sys.argv[1]
OUT = sys.argv[2]
# Echter Zustand als Vorlage (Eingangsnamen, Firmware …), Wiedergabe durch Demo ersetzt
state = json.load(urllib.request.urlopen(f"{BASE}/api/state"))
state.update({
    "connected": True, "power": True, "volume": 22.0, "mute": False, "input": "NET",
    "sound_mode": "STEREO", "bass": 0, "treble": 0, "balance": 0, "menu_open": False,
    "update_available": False,
    "now_playing": {"source": "Bluetooth", "title": "Folge 42 – Wie der Receiver ins Netz kam",
                    "artist": "Demo-Podcast", "album": "2026", "state": "play"},
})


def mock_ws(st):
    def handler(ws):
        ws.send(json.dumps({"type": "state", "state": st}))
        ws.on_message(lambda _m: None)  # Befehle ignorieren
    return handler


def freeze(page):
    # Laufband und Übergänge anhalten, damit jedes Bild gleich aussieht
    page.wait_for_timeout(800)
    page.evaluate("document.getAnimations().forEach(a => { a.pause(); a.currentTime = 0; })")
    page.wait_for_timeout(300)


def shoot(p, name, *, width, height, scheme, st=state, full_page=False, clip_selector=None):
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2,
                              color_scheme=scheme)
    page = ctx.new_page()
    page.route_web_socket("**/ws", mock_ws(st))
    page.goto(BASE)
    freeze(page)
    if clip_selector:
        page.locator(clip_selector).screenshot(path=f"{OUT}/{name}.png")
    else:
        page.screenshot(path=f"{OUT}/{name}.png", full_page=full_page)
    browser.close()


with sync_playwright() as p:
    shoot(p, "mobile-hell", width=390, height=844, scheme="light", full_page=True)
    shoot(p, "mobile-dunkel", width=390, height=844, scheme="dark")
    shoot(p, "desktop-dunkel", width=1280, height=900, scheme="dark", full_page=True)
    shoot(p, "standby", width=390, height=844, scheme="light", st={**state, "power": False, "now_playing": None})
    shoot(p, "einstellungen", width=390, height=844, scheme="light", clip_selector=".settings")
print("fertig")
