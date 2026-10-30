"use strict";

// Live-Status kommt per WebSocket vom Backend (siehe CLAUDE.md, Abschnitt WebSocket-/REST-Protokoll).
// Bedienung reagiert sofort (optimistisch) und gleicht sich dann mit dem gemeldeten Zustand ab.

const $ = (sel) => document.querySelector(sel);

const PENDING_MS = 2500; // so lange gilt ein optimistischer Wert, bis der Receiver ihn bestätigt
const POWER_PENDING_MS = 6000; // Einschalten dauert beim DRA-800H ca. 3 s
const VOLUME_SEND_MS = 90; // Taktung beim Ziehen; das Backend fasst zusätzlich zusammen
// Grenzen und Anzeige der Stepper im Klang-Bereich
const TONES = {
  bass: { min: -6, max: 6, label: (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v)} dB` },
  treble: { min: -6, max: 6, label: (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v)} dB` },
  balance: { min: -12, max: 12, label: (v) => (v === 0 ? "Mitte" : `${v < 0 ? "L" : "R"} ${Math.abs(v)}`) },
};

let ws = null;
let server = null; // letzter Zustand vom Backend
const pending = new Map(); // key → { value, until }
let drag = null; // aktive Slider-Geste
let inputsKey = "", modesKey = "";

// ---------- Zustand ----------

function view(key) {
  const p = pending.get(key);
  if (p && p.until > Date.now()) return p.value;
  return server ? server[key] : undefined;
}

function setPending(key, value, ms = PENDING_MS) {
  pending.set(key, { value, until: Date.now() + ms });
}

function reconcile() {
  const now = Date.now();
  for (const [key, p] of pending) {
    if (p.until <= now || (server && server[key] === p.value)) pending.delete(key);
  }
}

// ---------- Verbindung ----------

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "state") {
      server = msg.state;
      // Wiedergabestatus flach ablegen, damit view()/pending wie bei anderen Werten funktionieren
      server.play_state = server.now_playing ? server.now_playing.state : null;
      reconcile();
      render();
    } else if (msg.type === "error") {
      // Abgelehnter Befehl: optimistischen Wert verwerfen, damit wieder der echte Zustand gilt
      if (msg.request && msg.request.cmd) pending.delete(keyFor(msg.request.cmd));
      toast(msg.message);
      render();
    }
  };
  ws.onclose = () => {
    server = server && { ...server, connected: false };
    render();
    setTimeout(connect, 1500);
  };
}

function keyFor(cmd) {
  return { volume_step: "volume" }[cmd] || cmd;
}

function send(cmd, value) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    toast("Keine Verbindung zum Server");
    return false;
  }
  ws.send(JSON.stringify({ cmd, value }));
  return true;
}

// ---------- Darstellung ----------

const fmt = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });

function render() {
  const online = !!(server && server.connected) && ws && ws.readyState === WebSocket.OPEN;
  const power = !!view("power");
  document.body.classList.toggle("is-offline", !online);
  document.body.classList.toggle("is-off", !power);

  $("#status-text").textContent = !server || !ws || ws.readyState !== WebSocket.OPEN
    ? "Verbinde mit Server …"
    : !server.connected ? "Receiver nicht erreichbar"
    : power ? ["Eingeschaltet", sourceLabel()].filter(Boolean).join(" · ") : "Standby";
  renderNowPlaying(online && power);
  renderPlayback();

  const powerBtn = $("#power");
  powerBtn.setAttribute("aria-pressed", String(power));
  powerBtn.classList.toggle("is-pending", pending.has("power"));
  powerBtn.disabled = !online;

  if (!server) return;

  if (server.model && $("#title").textContent !== server.model) {
    $("#title").textContent = server.model;
    document.title = `Denon ${server.model}`;
  }
  renderVolume();
  renderInputs();
  renderModes();
  for (const name of Object.keys(TONES)) renderTone(name);
  renderSettings();
}

function renderSettings() {
  const open = !!view("menu_open");
  const toggle = $("#menu-toggle");
  toggle.setAttribute("aria-pressed", String(open));
  $("#menu-toggle-label").textContent = open ? "Setup-Menü schließen" : "Setup-Menü öffnen";
  $("#fw").textContent = server.firmware || "–";
  $("#heos-version").textContent = server.heos_version || "–";
  const update = $("#update");
  update.textContent = server.update_available == null ? "–"
    : server.update_available ? "Update verfügbar – im Setup-Menü installieren" : "Aktuell";
  update.classList.toggle("is-update", !!server.update_available);
}

