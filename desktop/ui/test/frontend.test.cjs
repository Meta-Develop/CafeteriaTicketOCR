"use strict";

// Explicit mock bridge and DOM. No browser, camera, native service, or network is used.
// Run: node desktop/ui/test/frontend.test.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.open = false;
    this.children = [];
    this.events = new Map();
    this.attributes = new Map();
    this._value = "";
    this.className = "";
  }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  addEventListener(name, callback) { this.events.set(name, callback); }
  async fire(name, event = {}) { return this.events.get(name)?.({ target: this, ...event }); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  replaceChildren() { this.children = []; this.value = ""; }
  append(child) { this.children.push(child); if (this.tagName === "SELECT" && this.children.length === 1) this.value = child.value; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() { this.focused = true; }
  closest() { return null; }
  getBoundingClientRect() { return { left: 600, right: 1000, top: 0, bottom: 800 }; }
}

const disconnected = { connected: false, device: null, session_id: null, width: 0, height: 0, exposure_us: null, min_exposure_us: null, gain: null, gain_min: null, gain_max: null, auto_exposure: false, controls: {}, error: null };
const connected = { ...disconnected, connected: true, device: "usb:test", session_id: "session-A", width: 1280, height: 720, exposure_us: 100, min_exposure_us: 100, gain: 24, gain_min: 0, gain_max: 64, stream_fps: 110, brightness: 80, controls: {
  exposure_time_absolute: { kind: "int", min: 1, max: 5000, step: 1, value: 1, flags: "", choices: {} },
  gain: { kind: "int", min: 0, max: 64, step: 2, value: 24, flags: "", choices: {} },
  brightness: { kind: "int", min: -64, max: 64, step: 1, value: 0, flags: "", choices: {} },
  focus_auto: { kind: "bool", min: 0, max: 1, step: 1, value: 1, flags: "", choices: {} },
  power_line_frequency: { kind: "menu", min: 0, max: 3, step: 1, value: 1, flags: "", choices: { 0: "Disabled", 1: "50 Hz", 2: "60 Hz" } },
} };
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise(setImmediate); };
const result = (capture_id, status = "matched") => ({ capture_id, status, name: status === "matched" ? "テスト用カレー" : null, price: status === "matched" ? 540 : null, score: 90, confidence: 90, text: "テスト専用の読み取り文字", elapsed_ms: 510, message: status === "no_match" ? "メニューが見つかりませんでした。" : "" });

