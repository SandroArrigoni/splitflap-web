/* Split-Flap web app — mirrors the real display and sends text to it. */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const TRANSLIT = { "ß": "SS", "ẞ": "SS", "æ": "AE", "Æ": "AE", "œ": "OE", "Œ": "OE", "ø": "O", "Ø": "O",
    "ł": "L", "Ł": "L", "đ": "D", "Đ": "D", "þ": "TH", "Þ": "TH", "–": "-", "—": "-", "‐": "-", "‑": "-",
    "−": "-", "·": ".", "€": "EUR", "×": "*" };

  const store = {
    get(key, fallback) {
      try { const v = localStorage.getItem("splitflap." + key); return v === null ? fallback : JSON.parse(v); }
      catch (e) { return fallback; }
    },
    set(key, value) { try { localStorage.setItem("splitflap." + key, JSON.stringify(value)); } catch (e) { /* private mode */ } },
  };

  let state = null;            // last /api/state
  let charset = "";
  let flapMs = 115;
  let tiles = [];              // one per module
  let animation = store.get("animation", "left");
  let built = false;
  let pollTimer = null;
  let ticker = null;

  // ---------- API ----------
  async function api(path, body) {
    const opts = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    };
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || ("HTTP " + res.status));
    return data;
  }

  // ---------- toast ----------
  let toastTimer;
  function toast(msg, isError) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.toggle("error", !!isError);
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), isError ? 4500 : 1800);
  }

  // ---------- sanitising (mirror of display.sanitize, for the preview) ----------
  function sanitize(text, length) {
    let s = (text || "").normalize("NFKD").replace(/[\r\n\t\v\f]/g, " ").replace(/\p{Mn}/gu, "");
    s = Array.from(s).map((c) => TRANSLIT[c] || c).join("").toUpperCase();
    return Array.from(s).slice(0, length);
  }

  // ---------- the display ----------
  function sizeBoard() {
    if (!state) return;
    const board = $("#board");
    const cabinet = board.closest(".cabinet");
    const avail = cabinet.clientWidth - 2 * 14 - 20 - 2;
    const gap = 4;
    const max = window.innerWidth >= 720 ? 96 : 72;
    const w = Math.max(16, Math.min(max, Math.floor((avail - gap * (state.cols - 1)) / state.cols)));
    document.documentElement.style.setProperty("--flap-w", w + "px");
    document.documentElement.style.setProperty("--flap-h", Math.round(w * 1.4) + "px");
  }

  function buildBoard() {
    const board = $("#board");
    board.innerHTML = "";
    board.style.gridTemplateColumns = `repeat(${state.cols}, var(--flap-w))`;
    const tpl = $("#flap-template");
    tiles = state.modules.map((m) => {
      const el = tpl.content.firstElementChild.cloneNode(true);
      board.appendChild(el);
      const spans = el.querySelectorAll("span");
      const tile = { el, top: spans[0], bottom: spans[1], leafTop: spans[2], leafBottom: spans[3],
        shown: m.char == null ? " " : m.char, remaining: 0, wasMoving: false, lastTarget: m.target, timer: null };
      setStatic(tile, tile.shown);
      el.classList.toggle("unknown", !m.ok);
      return tile;
    });
    sizeBoard();
  }

  function setStatic(tile, ch) {
    tile.top.textContent = tile.bottom.textContent = ch;
  }

  function flipTo(tile, ch) {
    const prev = tile.shown;
    if (prev === ch) return;
    tile.shown = ch;
    tile.top.textContent = ch;          // new character waits behind the falling leaf
    tile.bottom.textContent = prev;     // old bottom stays until the new leaf lands
    tile.leafTop.textContent = prev;
    tile.leafBottom.textContent = ch;
    tile.el.classList.remove("flipping");
    void tile.el.offsetWidth;           // restart the CSS animation
    tile.el.classList.add("flipping");
    clearTimeout(tile.timer);
    tile.timer = setTimeout(() => {
      tile.bottom.textContent = tile.shown;
      tile.el.classList.remove("flipping");
    }, flipDuration() * 2);
  }

  function flipDuration() { return Math.max(20, Math.min(60, flapMs * 0.42)); }

  function wheelDistance(from, to) {
    const n = charset.length;
    const a = charset.indexOf(from), b = charset.indexOf(to);
    if (a < 0 || b < 0) return n;
    return ((b - a) % n + n) % n || n;
  }

  function nextOnWheel(ch) {
    const i = charset.indexOf(ch);
    return charset[(i + 1) % charset.length];
  }

  function updateBoard() {
    state.modules.forEach((m, i) => {
      const tile = tiles[i];
      if (!tile) return;
      tile.el.classList.toggle("unknown", !m.ok);
      if (m.moving) {
        if (!tile.wasMoving || m.target !== tile.lastTarget) {
          tile.remaining = m.target ? wheelDistance(tile.shown, m.target) : charset.length;
        }
      } else {
        tile.remaining = 0;
        if (m.char != null && m.char !== tile.shown) flipTo(tile, m.char);
      }
      tile.wasMoving = m.moving;
      tile.lastTarget = m.target;
    });
  }

  function tick() {
    for (const tile of tiles) {
      if (tile.remaining > 0) {
        flipTo(tile, nextOnWheel(tile.shown));
        tile.remaining--;
      }
    }
  }

  // ---------- compose ----------
  function buildLines() {
    const wrap = $("#lines");
    const saved = store.get("lines", []);
    wrap.innerHTML = "";
    for (let r = 0; r < state.rows; r++) {
      const input = document.createElement("input");
      input.className = "line-input";
      input.id = "line-" + r;
      input.type = "text";
      input.maxLength = state.cols + 8; // room for characters that get transliterated away
      input.placeholder = state.rows > 1 ? "Row " + (r + 1) : "Type…";
      input.autocapitalize = "characters";
      input.spellcheck = false;
      input.setAttribute("autocorrect", "off");
      input.setAttribute("enterkeyhint", r === state.rows - 1 ? "send" : "next");
      input.value = saved[r] || "";
      input.addEventListener("input", () => { renderCells(); store.set("lines", readLines()); });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && r < state.rows - 1) { e.preventDefault(); $("#line-" + (r + 1)).focus(); }
      });
      wrap.appendChild(input);
    }
    renderCells();
  }

  function readLines() {
    return Array.from(document.querySelectorAll(".line-input")).map((i) => i.value);
  }

  function renderCells() {
    const cells = $("#cells");
    const wipe = $("#wipe").checked;
    cells.innerHTML = "";
    let unsupported = new Set(), used = 0;
    readLines().forEach((line) => {
      const row = document.createElement("div");
      row.className = "row";
      const chars = sanitize(line, state.cols);
      used = Math.max(used, chars.length);
      for (let c = 0; c < state.cols; c++) {
        const cell = document.createElement("div");
        cell.className = "cell";
        const ch = chars[c];
        if (ch === undefined) {
          if (wipe) cell.textContent = " "; else cell.classList.add("keep");
        } else if (charset.includes(ch)) {
          cell.textContent = ch === " " ? " " : ch;
        } else {
          cell.classList.add("bad");
          cell.textContent = ch;
          unsupported.add(ch);
        }
        row.appendChild(cell);
      }
      cells.appendChild(row);
    });
    $("#counter").textContent = state.rows > 1 ? `${state.rows} × ${state.cols}` : `${used} / ${state.cols}`;
    const hint = $("#hint");
    if (unsupported.size) {
      hint.innerHTML = "";
      const span = document.createElement("span");
      span.className = "warn";
      span.textContent = "Not on the flaps: " + Array.from(unsupported).join(" ");
      hint.append(span, document.createTextNode(" — those modules keep their letter."));
    } else if (state.mode === "clock") {
      hint.textContent = "Sending a message turns the clock off.";
    } else {
      hint.textContent = wipe ? "" : "Dashed modules keep what they show.";
    }
  }

  function buildAnimations() {
    const wrap = $("#animations");
    wrap.innerHTML = "";
    const list = state.animations.filter((a) => state.rows > 1 || !a.multi_row);
    if (!list.some((a) => a.id === animation)) animation = list[0].id;
    for (const a of list) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pill";
      b.textContent = a.label;
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(a.id === animation));
      b.addEventListener("click", () => {
        animation = a.id;
        store.set("animation", animation);
        wrap.querySelectorAll(".pill").forEach((p) => p.setAttribute("aria-checked", String(p === b)));
        updateStaggerRow();
      });
      wrap.appendChild(b);
    }
    updateStaggerRow();
  }

  function updateStaggerRow() {
    $("#stagger-row").style.opacity = animation === "start" || animation === "stop" ? ".4" : "1";
  }

  function options() {
    return {
      animation,
      same: $("#same").checked,
      wipe: $("#wipe").checked,
      slowdown: Number($("#slowdown").value),
    };
  }

  async function send(lines) {
    const btn = $("#send");
    btn.disabled = true;
    try {
      const body = Object.assign({ lines }, options());
      await api("/api/display", body);
      poll(true);
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- presets & history ----------
  function linesFromLabel(label) {
    return state.rows > 1 ? label.split(" / ") : [label];
  }

  function renderPresets() {
    const wrap = $("#presets");
    const sig = JSON.stringify(state.presets);
    if (wrap.dataset.sig === sig) return;
    wrap.dataset.sig = sig;
    wrap.innerHTML = "";
    if (!state.presets.length) {
      wrap.innerHTML = '<span class="empty">Type something and tap Save.</span>';
      return;
    }
    for (const text of state.presets) {
      const chip = document.createElement("span");
      chip.className = "chip";
      const main = document.createElement("button");
      main.type = "button";
      main.className = "chip-text";
      main.textContent = text;
      main.title = "Show " + text;
      main.addEventListener("click", () => { fillLines(linesFromLabel(text)); send(linesFromLabel(text)); });
      const x = document.createElement("button");
      x.type = "button";
      x.className = "chip-x";
      x.setAttribute("aria-label", "Remove " + text);
      x.textContent = "×";
      x.addEventListener("click", async () => {
        try { await api("/api/presets/delete", { text }); poll(true); } catch (e) { toast(e.message, true); }
      });
      chip.append(main, x);
      wrap.appendChild(chip);
    }
  }

  function renderHistory() {
    const list = $("#history");
    const sig = JSON.stringify(state.history);
    if (list.dataset.sig === sig) return;
    list.dataset.sig = sig;
    list.innerHTML = "";
    if (!state.history.length) {
      list.innerHTML = '<li class="empty">Nothing sent yet.</li>';
      return;
    }
    for (const text of state.history.slice(0, 6)) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = text;
      b.addEventListener("click", () => { fillLines(linesFromLabel(text)); send(linesFromLabel(text)); });
      li.appendChild(b);
      list.appendChild(li);
    }
  }

  function fillLines(lines) {
    document.querySelectorAll(".line-input").forEach((input, r) => { input.value = lines[r] || ""; });
    store.set("lines", readLines());
    renderCells();
  }

  // ---------- status & hardware ----------
  function renderStatus() {
    const hw = state.hardware;
    const pill = $("#status");
    pill.classList.remove("ok", "sim", "err");
    let text;
    if (!hw.ok) { pill.classList.add("err"); text = "Offline"; }
    else if (hw.kind === "simulator") { pill.classList.add("sim"); text = "Simulator"; }
    else { pill.classList.add("ok"); text = "Connected"; }
    pill.querySelector(".status-text").textContent = text;

    $("#mode-badge").hidden = state.mode !== "clock";
    $("#busy-badge").hidden = !(state.busy || state.modules.some((m) => m.moving));
    const clock = $("#clock");
    clock.checked = state.mode === "clock";
    $("#clock-label").textContent = clock.checked ? "On" : "Off";
    document.querySelectorAll("#clock-formats .pill").forEach((p) =>
      p.setAttribute("aria-checked", String(p.dataset.format === state.clock_format)));

    $("#hw-summary").textContent = hw.ok ? (hw.kind === "simulator" ? "simulated" : "all boards answering") : "check connection";
    const info = $("#hw-info");
    const rows = [
      ["Bus", hw.kind === "i2c" ? "I²C (/dev/i2c-1)" : hw.kind === "simulator" ? "Simulator — no display attached" : "Not available"],
      ["Layout", `${state.rows} × ${state.cols} modules`],
      ["Boards", hw.boards.map((b) => `${b.address} · ${b.modules} modules ${b.ok ? "✓" : "✕"}`).join("<br>")],
      ["Flap time", `${state.flap_ms} ms per flap`],
    ];
    if (hw.error) rows.push(["Error", hw.error]);
    if (state.last_error && state.last_error !== hw.error) rows.push(["Last send", state.last_error]);
    const html = rows.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(v).replace(/&lt;br&gt;/g, "<br>")}</dd>`).join("");
    if (info.dataset.html !== html) { info.innerHTML = html; info.dataset.html = html; }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function buildCharset() {
    const wrap = $("#charset");
    wrap.innerHTML = "";
    for (const ch of charset) {
      const s = document.createElement("span");
      s.textContent = ch === " " ? " " : ch;
      s.title = ch === " " ? "space" : ch;
      wrap.appendChild(s);
    }
  }

  // ---------- polling ----------
  async function poll(soon) {
    clearTimeout(pollTimer);
    if (soon) { pollTimer = setTimeout(poll, 120); return; }
    try {
      state = await api("/api/state");
      if (!built) {
        charset = state.charset;
        flapMs = state.flap_ms;
        document.documentElement.style.setProperty("--flip", flipDuration() + "ms");
        buildBoard();
        buildLines();
        buildAnimations();
        buildCharset();
        clearInterval(ticker);
        ticker = setInterval(tick, flapMs);
        built = true;
      }
      updateBoard();
      renderStatus();
      renderPresets();
      renderHistory();
      if (document.activeElement && !document.activeElement.classList.contains("line-input")) renderCells();
    } catch (e) {
      const pill = $("#status");
      pill.classList.remove("ok", "sim");
      pill.classList.add("err");
      pill.querySelector(".status-text").textContent = "No connection";
    }
    const active = state && (state.busy || state.modules.some((m) => m.moving));
    if (!document.hidden) pollTimer = setTimeout(poll, active ? 150 : 1200);
  }

  // ---------- wiring ----------
  function wire() {
    $("#compose").addEventListener("submit", (e) => {
      e.preventDefault();
      send(readLines());
    });
    $("#save").addEventListener("click", async () => {
      const label = readLines().join(" / ").replace(/( \/ )+$/, "");
      if (!label.trim()) { toast("Type something first"); return; }
      try { await api("/api/presets", { text: state.rows > 1 ? label : readLines()[0] }); toast("Saved"); poll(true); }
      catch (e) { toast(e.message, true); }
    });
    $("#clear").addEventListener("click", () => run("/api/clear", { animation, slowdown: options().slowdown }));
    $("#resync").addEventListener("click", () => run("/api/resync", {}, "Every module turns once"));
    $("#stop").addEventListener("click", () => run("/api/stop", {}, "Stopped sending"));
    $("#clock").addEventListener("change", (e) => {
      const fmt = state && state.clock_format;
      run("/api/mode", { mode: e.target.checked ? "clock" : "text", format: fmt });
    });
    document.querySelectorAll("#clock-formats .pill").forEach((p) =>
      p.addEventListener("click", () => run("/api/mode", { mode: "clock", format: p.dataset.format })));
    $("#scan").addEventListener("click", async () => {
      const out = $("#scan-result");
      out.textContent = "Scanning…";
      try {
        const r = await api("/api/scan");
        out.textContent = `Found: ${r.found.join(", ") || "nothing"}` +
          (r.missing && r.missing.length ? ` — missing ${r.missing.join(", ")} (check config.json)` : " — all expected boards answer.");
      } catch (e) { out.textContent = e.message; }
    });
    $("#status").addEventListener("click", () => {
      const hw = $("#hardware");
      hw.open = true;
      hw.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    const slider = $("#slowdown");
    slider.value = store.get("slowdown", 1);
    const showSlow = () => { $("#slowdown-out").textContent = slider.value + "×"; };
    slider.addEventListener("input", () => { showSlow(); store.set("slowdown", Number(slider.value)); });
    showSlow();

    for (const id of ["same", "wipe"]) {
      const box = $("#" + id);
      box.checked = store.get(id, box.checked);
      box.addEventListener("change", () => { store.set(id, box.checked); if (state) renderCells(); });
    }

    window.addEventListener("resize", sizeBoard);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  }

  async function run(path, body, okMsg) {
    try { await api(path, body); if (okMsg) toast(okMsg); poll(true); }
    catch (e) { toast(e.message, true); }
  }

  wire();
  poll();
})();