function renderVolume() {
  const max = server.volume_max;
  const vol = drag ? drag.value : view("volume");
  const muted = !!view("mute");
  const slider = $("#vol-slider");

  $("#vol-value").textContent = vol == null ? "–" : fmt.format(vol);
  $("#vol-db").textContent = vol == null ? "" : `${fmt.format(vol - 80).replace("-", "−")} dB`;
  $("#vol-limit").textContent = `max. ${fmt.format(max)}`;
  const p = vol == null ? 0 : Math.min(vol / max, 1);
  slider.style.setProperty("--p", p);
  slider.classList.toggle("icon-covered", p * slider.clientWidth > 44); // Symbol reicht bis ca. 40 px
  slider.setAttribute("aria-valuemax", max);
  slider.setAttribute("aria-valuenow", vol ?? 0);
  slider.setAttribute("aria-valuetext", vol == null ? "unbekannt" : `${fmt.format(vol)}${muted ? ", stumm" : ""}`);

  $(".volume").classList.toggle("is-muted", muted);
  $("#mute").setAttribute("aria-pressed", String(muted));
  $("#vol-up").disabled = vol == null || vol >= max;
  $("#vol-down").disabled = vol == null || vol <= 0;
}

// Bei HEOS (NET) die eigentliche Zuspielung zeigen, z. B. "Bluetooth" statt "HEOS Music"
function sourceLabel() {
  const code = view("input");
  const np = server.now_playing;
  if (code === "NET" && np && np.source) return np.source;
  const input = server.inputs.find((i) => i.code === code);
  return input ? input.name : code;
}

function renderPlayback() {
  const np = server && server.now_playing;
  const card = $("#playback");
  card.hidden = !(np && view("input") === "NET");
  if (card.hidden) return;
  $("#pb-source").textContent = np.source || "";
  const playing = view("play_state") === "play";
  const btn = $("#playpause");
  btn.classList.toggle("is-playing", playing);
  btn.setAttribute("aria-label", playing ? "Pause" : "Wiedergabe");
}

let npText = "";
function renderNowPlaying(show) {
  const box = $("#np");
  const np = server && server.now_playing;
  const text = show && view("input") === "NET" && np && np.title
    ? [np.artist, np.title].filter(Boolean).join(" – ")
    : "";
  box.hidden = !text;
  const paused = !!np && np.state !== "play";
  box.classList.toggle("is-paused", paused);
  if (tickerAnim) paused ? tickerAnim.pause() : tickerAnim.play();
  if (text === npText) return;
  npText = text;
  box.querySelectorAll(".np-text").forEach((el) => (el.textContent = text));
  measureMarquee();
}

// Laufband: Text läuft immer von rechts nach links durch, nahtlos wiederholt, Tempo ~45 px/s
// (bei „Bewegung reduzieren“ halb so schnell). Der Abstand zwischen den Wiederholungen ist
// mindestens so breit wie das Band, damit auch ein kurzer Titel einzeln einläuft.
// Web Animations API statt CSS-Keyframes mit var(): die laufen in Safari auf dem iPhone nicht.
let tickerAnim = null;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

function measureMarquee() {
  const box = $("#np");
  const track = $("#np-track");
  if (tickerAnim) { tickerAnim.cancel(); tickerAnim = null; }
  if (box.hidden) return;
  const view = box.clientWidth - parseFloat(getComputedStyle(box).paddingLeft);
  const textWidth = track.firstElementChild.offsetWidth;
  const gap = Math.max(64, view - textWidth);
  const distance = textWidth + gap;
  track.style.gap = `${gap}px`;
  const speed = reducedMotion.matches ? 22 : 45;
  tickerAnim = track.animate(
    [{ transform: "translateX(0)" }, { transform: `translateX(${-distance}px)` }],
    { duration: (distance / speed) * 1000, iterations: Infinity, easing: "linear" },
  );
  if (box.classList.contains("is-paused")) tickerAnim.pause();
}

