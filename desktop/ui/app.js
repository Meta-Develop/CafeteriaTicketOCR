"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ids = ["ocr-status", "ocr-status-text", "app-error", "app-error-text", "app-retry", "camera-status-text", "camera-canvas", "live-label", "camera-empty", "camera-empty-title", "camera-empty-message", "camera-retry", "device-label", "camera-meta", "live-status", "ocr-metrics", "live-toggle", "dark-banner", "current-match", "ocr-text", "settings-panel", "camera-select", "camera-connect", "exposure-options", "manual-mode", "exposure-input", "exposure-value", "exposure-bounds", "gain-input", "gain-value", "native-controls", "settings-readback", "settings-error", "confirm-input", "gap-input", "roi-input", "scale-input", "result-time", "product-name", "product-price", "price-number", "result-message", "detection-history", "history-count", "history-empty"];
  const ui = Object.fromEntries(ids.map((id) => [id, $(id)]));
  const modes = [...document.querySelectorAll('input[name="exposure-mode"]')];
  const state = { camera: { connected: false, session_id: null, controls: {} }, devices: [],
    ocr: { ready: false }, live: { enabled: true, running: false },
    bridgeReady: false, disposed: false, starting: false, checking: false, toggling: false,
    frameReady: false, lastFrameAt: 0, cameraIssue: "", cameraRevision: 0, liveRevision: 0,
    selectedDevice: null, selectedLabel: "USBカメラ", selectionEdited: false,
    lastConnectAttempt: -Infinity, nextStatusAt: Infinity, requestedMode: "short" };
  const context = ui["camera-canvas"].getContext("2d", { alpha: false });
  const finite = (v) => typeof v === "number" && Number.isFinite(v);
  const text = (id, value) => { if (ui[id].textContent !== value) ui[id].textContent = value; };
  const show = (id, visible) => { ui[id].hidden = !visible; };
  const messageOf = (e) => typeof e === "string" ? e : e?.message || "処理を完了できませんでした。接続を確認してください。";
  const formatNumber = (v) => new Intl.NumberFormat("ja-JP").format(v);
  const activeSession = () => state.camera.connected && typeof state.camera.session_id === "string" && !!state.camera.session_id;
  const frameMatches = (frame) => activeSession() && frame?.session_id === state.camera.session_id;
  let unlisten = [], timer = null, controlTimer = null, latestFrame = null, frameRaf = null;
  let decoding = false, frameGeneration = 0, fallbackURL = null, bootstrapPending = false;
  let deviceKey = null, nativeKey = null, pendingControls = {}, sendingControls = false;
  const nativeInputs = new Map();
  const history = [], captureIds = new Set();
  let detectionCount = 0;

  async function request(payload) {
    if (state.disposed || !state.bridgeReady) throw new Error("アプリの接続が準備できていません。");
    return window.__TAURI__.core.invoke("backend_request", { request: payload });
  }
  function appError(error) { text("app-error-text", messageOf(error)); show("app-error", true); }
  function settingsError(error) { text("settings-error", messageOf(error)); show("settings-error", true); }
  function friendlyLabel(device, index = 0) {
    const label = String(device?.label || "").replace(/\s*\((?:\/dev\/|usb-)[^)]*\)/g, "").trim();
    return !label || /(^\/dev\/|\\|^COM\d+$)/i.test(label) ? `USBカメラ ${index + 1}` : label;
  }
  function invalidateFrame() {
    frameGeneration++; latestFrame = null; state.frameReady = false;
    context?.clearRect(0, 0, ui["camera-canvas"].width, ui["camera-canvas"].height);
    show("camera-canvas", false);
    if (frameRaf !== null) cancelAnimationFrame(frameRaf);
    frameRaf = null;
  }
  function updateDevices() {
    const key = JSON.stringify([state.devices, state.selectedDevice]);
    if (key === deviceKey) return;
    deviceKey = key;
    const selected = state.selectionEdited ? ui["camera-select"].value : state.selectedDevice;
    ui["camera-select"].replaceChildren();
    const items = [...state.devices];
    if (state.selectedDevice && !items.some((d) => d.id === state.selectedDevice)) items.unshift({ id: state.selectedDevice, label: `${state.selectedLabel}（未接続）` });
    if (!items.length) items.push({ id: "", label: "接続されたカメラがありません" });
    items.forEach((device, index) => { const option = document.createElement("option"); option.value = device.id; option.textContent = friendlyLabel(device, index); ui["camera-select"].append(option); });
    if (selected) ui["camera-select"].value = selected;
  }
  function updateCamera(camera) {
    if (!camera || typeof camera.connected !== "boolean") return;
    const old = state.camera;
    if (!camera.connected || camera.session_id !== old.session_id || camera.device !== old.device) {
      invalidateFrame();
      pendingControls = {};
      clearTimeout(controlTimer); controlTimer = null;
      nativeKey = null;
    }
    state.camera = { ...camera, session_id: camera.connected ? camera.session_id : null, controls: camera.controls || {} };
    if (!state.selectedDevice && camera.device) state.selectedDevice = camera.device;
    if (camera.connected && (!old.connected || camera.session_id !== old.session_id)) state.lastFrameAt = performance.now();
    updateDevices(); syncControls(); render();
  }
  function applyLive(live) {
    if (!live || typeof live.enabled !== "boolean") return;
    state.live = live;
    for (const [id, key] of [["confirm-input", "confirm_passes"], ["gap-input", "dedup_gap"], ["roi-input", "roi"], ["scale-input", "scale"]]) {
      if (finite(live[key])) ui[id].value = String(live[key]);
    }
    render();
  }
  async function refreshStatus() {
    if (state.checking || state.disposed) return;
    state.checking = true;
    const revision = state.cameraRevision, liveRevision = state.liveRevision;
    try {
      const status = await request({ action: "status" });
      if (!status?.camera || !status.ocr || !Array.isArray(status.devices)) throw new Error("カメラの状態を確認できません。");
      if (state.disposed) return;
      state.devices = status.devices.filter((d) => typeof d.id === "string"); state.ocr = status.ocr;
      updateDevices();
      if (revision === state.cameraRevision) updateCamera(status.camera);
      if (liveRevision === state.liveRevision) applyLive(status.live);
      render(); show("app-error", false);
    } finally { state.checking = false; state.nextStatusAt = performance.now() + 4000; }
  }
  async function connect(device) {
    if (!state.bridgeReady || state.starting || state.disposed) return;
    const target = device || state.selectedDevice || state.devices[0]?.id;
    if (target) {
      state.selectedDevice = target;
      const index = state.devices.findIndex((d) => d.id === target);
      if (index >= 0) state.selectedLabel = friendlyLabel(state.devices[index], index);
    }
    state.starting = true; state.cameraIssue = ""; state.lastConnectAttempt = performance.now();
    invalidateFrame(); render();
    try {
      const revision = state.cameraRevision;
      const status = await request({ action: "start_camera", ...(target ? { device: target } : {}) });
      if (!state.disposed && revision === state.cameraRevision) updateCamera(status.camera);
    } catch (error) { state.cameraIssue = messageOf(error); }
    finally { state.starting = false; render(); }
  }
  function render() {
    if (state.disposed) return;
    const camera = state.camera, live = activeSession() && state.frameReady;
    const missing = state.selectedDevice && !state.devices.some((d) => d.id === state.selectedDevice);
    const issue = missing ? `選択したカメラ「${state.selectedLabel}」が見つかりません。USB接続を確認するか、カメラ設定で選び直してください。` : state.cameraIssue || camera.error;
    ui["ocr-status"].className = `status-pill ${state.ocr.ready ? "ready" : "error"}`;
    text("ocr-status-text", state.ocr.ready ? "認識の準備完了" : state.ocr.error || "認識を準備中");
    text("camera-status-text", live ? "カメラ接続中" : state.starting ? "接続中" : "カメラ未接続");
    show("camera-canvas", live); show("live-label", live); show("camera-empty", !live);
    text("camera-empty-title", issue ? "カメラの接続を確認してください" : "カメラの映像を準備中");
    text("camera-empty-message", issue || "USBカメラを接続すると自動で映像を表示します。");
    show("camera-retry", state.bridgeReady && !state.starting && !live);
    text("device-label", state.selectedLabel);
    text("camera-meta", live ? `${camera.width} × ${camera.height} · ${camera.stream_fps || 0} fps` : "接続待ち");
    text("live-status", !state.live.enabled ? "自動認識を一時停止中" : state.live.error || (!state.ocr.ready ? state.ocr.error || "認識を準備中" : !live ? "カメラ待ち" : "自動認識中 · 食券をかざしてください"));
    text("live-toggle", state.live.enabled ? "認識を一時停止" : "認識を再開");
    ui["live-toggle"].disabled = !state.bridgeReady || state.toggling;
    ui["live-toggle"].setAttribute("aria-pressed", String(state.live.enabled));
    ui["camera-select"].disabled = !state.bridgeReady || state.starting;
    ui["camera-connect"].disabled = !state.bridgeReady || state.starting || !ui["camera-select"].value;
    ui["exposure-options"].disabled = !camera.connected;
    show("dark-banner", live && finite(camera.brightness) && camera.brightness < 45);
    if (camera.error) settingsError(camera.error);
  }
  const labels = { brightness: "明るさ", contrast: "コントラスト", saturation: "彩度", sharpness: "鮮明さ", hue: "色相", focus_absolute: "フォーカス", focus_automatic_continuous: "オートフォーカス", focus_auto: "オートフォーカス", white_balance_automatic: "自動ホワイトバランス", white_balance_temperature_auto: "自動ホワイトバランス", white_balance_temperature: "色温度", power_line_frequency: "電源周波数", backlight_compensation: "逆光補正", gamma: "ガンマ" };
  const excluded = new Set(["auto_exposure", "exposure_auto", "exposure_time_absolute", "exposure_absolute", "gain"]);
  const available = (c) => c && finite(c.min) && finite(c.max) && finite(c.value);
  const writable = (c) => available(c) && !/inactive|disabled|read-only/.test(c.flags || "");
  function syncControls() {
    const camera = state.camera, controls = camera.controls;
    const exposure = controls.exposure_time_absolute || controls.exposure_absolute, gain = controls.gain;
    const editing = sendingControls || Object.keys(pendingControls).length > 0;
    if (!editing) {
      if (camera.auto_exposure) state.requestedMode = "auto";
      else if (state.requestedMode === "auto") state.requestedMode = "manual";
      modes.forEach((mode) => { mode.checked = mode.value === state.requestedMode; });
    }
    function slider(id, control, multiplier = 1) {
      ui[id].disabled = !camera.connected || !writable(control);
      if (available(control)) {
        ui[id].min = String(control.min * multiplier); ui[id].max = String(control.max * multiplier); ui[id].step = String(control.step * multiplier);
        if (!editing) ui[id].value = String(control.value * multiplier);
      }
    }
    slider("exposure-input", exposure, 100); slider("gain-input", gain);
    ui["manual-mode"].disabled = !available(exposure);
    text("exposure-value", finite(camera.exposure_us) ? `${formatNumber(camera.exposure_us)} µs` : "非対応");
    text("exposure-bounds", available(exposure) ? `${formatNumber(exposure.min * 100)}–${formatNumber(exposure.max * 100)} µs · 刻み ${exposure.step * 100}` : "露光時間の調整は非対応");
    text("gain-value", finite(camera.gain) ? formatNumber(camera.gain) : "非対応");
    text("settings-readback", `現在値: 露光 ${finite(camera.exposure_us) ? camera.exposure_us + " µs" : "—"} · ゲイン ${camera.gain ?? "—"}\n映像 ${camera.stream_fps || 0} fps · 明るさ ${camera.brightness ?? "—"} / 255`);
    const entries = Object.entries(controls).filter(([name, c]) => !excluded.has(name) && available(c) && ["int", "bool", "menu", "intmenu"].includes(c.kind));
    const key = JSON.stringify(entries.map(([name, c]) => [name, c.kind, c.min, c.max, c.step, c.choices]));
    if (key !== nativeKey) {
      nativeKey = key; nativeInputs.clear(); ui["native-controls"].replaceChildren();
      entries.forEach(([name, c]) => {
        const label = document.createElement("label"), output = document.createElement("output");
        const input = document.createElement(c.choices && Object.keys(c.choices).length ? "select" : "input");
        input.id = `control-${name}`; label.setAttribute("for", input.id);
        label.textContent = labels[name] || name.replace(/_/g, " "); label.append(output);
        if (input.tagName === "SELECT") Object.entries(c.choices).forEach(([value, title]) => { const option = document.createElement("option"); option.value = value; option.textContent = title; input.append(option); });
        else { input.type = c.kind === "bool" ? "checkbox" : "range"; input.min = String(c.min); input.max = String(c.max); input.step = String(c.step); }
        input.addEventListener(input.type === "range" ? "input" : "change", () => {
          queueControls({ controls: { [name]: input.type === "checkbox" ? Number(input.checked) : Number(input.value) } });
        });
        ui["native-controls"].append(label); ui["native-controls"].append(input); nativeInputs.set(name, { input, output });
      });
    }
    entries.forEach(([name, c]) => {
      const { input, output } = nativeInputs.get(name);
      input.disabled = !camera.connected || !writable(c);
      output.textContent = c.choices?.[c.value] || (c.kind === "bool" ? c.value ? "オン" : "オフ" : String(c.value));
      if (!editing) { input.value = String(c.value); input.checked = !!c.value; }
    });
  }
  function queueControls(change) {
    if (!state.camera.connected || state.disposed) return;
    pendingControls = { ...pendingControls, ...change, ...(change.controls ? { controls: { ...pendingControls.controls, ...change.controls } } : {}) };
    if (controlTimer === null) controlTimer = setTimeout(flushControls, 75);
  }
  async function flushControls() {
    controlTimer = null;
    if (sendingControls || !Object.keys(pendingControls).length || !state.camera.connected || state.disposed) return;
    const settings = pendingControls; pendingControls = {}; sendingControls = true;
    try { await request({ action: "set_camera", ...settings }); show("settings-error", false); }
    catch (error) { settingsError(error); }
    finally { sendingControls = false; if (Object.keys(pendingControls).length && controlTimer === null) controlTimer = setTimeout(flushControls, 75); }
  }
  async function setLive(settings) {
    const revision = ++state.liveRevision;
    try { const live = await request({ action: "set_live", ...settings }); if (!state.disposed && revision === state.liveRevision) applyLive(live); }
    catch (error) { appError(error); }
  }
  function onLive(result) {
    if (state.disposed || !state.live.enabled || !frameMatches(result)) return;
    if (!["matched", "no_match"].includes(result.status)) return;
    text("current-match", result.status === "matched" ? `${result.name} · ${finite(result.price) ? formatNumber(result.price) + "円" : "価格未登録"} · スコア ${finite(result.score) ? result.score.toFixed(2) : "—"}` : "一致するメニューはありません");
    text("ocr-text", result.text || "読み取った文字はありません。");
    text("ocr-metrics", `認識速度 ${finite(result.passes_per_second) ? result.passes_per_second.toFixed(1) : "—"} 回/秒 · 認識時間 ${result.ocr_latency_ms ?? "—"} ms`);
  }
  function onTicket(result) {
    if (state.disposed || typeof result?.capture_id !== "string" || captureIds.has(result.capture_id)) return;
    if (result.status !== "matched") { appError(result.message || "検出結果を保存できませんでした。"); return; }
    if (typeof result.name !== "string") return;
    const timestamp = new Intl.DateTimeFormat("ja-JP", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(result.timestamp_ms || Date.now()));
    captureIds.add(result.capture_id); history.unshift({ ...result, timestamp }); detectionCount++;
    if (history.length > 200) captureIds.delete(history.pop().capture_id);
    text("product-name", result.name); text("result-time", timestamp);
    show("product-price", finite(result.price)); text("price-number", finite(result.price) ? formatNumber(result.price) : "");
    text("result-message", "検出した食券を保存しました。"); text("history-count", `${detectionCount}件`); show("history-empty", false);
    ui["detection-history"].replaceChildren();
    history.forEach((ticket) => {
      const row = document.createElement("li"), when = document.createElement("time"), detail = document.createElement("span");
      when.textContent = ticket.timestamp; detail.textContent = `${ticket.name} · ${finite(ticket.price) ? formatNumber(ticket.price) + "円" : "価格未登録"}`;
      row.append(when); row.append(detail); ui["detection-history"].append(row);
    });
  }
  function scheduleFrame() {
    if (!state.disposed && frameRaf === null && !decoding && latestFrame) frameRaf = requestAnimationFrame(drawLatestFrame);
  }

  function decodeJPEG(blob) {
    if (typeof createImageBitmap === "function") return createImageBitmap(blob);
    return new Promise((resolve, reject) => {
      const image = new Image();
      const url = URL.createObjectURL(blob);
      fallbackURL = url;
      const done = () => { URL.revokeObjectURL(url); if (fallbackURL === url) fallbackURL = null; };
      image.onload = () => { done(); resolve(image); };
      image.onerror = () => { done(); reject(new Error("カメラの映像を読み込めませんでした。再接続してください。")); };
      image.src = url;
    });
  }

  async function drawLatestFrame() {
    frameRaf = null;
    if (!latestFrame || state.disposed) return;
    const frame = latestFrame;
    const generation = frameGeneration;
    latestFrame = null;
    if (!frameMatches(frame)) return;
    decoding = true;
    let bitmap;
    try {
      const binary = atob(frame.jpeg);
      const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
      bitmap = await decodeJPEG(new Blob([bytes], { type: "image/jpeg" }));
      if (state.disposed || generation !== frameGeneration || !frameMatches(frame)) return;
      if (!context) throw new Error("カメラの映像を表示できません。アプリを起動し直してください。");
      const width = bitmap.width || bitmap.naturalWidth;
      const height = bitmap.height || bitmap.naturalHeight;
      if (!width || !height || width > 8192 || height > 8192) throw new Error("カメラから正しい映像を受信できませんでした。再接続してください。");
      if (ui["camera-canvas"].width !== width) ui["camera-canvas"].width = width;
      if (ui["camera-canvas"].height !== height) ui["camera-canvas"].height = height;
      context.drawImage(bitmap, 0, 0, width, height);
      const needsRender = !state.frameReady || !!state.cameraIssue;
      state.lastFrameAt = performance.now();
      state.frameReady = true;
      state.cameraIssue = "";
      if (needsRender) render();
    } catch (error) {
      if (!state.disposed && generation === frameGeneration) {
        state.frameReady = false;
        state.cameraIssue = messageOf(error);
        render();
      }
    } finally {
      bitmap?.close?.();
      decoding = false;
      scheduleFrame();
    }
  }

  function onFrame(frame) {
    if (state.disposed || !frameMatches(frame) || typeof frame.jpeg !== "string" || !frame.jpeg.length || frame.jpeg.length > 4000000) return;
    latestFrame = frame;
    scheduleFrame();
  }

  async function tick() {
    if (state.disposed || !state.bridgeReady) return;
    const now = performance.now();
    if (state.camera.connected && now - state.lastFrameAt > 5000 && !state.starting) { invalidateFrame(); state.cameraIssue = "映像の受信が止まりました。USB接続を確認してください。"; render(); }
    if (now < state.nextStatusAt || state.checking || state.starting) return;
    try { await refreshStatus(); if (!state.camera.connected && state.devices.length && now - state.lastConnectAttempt >= 6000) await connect(); }
    catch (error) { appError(error); }
  }
  async function bootstrap() {
    if (bootstrapPending || state.disposed) return;
    bootstrapPending = true;
    try {
      if (!window.__TAURI__?.core?.invoke || !window.__TAURI__?.event?.listen) throw new Error("食券メニュー認識のデスクトップアプリから起動してください。");
      if (!state.bridgeReady) {
        const subscriptions = await Promise.allSettled([
          window.__TAURI__.event.listen("camera_frame", (event) => onFrame(event.payload)),
          window.__TAURI__.event.listen("camera_state", (event) => { if (!state.disposed) { state.cameraRevision++; updateCamera(event.payload); } }),
          window.__TAURI__.event.listen("live_recognition", (event) => onLive(event.payload)),
          window.__TAURI__.event.listen("ticket_detected", (event) => onTicket(event.payload)),
          window.__TAURI__.event.listen("live_state", (event) => { if (!state.disposed) { state.liveRevision++; applyLive(event.payload); } }),
        ]);
        const registered = subscriptions.filter((s) => s.status === "fulfilled").map((s) => s.value);
        const failed = subscriptions.find((s) => s.status === "rejected");
        if (state.disposed || failed) { registered.forEach((remove) => remove()); if (failed) throw failed.reason; return; }
        unlisten = registered; state.bridgeReady = true;
      }
      await refreshStatus(); if (!state.camera.connected) await connect();
      if (!state.disposed && timer === null) timer = setInterval(tick, 1000);
    } catch (error) { appError(error); }
    finally { bootstrapPending = false; render(); }
  }
  function dispose() {
    if (state.disposed) return;
    state.disposed = true; invalidateFrame(); clearInterval(timer); clearTimeout(controlTimer);
    unlisten.forEach((remove) => { try { remove(); } catch { /* Native teardown can precede pagehide. */ } }); unlisten = [];
    if (fallbackURL) URL.revokeObjectURL(fallbackURL);
    if (state.bridgeReady) window.__TAURI__.core.invoke("backend_request", { request: { action: "shutdown" } }).catch(() => {});
  }
  ui["app-retry"].addEventListener("click", bootstrap);
  ui["camera-retry"].addEventListener("click", () => connect());
  ui["camera-select"].addEventListener("change", () => { state.selectionEdited = true; render(); });
  ui["camera-connect"].addEventListener("click", async () => { state.selectionEdited = false; await connect(ui["camera-select"].value); });
  ui["live-toggle"].addEventListener("click", async () => { state.toggling = true; render(); await setLive({ enabled: !state.live.enabled }); state.toggling = false; render(); });
  modes.forEach((input) => input.addEventListener("change", () => {
    state.requestedMode = input.value;
    queueControls({ exposure_mode: input.value, ...(input.value === "manual" ? { exposure_us: Number(ui["exposure-input"].value) } : {}) });
  }));
  ui["exposure-input"].addEventListener("input", () => { state.requestedMode = "manual"; modes.forEach((mode) => { mode.checked = mode.value === "manual"; }); queueControls({ exposure_mode: "manual", exposure_us: Number(ui["exposure-input"].value) }); });
  ui["gain-input"].addEventListener("input", () => queueControls({ gain: Number(ui["gain-input"].value) }));
  for (const [id, key] of [["confirm-input", "confirm_passes"], ["gap-input", "dedup_gap"], ["roi-input", "roi"], ["scale-input", "scale"]]) ui[id].addEventListener("change", () => setLive({ [key]: Number(ui[id].value) }));
  window.addEventListener("pagehide", dispose);
  render(); bootstrap();
})();