function harness({ attached = true, devices, ocrReady = true, native = true, deferDecode = false, listenFailure = false, listenersDeferred = false } = {}) {
  const elements = new Map();
  for (const match of html.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[1]);
    element.hidden = /\bhidden\b/.test(match[2]);
    element.disabled = /\bdisabled\b/.test(match[2]);
    elements.set(match[3], element);
  }
  const radios = ["short", "auto", "manual"].map((value) => {
    const element = value === "manual" ? elements.get("manual-mode") : new Element("input");
    element.value = value;
    element.checked = value === "short";
    return element;
  });
  const calls = [];
  const trace = [];
  const listeners = new Map();
  const listenerResolvers = [];
  const removed = [];
  const timers = new Map();
  const intervals = new Map();
  const rafs = new Map();
  const events = new Map();
  const documentEvents = new Map();
  const decodes = [];
  let seq = 0;
  let now = 1000;
  let decodedCount = 0;
  let closedCount = 0;
  let drawCount = 0;
  let clearCount = 0;
  let brightness = 128;
  let current = { camera: { ...disconnected }, devices: devices || (attached ? [{ id: "usb:test", label: "テストUSBカメラ" }] : []), ocr: { ready: ocrReady, menu_count: 2, error: ocrReady ? null : "メニューを読み込めません。アプリを起動し直してください。" }, busy: false, live: { enabled: true, running: true, error: null, confirm_passes: 0, dedup_gap: 1.5, roi: 1, scale: 1 } };
  const handlers = {};
  const context2d = {
    drawImage() { drawCount++; },
    clearRect() { clearCount++; },
    getImageData() { return { data: new Uint8ClampedArray(32 * 18 * 4).fill(brightness) }; },
  };
  elements.get("camera-canvas").getContext = () => context2d;
  const document = {
    getElementById: (id) => { assert(elements.has(id), `Missing HTML element: ${id}`); return elements.get(id); },
    querySelectorAll: () => radios,
    createElement(tag) { const element = new Element(tag); if (tag === "canvas") element.getContext = () => context2d; return element; },
    addEventListener: (name, callback) => documentEvents.set(name, callback),
  };
  const window = { addEventListener: (name, callback) => events.set(name, callback) };
  if (native) window.__TAURI__ = {
    core: {
      async invoke(command, args) {
        assert.equal(command, "backend_request");
        assert.deepEqual(Object.keys(args), ["request"]);
        const request = args.request;
        calls.push(JSON.parse(JSON.stringify(request)));
        trace.push(`invoke:${request.action}`);
        if (handlers[request.action]) return handlers[request.action](request);
        if (request.action === "status") return structuredClone(current);
        if (request.action === "start_camera") {
          if (!attached) throw new Error("USBカメラが見つかりません。接続を確認してください。");
          const device = current.devices.find((device) => !request.device || request.device === device.id);
          if (!device) throw new Error("選択したカメラが接続されていません。");
          current.camera = { ...connected, device: device.id };
          return structuredClone(current);
        }
        if (request.action === "set_camera") return { accepted: true };
        if (request.action === "set_live") { Object.assign(current.live, request); return { accepted: true, ...current.live }; }
        if (request.action === "shutdown") return { accepted: true };
        assert.fail(`Unexpected action ${request.action}`);
      },
    },
    event: {
      async listen(name, callback) {
        trace.push(`listen:${name}`);
        if (listenFailure && name === "camera_frame") throw new Error("映像の受信を開始できません。");
        if (listenersDeferred) await new Promise((resolve) => listenerResolvers.push(resolve));
        listeners.set(name, callback);
        return () => { removed.push(name); listeners.delete(name); };
      },
    },
  };
  const environment = {
    window, document, Intl, Blob, Uint8Array, URL, Image: class {},
    performance: { now: () => now },
    atob: (encoded) => Buffer.from(encoded, "base64").toString("binary"),
    requestAnimationFrame: (callback) => { const id = ++seq; rafs.set(id, callback); return id; },
    cancelAnimationFrame: (id) => rafs.delete(id),
    setInterval: (callback) => { const id = ++seq; intervals.set(id, callback); return id; },
    clearInterval: (id) => intervals.delete(id),
    setTimeout: (callback, ms) => { assert.equal(ms, 75); const id = ++seq; timers.set(id, callback); return id; },
    clearTimeout: (id) => timers.delete(id),
    createImageBitmap: () => {
      decodedCount++;
      const bitmap = { width: 640, height: 360, close() { closedCount++; } };
      return deferDecode ? new Promise((resolve) => decodes.push(() => resolve(bitmap))) : Promise.resolve(bitmap);
    },
  };
  vm.runInNewContext(source, environment, { filename: "app.js" });
  return {
    ui: (id) => elements.get(id), calls, trace, handlers, radios, removed, timers, intervals, rafs, decodes,
    emit: (name, payload) => listeners.get(name)?.({ payload }),
    async frame(session_id = current.camera.session_id) { listeners.get("camera_frame")?.({ payload: { jpeg: "YQ==", session_id, width: 640, height: 360, timestamp_ms: now } }); await this.raf(); },
    async raf() { const callbacks = [...rafs.values()]; rafs.clear(); for (const callback of callbacks) callback(); await settle(); },
    flushTimers() { const callbacks = [...timers.values()]; timers.clear(); for (const callback of callbacks) callback(); },
    async tick(ms = 4001) { now += ms; for (const callback of intervals.values()) await callback(); await settle(); },
    mode(value) { radios.forEach((radio) => { radio.checked = radio.value === value; }); return radios.find((radio) => radio.value === value).fire("change"); },
    status: () => current,
    setCamera(camera) { current.camera = { ...camera }; this.emit("camera_state", camera); },
    setBrightness(value) { brightness = value; },
    advance(ms) { now += ms; },
    releaseListeners() { listenerResolvers.forEach((resolve) => resolve()); },
    key: (event) => documentEvents.get("keydown")(event),
    close: () => events.get("pagehide")(),
    counts: () => ({ decodedCount, closedCount, drawCount, clearCount }),
  };
}