function renderInputs() {
  const box = $("#inputs");
  const key = JSON.stringify(server.inputs);
  if (key !== inputsKey) {
    inputsKey = key;
    box.replaceChildren(...server.inputs.map(({ code, name }) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "input-tile";
      b.setAttribute("role", "radio");
      b.dataset.code = code;
      b.textContent = name;
      return b;
    }));
  }
  const current = view("input");
  for (const b of box.children) b.setAttribute("aria-checked", String(b.dataset.code === current));
}

function renderModes() {
  const box = $("#modes");
  const key = JSON.stringify(server.sound_modes);
  if (key !== modesKey) {
    modesKey = key;
    box.querySelectorAll("button").forEach((b) => b.remove());
    for (const { code, name } of server.sound_modes) {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "radio");
      b.dataset.code = code;
      b.textContent = name;
      box.append(b);
    }
  }
  const current = view("sound_mode");
  const buttons = [...box.querySelectorAll("button")];
  buttons.forEach((b) => b.setAttribute("aria-checked", String(b.dataset.code === current)));
  // Thumb gleitet zur aktiven Option; unbekannter Modus → Thumb ausblenden
  const index = buttons.findIndex((b) => b.dataset.code === current);
  box.style.setProperty("--seg-n", buttons.length);
  box.style.setProperty("--seg-o", index >= 0 ? 1 : 0);
  if (index >= 0) box.style.setProperty("--seg-i", index);
}

function renderTone(name) {
  const row = document.querySelector(`.tone-row[data-tone="${name}"]`);
  const { min, max, label } = TONES[name];
  const v = view(name);
  row.querySelector(".tone-value").textContent = v == null ? "–" : label(v);
  row.querySelector('[data-dir="-1"]').disabled = v == null || v <= min;
  row.querySelector('[data-dir="1"]').disabled = v == null || v >= max;
}

// ---------- Bedienung ----------

$("#power").addEventListener("click", () => {
  const next = !view("power");
  if (send("power", next)) {
    setPending("power", next, POWER_PENDING_MS);
    render();
  }
});

$("#mute").addEventListener("click", () => {
  const next = !view("mute");
  if (send("mute", next)) {
    setPending("mute", next);
    render();
  }
});

$("#playpause").addEventListener("click", () => {
  const next = view("play_state") === "play" ? "pause" : "play";
  if (send("play_state", next)) {
    setPending("play_state", next);
    render();
  }
});
$("#prev").addEventListener("click", () => send("previous"));
$("#next").addEventListener("click", () => send("next"));

$("#menu-toggle").addEventListener("click", () => {
  const next = !view("menu_open");
  if (send("menu", next ? "open" : "close")) {
    setPending("menu_open", next);
    render();
  }
});
// Steuerkreuz und Zusatztasten: jeder Druck geht einzeln raus (wie auf der Fernbedienung)
for (const box of [$("#dpad"), $("#menu-keys")]) {
  box.addEventListener("click", (e) => {
    const b = e.target.closest("[data-key]");
    if (b) send("menu", b.dataset.key);
  });
}

$("#inputs").addEventListener("click", (e) => {
  const b = e.target.closest(".input-tile");
  if (b && send("input", b.dataset.code)) {
    setPending("input", b.dataset.code);
    render();
  }
});

$("#modes").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b && send("sound_mode", b.dataset.code)) {
    setPending("sound_mode", b.dataset.code);
    render();
  }
});

document.querySelectorAll(".tone-row").forEach((row) => {
  row.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    const v = view(row.dataset.tone);
    if (!b || v == null) return;
    const { min, max } = TONES[row.dataset.tone];
    const next = Math.max(min, Math.min(max, v + Number(b.dataset.dir)));
    if (send(row.dataset.tone, next)) {
      setPending(row.dataset.tone, next);
      render();
    }
  });
});

function setVolume(value) {
  const max = server.volume_max;
  const v = Math.round(Math.max(0, Math.min(value, max)) * 2) / 2;
  if (send("volume", v)) {
    setPending("volume", v);
    pending.delete("mute"); // Receiver hebt Mute bei jeder Lautstärkeänderung selbst auf
  }
  return v;
}

