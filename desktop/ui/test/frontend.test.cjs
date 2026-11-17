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

const disconnected = { connected: false, device: null, session_id: null, width: 0, height: 0, exposure_us: null, min_exposure_us: null, gain: null, gain_min: null, gain_max: null, auto_exposure: false, error: null };
const connected = { ...disconnected, connected: true, device: "usb:test", session_id: "session-A", width: 1280, height: 720, exposure_us: 100, min_exposure_us: 100, gain: 24, gain_min: 0, gain_max: 64 };
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
  let current = { camera: { ...disconnected }, devices: devices || (attached ? [{ id: "usb:test", label: "テストUSBカメラ" }] : []), ocr: { ready: ocrReady, menu_count: 2, error: ocrReady ? null : "メニューを読み込めません。アプリを起動し直してください。" }, busy: false };
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
        if (request.action === "recognize") return { accepted: true, capture_id: `capture-${calls.filter((call) => call.action === "recognize").length}` };
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
    setTimeout: (callback) => { const id = ++seq; timers.set(id, callback); return id; },
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
  assert(!/mock|fixture|テスト用/.test(html), "Production HTML must not include fixture results");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(ids.length, new Set(ids).size, "Static HTML ids must be unique");
  for (const match of html.matchAll(/<(?:link|script)\b[^>]*(?:href|src)="([^"]+)"/g)) {
    assert(!/^(?:https?:|\/)/.test(match[1]), "App assets must be local");
    assert(fs.existsSync(path.join(__dirname, "..", match[1])), `Missing static asset: ${match[1]}`);
  }
  const ordering = harness({ listenersDeferred: true });
  await settle();
  assert.equal(ordering.calls.length, 0, "No commands before all listeners are ready");
  ordering.releaseListeners();
  await settle();
  assert.deepEqual(ordering.trace.slice(0, 5), ["listen:camera_frame", "listen:camera_state", "listen:recognition_result", "invoke:status", "invoke:start_camera"]);
  ordering.close();

  const noBridge = harness({ native: false });
  await settle();
  assert.equal(noBridge.ui("app-error").hidden, false);
  assert.match(noBridge.ui("app-error-text").textContent, /デスクトップアプリ/);
  assert.equal(noBridge.ui("scan-button").disabled, true);

  const failedListeners = harness({ listenFailure: true });
  await settle();
  assert.equal(failedListeners.calls.length, 0);
  assert.equal(failedListeners.removed.length, 2, "Partial listener registration must be cleaned up");

  const missing = harness({ attached: false });
  await settle();
  assert.equal(missing.ui("scan-button").disabled, true);
  assert.equal(missing.ui("camera-retry").hidden, false);
  assert.match(missing.ui("camera-empty-message").textContent, /USBカメラが見つかりません/);
  assert.equal(missing.ui("settings-apply").disabled, true);
  assert.equal(missing.calls.filter((call) => call.action === "set_camera").length, 0);
  missing.close();

  const app = harness();
  await settle();
  assert.equal(app.ui("scan-button").disabled, true, "A connection alone is not a frame");
  await app.frame();
  assert.equal(app.ui("scan-button").disabled, false);
  assert.equal(app.ui("camera-canvas").hidden, false);
  assert.equal(app.ui("device-label").textContent, "テストUSBカメラ");
  assert.equal(app.ui("gain-input").min, "0");
  assert.equal(app.ui("gain-input").max, "64");
  assert.equal(app.ui("exposure-input").min, "100");
  assert.equal(app.calls.filter((call) => call.action === "set_camera").length, 0, "Readback must not auto-apply settings");

  await app.ui("scan-button").fire("click");
  assert.equal(app.ui("result-busy").hidden, false);
  assert.equal(app.ui("scan-button").disabled, true);
  const before = app.counts().drawCount;
  await app.frame();
  assert(app.counts().drawCount > before, "Preview continues during recognition");
  app.emit("recognition_result", result("old-capture"));
  assert.equal(app.ui("result-busy").hidden, false, "Unrelated capture must not change current result");
  app.emit("recognition_result", result("capture-1"));
  assert.equal(app.ui("product-name").textContent, "テスト用カレー");
  assert.equal(app.ui("price-number").textContent, "540");
  assert.equal(app.ui("product-price").hidden, false);
  assert.equal(app.ui("ocr-text").textContent, "テスト専用の読み取り文字");
  assert.equal(app.ui("ocr-details").open, false);
  assert.equal(app.ui("scan-button").disabled, false);

  await app.ui("scan-button").fire("click");
  assert.equal(app.ui("result-data").hidden, true, "Previous result is hidden immediately on a new scan");
  assert.equal(app.ui("ocr-text").textContent, "");
  app.emit("recognition_result", result("capture-1"));
  assert.equal(app.ui("result-busy").hidden, false);
  app.emit("recognition_result", result("capture-2", "no_match"));
  assert.equal(app.ui("product-price").hidden, true);
  assert.match(app.ui("product-name").textContent, /特定できません/);
  assert.equal(app.ui("result-retry").hidden, false);

  app.handlers.recognize = () => { throw new Error("食券を撮影できませんでした。再試行してください。"); };
  await app.ui("result-retry").fire("click");
  assert.match(app.ui("result-message").textContent, /撮影できませんでした/);
  assert.equal(app.ui("product-price").hidden, true);
  assert.equal(app.ui("scan-button").disabled, false);

  app.handlers.recognize = () => { app.emit("recognition_result", result("early", "no_match")); return { accepted: true, capture_id: "early" }; };
  await app.ui("scan-button").fire("click");
  assert.equal(app.ui("result-busy").hidden, true, "A result arriving before acceptance is preserved by capture id");
  assert.match(app.ui("product-name").textContent, /特定できません/);

  await app.ui("settings-open").fire("click");
  assert.equal(app.ui("settings-dialog").open, true);
  await app.mode("manual");
  app.ui("exposure-input").value = "50";
  await app.ui("exposure-input").fire("input");
  await app.ui("settings-apply").fire("click");
  assert.match(app.ui("settings-error").textContent, /100/);
  assert.equal(app.calls.filter((call) => call.action === "set_camera").length, 0);
  app.ui("exposure-input").value = "400";
  app.ui("gain-input").value = "32";
  for (let i = 0; i < 50; i++) await app.ui("gain-input").fire("input");
  assert.equal(app.timers.size, 1, "Slider edits have one debounced task");
  assert.equal(app.calls.filter((call) => call.action === "set_camera").length, 0);
  app.flushTimers();
  await app.ui("settings-apply").fire("click");
  assert.deepEqual(app.calls.find((call) => call.action === "set_camera"), { action: "set_camera", exposure_mode: "manual", exposure_us: 400, gain: 32 });
  app.setCamera({ ...connected, exposure_us: 400, gain: 32 });
  assert.match(app.ui("settings-readback").textContent, /400/);
  assert.equal(app.ui("settings-apply").disabled, true);
  app.handlers.set_camera = () => { throw new Error("カメラが設定に対応していません。"); };
  await app.mode("auto");
  await app.ui("settings-apply").fire("click");
  assert.match(app.ui("settings-error").textContent, /対応していません/);
  assert.equal(app.ui("settings-apply").disabled, false, "Failed settings remain retryable");
  await app.ui("settings-close").fire("click");

  app.setCamera({ ...disconnected, error: "カメラへのアクセス権限がありません。権限を確認してください。" });
  assert.equal(app.ui("camera-canvas").hidden, true);
  assert.equal(app.ui("scan-button").disabled, true);
  assert.match(app.ui("camera-empty-message").textContent, /アクセス権限/);
  app.setCamera({ ...connected, gain_min: null, gain_max: null, min_exposure_us: null });
  assert.equal(app.ui("gain-input").disabled, true);
  assert.equal(app.ui("manual-mode").disabled, true);
  assert.equal(app.ui("scan-button").disabled, true);
  await app.frame();
  assert.equal(app.ui("scan-button").disabled, false);
  app.setCamera({ ...connected, error: "このカメラでは一部の設定を適用できません。" });
  assert.equal(app.ui("camera-warning").hidden, false, "Asynchronous settings errors stay visible while preview is live");
  assert.match(app.ui("settings-error").textContent, /設定を適用できません/);
  app.setCamera({ ...connected });
  assert.equal(app.ui("camera-warning").hidden, true);
  assert.equal(app.ui("settings-error").hidden, true);
  app.close();
  await settle();
  assert.equal(app.removed.length, 3);
  assert.equal(app.intervals.size, 0);
  assert.equal(app.timers.size, 0);
  assert.equal(app.calls.at(-1).action, "shutdown");

  const dark = harness();
  await settle();
  dark.setBrightness(20);
  await dark.frame();
  dark.advance(1100);
  await dark.frame();
  assert.equal(dark.ui("dark-banner").hidden, false);
  await dark.ui("brightness-priority").fire("click");
  assert.deepEqual(dark.calls.at(-1), { action: "set_camera", exposure_mode: "auto" });
  dark.close();

  const unready = harness({ ocrReady: false });
  await settle();
  await unready.frame();
  assert.equal(unready.ui("scan-button").disabled, true);
  assert.match(unready.ui("scan-help").textContent, /起動し直してください/);
  unready.close();

  const recovery = harness({ attached: false });
  await settle();
  recovery.status().devices = [{ id: "usb:test", label: "再接続テストカメラ" }];
  recovery.handlers.start_camera = () => { recovery.status().camera = { ...connected }; return structuredClone(recovery.status()); };
  await recovery.tick(7000);
  assert.equal(recovery.calls.filter((call) => call.action === "start_camera").length, 2, "Attaching a camera retries connection without user intervention");
  await recovery.frame();
  assert.equal(recovery.ui("scan-button").disabled, false);
  let releaseStatus;
  recovery.handlers.status = () => new Promise((resolve) => { releaseStatus = resolve; });
  const staleStatus = recovery.tick(4001);
  await settle();
  recovery.setCamera({ ...connected, error: "新しいカメラ状態のテスト" });
  releaseStatus({ ...structuredClone(recovery.status()), camera: { ...disconnected } });
  await staleStatus;
  assert.equal(recovery.ui("camera-canvas").hidden, false, "A late status snapshot must not overwrite newer camera events");
  recovery.close();

  const cameraA = { id: "usb:A", label: "カメラA" };
  const cameraB = { id: "usb:B", label: "カメラB" };
  for (const automatic of [false, true]) {
    const selection = harness({ devices: automatic ? [cameraB, cameraA] : [cameraA, cameraB] });
    await settle();
    if (!automatic) {
      selection.ui("camera-select").value = cameraB.id;
      await selection.ui("camera-select").fire("change");
      await selection.ui("camera-connect").fire("click");
    }
    assert.equal(selection.calls.at(-1).device, cameraB.id);
    await selection.frame();
    selection.status().devices = [cameraA];
    selection.setCamera({ ...disconnected, error: "カメラが抜かれました。" });
    await selection.tick(7000);
    assert.deepEqual(selection.calls.at(-1), { action: "start_camera", device: cameraB.id }, "Automatic retry must retain the exact selected camera, including the initial automatic selection");
    assert.equal(selection.ui("scan-button").disabled, true);
    assert.match(selection.ui("camera-empty-message").textContent, /カメラB.*見つかりません/);
    assert.match(selection.ui("camera-empty-message").textContent, /カメラ設定/);
    assert.equal(selection.ui("camera-select").value, cameraB.id, "An unplugged selection stays visible");
    await selection.ui("camera-retry").fire("click");
    assert.equal(selection.calls.at(-1).device, cameraB.id, "Manual retry must also retain B while A is available");
    selection.ui("camera-select").value = cameraA.id;
    await selection.ui("camera-select").fire("change");
    assert.equal(selection.ui("camera-connect").disabled, false);
    await selection.ui("camera-connect").fire("click");
    assert.equal(selection.calls.at(-1).device, cameraA.id, "The owner can explicitly choose another camera");
    await selection.frame();
    assert.equal(selection.ui("scan-button").disabled, false);
    selection.close();
  }

  const sessions = harness({ deferDecode: true });
  await settle();
  await sessions.frame(); // A is decoding when B becomes connected.
  const beforeSwitch = sessions.counts();
  sessions.setCamera({ ...connected, device: cameraB.id, session_id: "session-B" });
  assert(sessions.counts().clearCount > beforeSwitch.clearCount, "A's old canvas pixels are cleared on a session change");
  await sessions.frame("session-A");
  sessions.emit("camera_frame", { jpeg: "YQ==" });
  sessions.decodes.shift()();
  await settle();
  assert.equal(sessions.counts().drawCount, 0, "A finishing decode after B state must not draw");
  assert.equal(sessions.ui("scan-button").disabled, true, "Late A cannot enable capture for B");
  assert.equal(sessions.ui("camera-canvas").hidden, true);
  assert.equal(sessions.rafs.size, 0, "Late A or missing identity must not queue a decode");
  await sessions.frame();
  sessions.decodes.shift()();
  await settle();
  assert.equal(sessions.ui("scan-button").disabled, false, "An actual B frame may enable capture");
  sessions.setCamera({ ...connected, device: cameraB.id, session_id: "session-B-reopened" });
  const beforeStale = sessions.counts();
  await sessions.frame("session-B");
  sessions.emit("camera_frame", { jpeg: "YQ==" });
  assert.equal(sessions.counts().decodedCount, beforeStale.decodedCount, "Old or missing sessions are rejected before decode, even for the same device");
  assert.equal(sessions.ui("scan-button").disabled, true);
  await sessions.frame();
  sessions.decodes.shift()();
  await settle();
  assert.equal(sessions.ui("scan-button").disabled, false);
  sessions.setCamera({ ...disconnected });
  assert.equal(sessions.ui("camera-canvas").hidden, true);
  assert.equal(sessions.ui("scan-button").disabled, true);
  await sessions.frame("session-B-reopened");
  sessions.setCamera({ ...connected, session_id: null });
  sessions.emit("camera_frame", { jpeg: "YQ==" });
  await sessions.raf();
  assert.equal(sessions.ui("scan-button").disabled, true, "Connected state without a valid session must never enable capture");
  sessions.close();

  const lostResult = harness();
  await settle();
  await lostResult.frame();
  await lostResult.ui("scan-button").fire("click");
  await lostResult.tick(9000);
  assert.equal(lostResult.ui("result-busy").hidden, false, "An event just behind an idle status has a grace period");
  await lostResult.tick(4001);
  assert.match(lostResult.ui("result-message").textContent, /結果を受信できません/);
  assert.equal(lostResult.ui("result-retry").hidden, false, "A finished job with a lost result is retryable");
  lostResult.close();

  const bounded = harness({ deferDecode: true });
  await settle();
  await bounded.frame();
  for (let i = 0; i < 100; i++) bounded.emit("camera_frame", { jpeg: "YQ==", session_id: connected.session_id });
  assert.equal(bounded.counts().decodedCount, 1, "Only one frame decode is in flight");
  assert.equal(bounded.rafs.size, 0);
  bounded.decodes.shift()();
  await settle();
  assert.equal(bounded.rafs.size, 1, "Only the most recent waiting frame is scheduled");
  await bounded.raf();
  assert.equal(bounded.counts().decodedCount, 2);
  bounded.close();
  bounded.decodes.shift()();
  await settle();
  assert.equal(bounded.counts().closedCount, 2, "Bitmaps are closed, including after shutdown");
  assert.equal(bounded.rafs.size, 0);
  console.log("PASS frontend: listener ordering, no bridge/camera, live preview, busy, capture ids, matched/no-match/errors, settings bounds/debounce, darkness, OCR readiness, exact-device auto/manual retries, missing/stale session rejection before/after decode, same-device reopen, stale status/lost result, bounded frames, shutdown (mock DOM + mock Tauri only)");
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