async function run() {
  assert(!/mock|fixture|テスト用/.test(html), "Production HTML has no fixture data");
  assert(!/撮影して認識|settings-apply|<dialog/.test(html));
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
  assert.equal(ids.length, new Set(ids).size);
  for (const match of html.matchAll(/(?:src|href)="([^"#]+)"/g)) {
    assert(!/^(?:https?:|\/)/.test(match[1]));
    assert(fs.existsSync(path.join(__dirname, "..", match[1])));
  }
  const ordering = harness({ listenersDeferred: true });
  await settle(); assert.equal(ordering.calls.length, 0);
  ordering.releaseListeners(); await settle();
  assert.deepEqual(ordering.trace.slice(0, 7), ["listen:camera_frame", "listen:camera_state", "listen:live_recognition", "listen:ticket_detected", "listen:live_state", "invoke:status", "invoke:start_camera"]);
  ordering.close();
  const noBridge = harness({ native: false }); await settle();
  assert.match(noBridge.ui("app-error-text").textContent, /デスクトップアプリ/);
  assert.equal(noBridge.ui("live-toggle").disabled, true);
  const failed = harness({ listenFailure: true }); await settle();
  assert.equal(failed.calls.length, 0); assert.equal(failed.removed.length, 4);
  const missing = harness({ attached: false }); await settle();
  assert.match(missing.ui("camera-empty-message").textContent, /USBカメラが見つかりません/);
  assert.equal(missing.ui("gain-input").disabled, true); missing.close();

  const app = harness(); await settle(); await app.frame();
  assert.equal(app.ui("camera-canvas").hidden, false);
  assert.equal(app.calls.some((c) => c.action === "recognize"), false, "Live view needs no shot request");
  const liveResult = { ...result("capture-1"), score: 1.1, session_id: connected.session_id, ocr_latency_ms: 51, passes_per_second: 18.2 };
  app.emit("live_recognition", liveResult);
  assert.match(app.ui("current-match").textContent, /テスト用カレー/);
  assert.equal(app.ui("ocr-text").textContent, liveResult.text);
  assert.match(app.ui("ocr-metrics").textContent, /18.2.*51 ms/);
  app.emit("ticket_detected", { ...liveResult, timestamp_ms: 1794898800000 });
  assert.equal(app.ui("product-name").textContent, "テスト用カレー");
  assert.equal(app.ui("price-number").textContent, "540");
  assert.equal(app.ui("detection-history").children.length, 1);
  assert.match(app.ui("detection-history").children[0].children[0].textContent, /\d{2}:\d{2}:\d{2}/);
  app.emit("ticket_detected", liveResult);
  assert.equal(app.ui("detection-history").children.length, 1, "Duplicate capture ids do not duplicate tickets");
  app.emit("live_recognition", { ...liveResult, status: "no_match", name: null, price: null, text: "" });
  assert.match(app.ui("current-match").textContent, /一致するメニューはありません/);
  assert.equal(app.ui("product-name").textContent, "テスト用カレー", "Latest detected ticket persists through misses");
  const before = app.counts().drawCount; await app.frame();
  assert(app.counts().drawCount > before, "Preview continues during live OCR");
  await app.ui("live-toggle").fire("click");
  assert.deepEqual(app.calls.at(-1), { action: "set_live", enabled: false });
  assert.match(app.ui("live-status").textContent, /一時停止/);
  app.emit("live_recognition", { ...liveResult, text: "stale" });
  assert.notEqual(app.ui("ocr-text").textContent, "stale");
  await app.frame(); assert(app.counts().drawCount > before + 1, "Pause only stops recognition");
  await app.ui("live-toggle").fire("click");
  assert.deepEqual(app.calls.at(-1), { action: "set_live", enabled: true });

  assert.equal(app.ui("exposure-input").min, "100");
  assert.equal(app.ui("exposure-input").max, "500000");
  assert.equal(app.ui("exposure-input").step, "100");
  assert.equal(app.ui("gain-input").step, "2");
  app.ui("exposure-input").value = "400";
  await app.ui("exposure-input").fire("input");
  app.ui("gain-input").value = "32";
  for (let i = 0; i < 50; i++) await app.ui("gain-input").fire("input");
  assert.equal(app.timers.size, 1, "Dragging coalesces edits into one 75ms task");
  assert.equal(app.calls.filter((c) => c.action === "set_camera").length, 0);
  app.flushTimers(); await settle();
  assert.deepEqual(app.calls.at(-1), { action: "set_camera", exposure_mode: "manual", exposure_us: 400, gain: 32 });
  let finishWrite;
  app.handlers.set_camera = () => new Promise((resolve) => { finishWrite = resolve; });
  app.ui("gain-input").value = "34"; await app.ui("gain-input").fire("input"); app.flushTimers(); await settle();
  app.ui("gain-input").value = "36"; await app.ui("gain-input").fire("input"); app.flushTimers(); await settle();
  assert.equal(app.calls.at(-1).gain, 34);
  finishWrite({ accepted: true }); await settle();
  delete app.handlers.set_camera;
  app.flushTimers(); await settle();
  assert.equal(app.calls.at(-1).gain, 36, "Edits during a write apply as soon as that write completes");
  const native = (name) => app.ui("native-controls").children.find((c) => c.id === `control-${name}`);
  native("brightness").value = "-10"; await native("brightness").fire("input");
  native("focus_auto").checked = false; await native("focus_auto").fire("change");
  native("power_line_frequency").value = "2"; await native("power_line_frequency").fire("change");
  app.flushTimers(); await settle();
  assert.deepEqual(app.calls.at(-1), { action: "set_camera", controls: { brightness: -10, focus_auto: 0, power_line_frequency: 2 } });
  app.setCamera({ ...connected, exposure_us: 400, gain: 36, brightness: 20, controls: { ...connected.controls, gain: { ...connected.controls.gain, value: 36 } } });
  assert.match(app.ui("settings-readback").textContent, /400.*36/);
  assert.match(app.ui("settings-readback").textContent, /110 fps.*20/);
  assert.equal(app.ui("dark-banner").hidden, false);
  app.handlers.set_camera = () => { throw new Error("カメラ設定を変更できません。"); };
  await app.mode("auto"); app.flushTimers(); await settle();
  assert.match(app.ui("settings-error").textContent, /変更できません/);
  app.ui("roi-input").value = "0.5"; await app.ui("roi-input").fire("change");
  assert.deepEqual(app.calls.at(-1), { action: "set_live", roi: 0.5 });
  const malicious = "<img src=x onerror=alert(1)>";
  app.emit("ticket_detected", { ...liveResult, capture_id: "unsafe-text", name: malicious });
  assert.equal(app.ui("product-name").textContent, malicious, "OCR names are text, never HTML");
  for (let i = 0; i < 205; i++) app.emit("ticket_detected", { ...liveResult, capture_id: `ticket-${i}` });
  assert.equal(app.ui("detection-history").children.length, 200, "History memory is bounded");
  app.close(); await settle();
  assert.equal(app.removed.length, 5); assert.equal(app.timers.size, 0); assert.equal(app.intervals.size, 0);
  assert.equal(app.calls.at(-1).action, "shutdown");

  const recovery = harness({ attached: false }); await settle();
  recovery.status().devices = [{ id: "usb:test", label: "再接続カメラ" }];
  recovery.handlers.start_camera = () => { recovery.status().camera = { ...connected }; return structuredClone(recovery.status()); };
  await recovery.tick(7000); await recovery.frame();
  assert.equal(recovery.ui("camera-canvas").hidden, false);
  let releaseStatus;
  recovery.handlers.status = () => new Promise((resolve) => { releaseStatus = resolve; });
  const staleStatus = recovery.tick(); await settle();
  recovery.setCamera({ ...connected, error: "新しい状態" });
  releaseStatus({ ...structuredClone(recovery.status()), camera: { ...disconnected } }); await staleStatus;
  assert.equal(recovery.ui("camera-canvas").hidden, false, "Late status cannot overwrite camera events");
  recovery.close();

  const cameraA = { id: "usb:A", label: "カメラA" }, cameraB = { id: "usb:B", label: "カメラB" };
  const selection = harness({ devices: [cameraA, cameraB] }); await settle();
  selection.ui("camera-select").value = cameraB.id; await selection.ui("camera-select").fire("change");
  await selection.ui("camera-connect").fire("click");
  selection.status().devices = [cameraA]; selection.setCamera({ ...disconnected });
  await selection.tick(7000);
  assert.deepEqual(selection.calls.at(-1), { action: "start_camera", device: cameraB.id });
  assert.match(selection.ui("camera-empty-message").textContent, /カメラB.*見つかりません/);
  selection.close();

  const sessions = harness({ deferDecode: true }); await settle(); await sessions.frame();
  sessions.setCamera({ ...connected, session_id: "session-B" });
  await sessions.frame("session-A"); sessions.decodes.shift()(); await settle();
  assert.equal(sessions.counts().drawCount, 0, "Old in-flight decode cannot draw in a new session");
  sessions.emit("live_recognition", { ...liveResult, text: "stale session" });
  assert.notEqual(sessions.ui("ocr-text").textContent, "stale session");
  await sessions.frame(); sessions.decodes.shift()(); await settle();
  assert.equal(sessions.ui("camera-canvas").hidden, false);
  sessions.setCamera({ ...connected, session_id: "session-B-reopen" });
  await sessions.frame("session-B"); assert.equal(sessions.rafs.size, 0);
  sessions.close();

  const bounded = harness({ deferDecode: true }); await settle(); await bounded.frame();
  for (let i = 0; i < 100; i++) bounded.emit("camera_frame", { jpeg: "YQ==", session_id: connected.session_id });
  assert.equal(bounded.counts().decodedCount, 1);
  bounded.decodes.shift()(); await settle(); assert.equal(bounded.rafs.size, 1);
  await bounded.raf(); bounded.close(); bounded.decodes.shift()(); await settle();
  assert.equal(bounded.counts().closedCount, 2); assert.equal(bounded.rafs.size, 0);
  console.log("PASS frontend: live OCR/tickets/history, pause/resume, 75ms immediate controls and in-flight edits, native ranges/toggles/menus, readback, safe text, listener ordering, retries/session isolation, bounded preview/history, shutdown (mock DOM + Tauri; no GUI/camera)");
}
run().catch((error) => { console.error(error); process.exitCode = 1; });