// +/− mit Halten zum Wiederholen; zählt lokal weiter, damit schnelles Tippen nicht verloren geht
function holdRepeat(button, dir) {
  let timer = null;
  const step = () => {
    const v = view("volume");
    if (v == null) return stop();
    const next = v + dir * 0.5;
    if (next > server.volume_max) {
      toast(`Obergrenze ${fmt.format(server.volume_max)} erreicht`);
      return stop();
    }
    if (next < 0) return stop();
    setVolume(next);
    render();
  };
  const stop = () => { clearTimeout(timer); timer = null; };
  button.addEventListener("pointerdown", (e) => {
    if (button.disabled) return;
    button.setPointerCapture(e.pointerId);
    step();
    timer = setTimeout(function repeat() { step(); if (timer) timer = setTimeout(repeat, 120); }, 420);
  });
  for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) button.addEventListener(ev, stop);
  // Tastatur: Enter/Leertaste lösen nur click aus, kein pointerdown
  button.addEventListener("click", (e) => { if (e.detail === 0) step(); });
}
holdRepeat($("#vol-down"), -1);
holdRepeat($("#vol-up"), 1);

// Slider: relative Geste wie im Kontrollzentrum. Ein bloßes Antippen ändert nichts – so kann ein
// versehentlicher Tipp ganz rechts nicht plötzlich laut machen. Bewegung folgt dem Finger 1:1.
const slider = $("#vol-slider");
const DRAG_THRESHOLD = 6; // px, bevor eine Bewegung als Ziehen gilt

function rubberband(overshoot, dimension, c = 0.55) {
  return (overshoot * dimension * c) / (dimension + c * Math.abs(overshoot));
}

slider.addEventListener("pointerdown", (e) => {
  if (!server || view("volume") == null) return;
  slider.setPointerCapture(e.pointerId);
  drag = { x0: e.clientX, v0: view("volume"), value: view("volume"), width: slider.clientWidth, active: false, lastSent: 0 };
});

slider.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const dx = e.clientX - drag.x0;
  if (!drag.active) {
    if (Math.abs(dx) < DRAG_THRESHOLD) return;
    drag.active = true;
    drag.x0 = e.clientX; // ab hier 1:1, ohne Sprung um die Schwelle
    slider.classList.add("is-dragging");
    return;
  }
  const max = server.volume_max;
  const raw = drag.v0 + (dx / drag.width) * max;
  const clamped = Math.max(0, Math.min(raw, max));
  drag.value = Math.round(clamped * 2) / 2;

  // Weicher Widerstand an den Enden statt hartem Stopp
  const overPx = ((raw - clamped) / max) * drag.width;
  if (overPx !== 0) {
    const stretch = rubberband(Math.abs(overPx), drag.width) / drag.width;
    slider.style.setProperty("--rb-origin", overPx > 0 ? "left center" : "right center");
    slider.style.transform = `scaleX(${1 + stretch * 0.25}) scaleY(${1 - stretch * 0.15})`;
  } else {
    slider.style.transform = "";
  }

  const now = performance.now();
  if (now - drag.lastSent >= VOLUME_SEND_MS && drag.value !== view("volume")) {
    drag.lastSent = now;
    setVolume(drag.value);
  }
  renderVolume();
});

function endDrag() {
  if (!drag) return;
  const finished = drag;
  drag = null;
  slider.classList.remove("is-dragging");
  slider.style.transform = "";
  if (finished.active && finished.value !== finished.v0) setVolume(finished.value);
  render();
}
slider.addEventListener("pointerup", endDrag);
slider.addEventListener("pointercancel", endDrag);

slider.addEventListener("keydown", (e) => {
  const v = view("volume");
  if (v == null) return;
  const delta = { ArrowRight: 0.5, ArrowUp: 0.5, ArrowLeft: -0.5, ArrowDown: -0.5, PageUp: 5, PageDown: -5 }[e.key];
  if (delta === undefined) return;
  e.preventDefault();
  setVolume(v + delta);
  render();
});

// ---------- Hinweise ----------

let toastTimer = null;
function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("is-visible"), 2600);
}

// Trennlinie unter der Kopfleiste nur, wenn Inhalt darunter scrollt
addEventListener("scroll", () => document.body.classList.toggle("scrolled", scrollY > 4), { passive: true });
addEventListener("resize", () => { if (server) measureMarquee(); });
// Abgelaufene optimistische Werte zurücknehmen, auch wenn keine Meldung kommt
setInterval(() => { if (pending.size) { reconcile(); render(); } }, 500);

connect();
