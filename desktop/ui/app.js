"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ids = ["ocr-status", "ocr-status-text", "settings-open", "app-error", "app-error-text", "app-retry", "camera-status", "camera-status-text", "camera-canvas", "live-label", "frame-guide", "camera-empty", "camera-empty-title", "camera-empty-message", "camera-retry", "preview-note", "device-label", "camera-meta", "camera-warning", "camera-warning-settings", "dark-banner", "dark-message", "brightness-priority", "scan-button", "scan-label", "scan-icon", "scan-help", "result-panel", "result-time", "result-empty", "result-busy", "result-data", "result-badge", "result-symbol", "result-status", "product-name", "product-price", "price-number", "result-message", "result-retry", "result-elapsed", "ocr-details", "ocr-text", "settings-dialog", "settings-close", "camera-select", "camera-connect", "settings-camera-note", "exposure-options", "manual-mode", "manual-exposure", "exposure-input", "exposure-bounds", "gain-input", "gain-value", "gain-min", "gain-max", "gain-hint", "settings-readback", "settings-error", "settings-feedback", "settings-apply"];
  const ui = Object.fromEntries(ids.map((id) => [id, $(id)]));
  const modes = [...document.querySelectorAll('input[name="exposure-mode"]')];
  const cameraDefaults = { connected: false, device: null, session_id: null, width: 0, height: 0, exposure_us: null, min_exposure_us: null, gain: null, gain_min: null, gain_max: null, auto_exposure: false, error: null };
  const state = {
    camera: { ...cameraDefaults }, devices: [], ocr: { ready: false, error: null },
    bridgeReady: false, disposed: false, starting: false, checking: false,
    backendBusy: false, scanning: false, accepting: false, captureId: null,
    earlyResults: new Map(), result: null, resultTime: "", cameraIssue: "", scanIdleSince: null,
    frameReady: false, lastFrameAt: 0, cameraRevision: 0, recognitionRevision: 0,
    lastConnectAttempt: -Infinity, nextStatusAt: Infinity, dirty: false,
    saving: false, brightnessPending: false, requestedMode: "short", dark: false,
    deviceSelectionEdited: false, selectedDevice: null, selectedDeviceLabel: "USBカメラ",
  };
  const context = ui["camera-canvas"].getContext("2d", { alpha: false });
  const lightCanvas = document.createElement("canvas");
  lightCanvas.width = 32;
  lightCanvas.height = 18;
  const lightContext = lightCanvas.getContext("2d", { willReadFrequently: true });
  let unlisten = [];
  let timer = null;
  let gainTimer = null;
  let latestFrame = null;
  let frameRaf = null;
  let decoding = false;
  let frameGeneration = 0;
  let fallbackURL = null;
  let lastLightCheck = -Infinity;
  let darkSamples = 0;
  let bootstrapPending = false;
  let appRetryAction = null;
  let deviceListKey = null;

  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const messageOf = (error) => typeof error === "string" ? error : error?.message || "処理を完了できませんでした。もう一度お試しください。";
  const text = (id, value) => { if (ui[id].textContent !== value) ui[id].textContent = value; };
  const show = (id, visible) => { if (ui[id].hidden === !!visible) ui[id].hidden = !visible; };
  const currentMode = () => modes.find((input) => input.checked)?.value || "short";
  const isBusy = () => state.scanning || state.backendBusy;
  const activeSession = () => state.camera.connected && typeof state.camera.session_id === "string" && state.camera.session_id.length > 0;
  const frameMatches = (frame) => activeSession() && frame?.session_id === state.camera.session_id;
  const canScan = () => state.bridgeReady && activeSession() && state.frameReady && state.ocr.ready && !isBusy() && !state.disposed;
  const formatNumber = (value) => new Intl.NumberFormat("ja-JP").format(value);

  function friendlyLabel(device, index = 0) {
    const label = String(device?.label || "").replace(/\s*\((?:\/dev\/|usb-)[^)]*\)/g, "").trim();
    // Device ids stay in option values; machine paths never become visible labels.
    return !label || /(^\/dev\/|\\|^COM\d+$)/i.test(label) ? `USBカメラ ${index + 1}` : label;
  }

  async function request(payload) {
    if (state.disposed || !state.bridgeReady) throw new Error("アプリの接続が準備できていません。「再試行」を押してください。");
    return window.__TAURI__.core.invoke("backend_request", { request: payload });
  }

  function appError(error, retry = null) {
    appRetryAction = retry;
    text("app-error-text", messageOf(error));
    show("app-error", true);
  }

  function settingsError(error) {
    text("settings-error", messageOf(error));
    show("settings-error", true);
    text("settings-feedback", "");
  }

  function invalidateFrame() {
    frameGeneration++;
    latestFrame = null;
    state.frameReady = false;
    state.dark = false;
    darkSamples = 0;
    context?.clearRect(0, 0, ui["camera-canvas"].width, ui["camera-canvas"].height);
    show("camera-canvas", false);
    if (frameRaf !== null) cancelAnimationFrame(frameRaf);
    frameRaf = null;
  }

  function updateCamera(camera) {
    if (!camera || typeof camera.connected !== "boolean") return;
    const old = state.camera;
    if (!camera.connected || camera.session_id !== old.session_id || camera.device !== old.device) invalidateFrame();
    state.camera = { ...cameraDefaults, ...camera };
    if (!camera.connected) state.camera.session_id = null;
    if (!state.selectedDevice && camera.device) rememberDevice(camera.device);
    if (camera.connected && camera.error && (camera.error !== old.error || !old.connected)) settingsError(camera.error);
    else if (!camera.error && old.error && ui["settings-error"].textContent === old.error) show("settings-error", false);
    updateDevices();
    if (camera.connected && (!old.connected || camera.session_id !== old.session_id)) state.lastFrameAt = performance.now();
    if (camera.connected && state.frameReady) state.cameraIssue = "";
    syncSettings();
    render();
  }

  function applyStatus(status, revision, recognitionRevision) {
    if (!status || !status.camera || !status.ocr || !Array.isArray(status.devices)) {
      throw new Error("カメラの状態を確認できませんでした。もう一度お試しください。");
    }
    state.devices = status.devices.filter((device) => typeof device.id === "string");
    state.ocr = status.ocr;
    if (recognitionRevision === state.recognitionRevision) {
      state.backendBusy = status.busy === true;
      if (state.scanning && !state.accepting && !state.backendBusy) {
        // Confirm idle twice: the event can arrive just after the status reply.
        if (state.scanIdleSince === null) state.scanIdleSince = performance.now();
        else if (performance.now() - state.scanIdleSince >= 4000) finishRecognition({ status: "error", message: "読み取り結果を受信できませんでした。もう一度撮影してください。", text: "" });
      } else {
        state.scanIdleSince = null;
      }
    }
    updateDevices();
    // A camera event arriving while status is in flight is newer than that snapshot.
    if (revision === state.cameraRevision) updateCamera(status.camera);
    else render();
    renderResult();
    if (!appRetryAction) show("app-error", false);
  }

  async function refreshStatus() {
    if (state.checking || state.disposed) return;
    state.checking = true;
    const revision = state.cameraRevision;
    const recognitionRevision = state.recognitionRevision;
    try {
      const status = await request({ action: "status" });
      if (!state.disposed) applyStatus(status, revision, recognitionRevision);
      return status;
    } finally {
      state.checking = false;
      state.nextStatusAt = performance.now() + 4000;
    }
  }

  function rememberDevice(id) {
    if (state.selectedDevice !== id) state.selectedDeviceLabel = "USBカメラ";
    state.selectedDevice = id;
    const index = state.devices.findIndex((device) => device.id === id);
    if (index >= 0) state.selectedDeviceLabel = friendlyLabel(state.devices[index], index);
    updateDevices();
  }

  function updateDevices() {
    const key = JSON.stringify([state.devices, state.selectedDevice]);
    if (key === deviceListKey) return;
    deviceListKey = key;
    const selected = state.deviceSelectionEdited ? ui["camera-select"].value : state.selectedDevice;
    ui["camera-select"].replaceChildren();
    if (state.selectedDevice && !state.devices.some((device) => device.id === state.selectedDevice)) {
      const option = document.createElement("option");
      option.value = state.selectedDevice;
      option.textContent = `${state.selectedDeviceLabel}（未接続）`;
      ui["camera-select"].append(option);
    }
    if (!state.devices.length && !state.selectedDevice) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "接続されたカメラがありません";
      ui["camera-select"].append(option);
    } else {
      state.devices.forEach((device, index) => {
        const option = document.createElement("option");
        option.value = device.id;
        option.textContent = friendlyLabel(device, index);
        ui["camera-select"].append(option);
      });
    }
    if (selected && ui["camera-select"].children.length) ui["camera-select"].value = selected;
  }

  async function connect(device) {
    if (!state.bridgeReady || state.starting || state.disposed) return;
    const target = device || state.selectedDevice || state.devices[0]?.id;
    if (target) rememberDevice(target);
    state.starting = true;
    state.lastConnectAttempt = performance.now();
    state.cameraIssue = "";
    invalidateFrame();
    state.camera = { ...state.camera, connected: false, session_id: null };
    render();
    try {
      const revision = state.cameraRevision;
      const recognitionRevision = state.recognitionRevision;
      const status = await request({ action: "start_camera", ...(target ? { device: target } : {}) });
      if (!state.disposed) applyStatus(status, revision, recognitionRevision);
    } catch (error) {
      state.cameraIssue = messageOf(error);
      if (ui["settings-dialog"].open) settingsError(error);
    } finally {
      state.starting = false;
      render();
    }
  }

  function render() {
    if (state.disposed) return;
    const camera = state.camera;
    const live = activeSession() && state.frameReady;
    const selectionMissing = state.selectedDevice && !state.devices.some((device) => device.id === state.selectedDevice);
    const cameraError = !camera.connected && selectionMissing ? `選択したカメラ「${state.selectedDeviceLabel}」が見つかりません。USB接続を確認してください。別のカメラを使う場合は「カメラ設定」で選び直してください。` : state.cameraIssue || camera.error;
    const waiting = state.starting || (camera.connected && !cameraError && !live);
    ui["ocr-status"].className = `status-pill ${state.ocr.ready ? "ready" : state.ocr.error ? "error" : ""}`;
    text("ocr-status-text", state.ocr.ready ? "認識の準備完了" : state.ocr.error ? "認識を準備できません" : "認識を準備中");
    ui["ocr-status"].title = state.ocr.error || "";
    ui["camera-status"].className = `camera-status ${live ? "connected" : cameraError ? "error" : ""}`;
    text("camera-status-text", live ? "カメラ接続中" : waiting ? "カメラを接続中" : "カメラ未接続");
    show("camera-canvas", live);
    show("live-label", live);
    show("frame-guide", live);
    show("preview-note", live);
    show("camera-empty", !live);
    text("camera-empty-title", cameraError ? (camera.connected ? "映像を確認できません" : "カメラに接続できません") : waiting ? "カメラの映像を準備中" : state.bridgeReady ? "USBカメラを接続してください" : "アプリの接続を確認しています");
    text("camera-empty-message", cameraError || (waiting ? "まもなくライブ映像を表示します。" : state.bridgeReady ? "USBカメラを接続すると、自動で映像を表示します。\n見つからない場合は、接続し直して再試行してください。" : "アプリの準備ができると、カメラに自動で接続します。"));
    show("camera-retry", state.bridgeReady && !waiting);
    ui["camera-retry"].disabled = state.starting;
    const deviceIndex = state.devices.findIndex((device) => device.id === camera.device);
    text("device-label", deviceIndex >= 0 ? friendlyLabel(state.devices[deviceIndex], deviceIndex) : "USBカメラ");
    text("camera-meta", live ? (camera.width > 0 && camera.height > 0 ? `${camera.width} × ${camera.height}` : "ライブ映像") : waiting ? "準備中" : "接続待ち");
    ui["scan-button"].disabled = !canScan();
    text("scan-label", isBusy() ? "認識しています…" : "撮影して認識");
    ui["scan-button"].setAttribute("aria-busy", String(isBusy()));
    text("scan-help", isBusy() ? "読み取り中も、カメラの映像は止まりません。" : !state.ocr.ready && state.ocr.error ? state.ocr.error : !state.ocr.ready ? "文字認識を準備しています。少しお待ちください。" : !live ? "カメラの映像が表示されると撮影できます。" : "食券の文字全体を写してから撮影してください。Enterキーでも撮影できます。");
    ui["result-retry"].disabled = !canScan();
    show("dark-banner", live && state.dark);
    show("camera-warning", live && !!camera.error);
    text("dark-message", camera.auto_exposure ? "食券に光が当たるよう、照明や置き方を調整してください。" : "明るさを優先すると、文字が見やすくなります。");
    text("brightness-priority", state.brightnessPending ? "調整中…" : camera.auto_exposure ? "カメラ設定" : "明るさを優先");
    ui["brightness-priority"].disabled = state.brightnessPending || !camera.connected;
    renderSettingsAvailability();
  }

  function renderResult() {
    const result = state.result;
    show("result-empty", !isBusy() && !result);
    show("result-busy", isBusy());
    show("result-data", !isBusy() && !!result);
    ui["result-panel"].setAttribute("aria-busy", String(isBusy()));
    text("result-time", state.resultTime);
    show("ocr-details", !isBusy() && !!result);
    if (!result || isBusy()) {
      ui["ocr-details"].open = false;
      text("ocr-text", "");
      return;
    }
    const matched = result.status === "matched" && typeof result.name === "string" && !!result.name.trim() && finite(result.price) && result.price >= 0;
    const failed = result.status === "error" || (result.status === "matched" && !matched);
    ui["result-data"].className = `result-data ${matched ? "matched" : failed ? "error" : "no-match"}`;
    ui["result-symbol"].setAttribute("href", matched ? "#i-check" : failed ? "#i-alert" : "#i-ticket");
    text("result-status", matched ? "メニューが見つかりました" : failed ? "読み取りに失敗しました" : "該当するメニューがありません");
    text("product-name", matched ? result.name : failed ? "もう一度お試しください" : "メニューを特定できませんでした");
    show("product-price", matched);
    text("price-number", matched ? formatNumber(result.price) : "");
    const fallback = matched ? "次の食券も、そのまま撮影できます。" : failed ? "カメラの映像を確認して、もう一度撮影してください。" : "文字全体がはっきり写る位置で、もう一度撮影してください。";
    text("result-message", result.message || fallback);
    show("result-retry", !matched);
    text("result-elapsed", finite(result.elapsed_ms) && result.elapsed_ms >= 0 ? `読み取り時間 ${(result.elapsed_ms / 1000).toFixed(1)}秒` : "");
    text("ocr-text", result.text || "読み取った文字はありません。");
  }

  function finishRecognition(result) {
    state.recognitionRevision++;
    state.scanning = false;
    state.accepting = false;
    state.backendBusy = false;
    state.captureId = null;
    state.scanIdleSince = null;
    state.earlyResults.clear();
    state.result = result;
    state.resultTime = new Intl.DateTimeFormat("ja-JP", { hour: "2-digit", minute: "2-digit" }).format(new Date());
    renderResult();
    render();
  }

  function onRecognition(result) {
    if (!result || typeof result.capture_id !== "string" || !["matched", "no_match", "error"].includes(result.status)) return;
    if (state.accepting) {
      // An extremely quick result may beat the command acknowledgment. Keep this bounded.
      if (state.earlyResults.size >= 4) state.earlyResults.delete(state.earlyResults.keys().next().value);
      state.earlyResults.set(result.capture_id, result);
    } else if (state.scanning && result.capture_id === state.captureId) {
      finishRecognition(result);
    }
  }

  async function recognize() {
    if (!canScan()) return;
    state.recognitionRevision++;
    state.scanning = true;
    state.scanIdleSince = null;
    state.accepting = true;
    state.result = null;
    state.resultTime = "";
    state.captureId = null;
    state.earlyResults.clear();
    renderResult();
    render();
    try {
      const accepted = await request({ action: "recognize" });
      if (state.disposed) return;
      if (accepted?.accepted !== true || typeof accepted.capture_id !== "string") throw new Error("撮影を受け付けられませんでした。もう一度撮影してください。");
      state.captureId = accepted.capture_id;
      state.accepting = false;
      const early = state.earlyResults.get(state.captureId);
      state.earlyResults.clear();
      if (early) finishRecognition(early);
    } catch (error) {
      if (!state.disposed) finishRecognition({ status: "error", name: null, price: null, text: "", message: messageOf(error) });
    }
  }

  function syncSettings() {
    const camera = state.camera;
    const gainSupported = finite(camera.gain_min) && finite(camera.gain_max) && camera.gain_max > camera.gain_min && finite(camera.gain);
    const manualSupported = finite(camera.min_exposure_us) && camera.min_exposure_us > 0;
    if (gainSupported) {
      ui["gain-input"].min = String(camera.gain_min);
      ui["gain-input"].max = String(camera.gain_max);
      ui["gain-input"].step = "any";
      text("gain-min", formatNumber(camera.gain_min));
      text("gain-max", formatNumber(camera.gain_max));
    } else {
      text("gain-min", "—");
      text("gain-max", "—");
    }
    if (manualSupported) ui["exposure-input"].min = String(Math.ceil(camera.min_exposure_us));
    else ui["exposure-input"].removeAttribute("min");
    text("exposure-bounds", manualSupported ? `最短 ${formatNumber(camera.min_exposure_us)} µs。反映された値は「現在の設定」で確認できます。` : "このカメラでは手動露光の対応範囲を取得できません。");
    text("gain-hint", gainSupported ? "上げると明るくなりますが、ざらつきが増える場合があります。" : "このカメラは明るさの補正に対応していないか、範囲を取得できません。");
    if (!state.dirty && !state.saving) {
      const mode = camera.auto_exposure ? "auto" : state.requestedMode === "manual" || (manualSupported && finite(camera.exposure_us) && camera.exposure_us > camera.min_exposure_us) ? "manual" : "short";
      modes.forEach((input) => { input.checked = input.value === mode; });
      ui["exposure-input"].value = finite(camera.exposure_us) ? String(camera.exposure_us) : "";
      if (gainSupported) ui["gain-input"].value = String(camera.gain);
      text("gain-value", gainSupported ? formatNumber(camera.gain) : "—");
    }
    const exposure = camera.auto_exposure ? "明るさ自動" : finite(camera.exposure_us) ? `露光 ${formatNumber(camera.exposure_us)} µs` : "露光の値を取得できません";
    text("settings-readback", camera.connected ? `現在の設定：${exposure}${finite(camera.gain) ? ` ／ 補正 ${formatNumber(camera.gain)}` : ""}` : "接続後に現在の設定を表示します。");
    renderSettingsAvailability();
  }

  function renderSettingsAvailability() {
    const camera = state.camera;
    const ready = state.bridgeReady && camera.connected && !state.starting && !state.saving && !state.brightnessPending;
    const gainSupported = finite(camera.gain_min) && finite(camera.gain_max) && camera.gain_max > camera.gain_min && finite(camera.gain);
    const manualSupported = finite(camera.min_exposure_us) && camera.min_exposure_us > 0;
    ui["camera-select"].disabled = !state.devices.length || state.starting;
    ui["camera-connect"].disabled = !state.bridgeReady || !state.devices.some((device) => device.id === ui["camera-select"].value) || state.starting || isBusy();
    text("camera-connect", state.starting ? "接続しています…" : "このカメラに接続");
    ui["exposure-options"].disabled = !ready;
    ui["manual-mode"].disabled = !ready || !manualSupported;
    ui["gain-input"].disabled = !ready || !gainSupported;
    ui["exposure-input"].disabled = !ready || !manualSupported || currentMode() !== "manual";
    show("manual-exposure", currentMode() === "manual");
    ui["settings-apply"].disabled = !ready || !state.dirty || (currentMode() === "manual" && !manualSupported);
    text("settings-apply", state.saving ? "設定を反映中…" : "設定を適用");
    text("settings-camera-note", state.starting ? "カメラを接続しています。設定は映像の準備後に調整できます。" : camera.connected ? "カメラを切り替えるときは、選んでから接続してください。" : "カメラを接続すると、自動で映像を表示します。");
  }

  function markDirty() {
    state.dirty = true;
    show("settings-error", false);
    text("settings-feedback", "変更はまだ反映されていません。");
    renderSettingsAvailability();
  }

  async function applySettings() {
    if (ui["settings-apply"].disabled) return;
    const camera = state.camera;
    const mode = currentMode();
    const payload = { action: "set_camera", exposure_mode: mode };
    if (mode === "manual") {
      const exposure = Number(ui["exposure-input"].value);
      if (!ui["exposure-input"].value.trim() || !Number.isSafeInteger(exposure) || exposure < camera.min_exposure_us) {
        settingsError(`露光時間を ${formatNumber(camera.min_exposure_us)} µs 以上の整数で入力してください。`);
        ui["exposure-input"].focus();
        return;
      }
      payload.exposure_us = exposure;
    }
    if (!ui["gain-input"].disabled) {
      const gain = Number(ui["gain-input"].value);
      if (!finite(gain) || gain < camera.gain_min || gain > camera.gain_max) {
        settingsError("明るさの補正はカメラの対応範囲で指定してください。");
        return;
      }
      payload.gain = gain;
    }
    state.saving = true;
    show("settings-error", false);
    text("settings-feedback", "");
    renderSettingsAvailability();
    try {
      await request(payload);
      state.requestedMode = mode;
      state.dirty = false;
      text("settings-feedback", "設定を送信しました。現在の値はカメラから確認します。");
      state.nextStatusAt = 0;
    } catch (error) {
      settingsError(error);
    } finally {
      state.saving = false;
      renderSettingsAvailability();
    }
  }

  function openSettings() {
    syncSettings();
    if (!ui["settings-dialog"].open) ui["settings-dialog"].showModal();
  }

  async function brighten() {
    if (state.camera.auto_exposure) return openSettings();
    if (state.brightnessPending || !state.camera.connected) return;
    state.brightnessPending = true;
    render();
    try {
      await request({ action: "set_camera", exposure_mode: "auto" });
      state.requestedMode = "auto";
      state.dirty = false;
      appRetryAction = null;
      show("app-error", false);
      state.nextStatusAt = 0;
    } catch (error) {
      appError(error, brighten);
    } finally {
      state.brightnessPending = false;
      render();
    }
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
      const wasDark = state.dark;
      state.lastFrameAt = performance.now();
      state.frameReady = true;
      state.cameraIssue = "";
      if (lightContext && state.lastFrameAt - lastLightCheck >= 1000) {
        lastLightCheck = state.lastFrameAt;
        lightContext.drawImage(bitmap, 0, 0, 32, 18);
        const pixels = lightContext.getImageData(0, 0, 32, 18).data;
        let luminance = 0;
        for (let index = 0; index < pixels.length; index += 4) luminance += .2126 * pixels[index] + .7152 * pixels[index + 1] + .0722 * pixels[index + 2];
        const dark = luminance / (pixels.length / 4) < 45;
        darkSamples = dark ? darkSamples + 1 : 0;
        state.dark = darkSamples >= 2;
      }
      if (needsRender || wasDark !== state.dark) render();
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
    if (state.camera.connected && now - state.lastFrameAt > 5000 && !state.starting) {
      invalidateFrame();
      state.cameraIssue = "映像の受信が止まりました。USBケーブルを確認して、もう一度接続してください。";
      render();
    }
    if (now < state.nextStatusAt || state.checking || state.starting) return;
    try {
      await refreshStatus();
      if (!state.camera.connected && state.devices.length && now - state.lastConnectAttempt >= 6000 && !isBusy()) await connect();
    } catch (error) {
      appError(error);
    }
  }

  async function bootstrap() {
    if (bootstrapPending || state.disposed) return;
    bootstrapPending = true;
    ui["app-retry"].disabled = true;
    try {
      if (!window.__TAURI__?.core?.invoke || !window.__TAURI__?.event?.listen) {
        throw new Error("アプリとの接続がありません。食券メニュー認識のデスクトップアプリから起動してください。");
      }
      if (!state.bridgeReady) {
        const subscriptions = await Promise.allSettled([
          window.__TAURI__.event.listen("camera_frame", (event) => onFrame(event.payload)),
          window.__TAURI__.event.listen("camera_state", (event) => { if (!state.disposed) { state.cameraRevision++; updateCamera(event.payload); } }),
          window.__TAURI__.event.listen("recognition_result", (event) => { if (!state.disposed) onRecognition(event.payload); }),
        ]);
        const registered = subscriptions.filter((result) => result.status === "fulfilled").map((result) => result.value);
        const failed = subscriptions.find((result) => result.status === "rejected");
        if (state.disposed || failed) {
          registered.forEach((remove) => { try { remove(); } catch { /* Continue releasing the remaining subscriptions. */ } });
          if (failed) throw failed.reason;
          return;
        }
        unlisten = registered;
        state.bridgeReady = true;
      }
      await refreshStatus();
      if (!state.camera.connected) await connect();
      if (!state.disposed && timer === null) timer = setInterval(tick, 1000);
    } catch (error) {
      appError(error);
    } finally {
      bootstrapPending = false;
      ui["app-retry"].disabled = false;
      render();
    }
  }

  function dispose() {
    if (state.disposed) return;
    const connected = state.bridgeReady;
    state.disposed = true;
    invalidateFrame();
    clearInterval(timer);
    clearTimeout(gainTimer);
    unlisten.forEach((remove) => { try { remove(); } catch { /* Native teardown may have already removed the listener. */ } });
    unlisten = [];
    state.earlyResults.clear();
    if (fallbackURL) URL.revokeObjectURL(fallbackURL);
    // The shell also owns child teardown; pagehide is a best-effort early release.
    if (connected) window.__TAURI__.core.invoke("backend_request", { request: { action: "shutdown" } }).catch(() => {});
  }

  ui["scan-button"].addEventListener("click", recognize);
  ui["result-retry"].addEventListener("click", recognize);
  ui["camera-retry"].addEventListener("click", () => connect());
  ui["app-retry"].addEventListener("click", async () => {
    const action = appRetryAction || bootstrap;
    show("app-error", false);
    await action();
  });
  ui["settings-open"].addEventListener("click", openSettings);
  ui["camera-warning-settings"].addEventListener("click", openSettings);
  ui["settings-close"].addEventListener("click", () => ui["settings-dialog"].close());
  ui["settings-dialog"].addEventListener("click", (event) => { if (event.target === ui["settings-dialog"]) { const rect = ui["settings-dialog"].getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) ui["settings-dialog"].close(); } });
  ui["settings-apply"].addEventListener("click", applySettings);
  ui["brightness-priority"].addEventListener("click", brighten);
  ui["camera-connect"].addEventListener("click", async () => {
    if (ui["camera-connect"].disabled) return;
    state.dirty = false;
    state.deviceSelectionEdited = false;
    show("settings-error", false);
    text("settings-feedback", "");
    await connect(ui["camera-select"].value);
  });
  ui["camera-select"].addEventListener("change", () => { state.deviceSelectionEdited = true; renderSettingsAvailability(); });
  modes.forEach((input) => input.addEventListener("change", markDirty));
  ui["exposure-input"].addEventListener("input", markDirty);
  ui["gain-input"].addEventListener("input", () => {
    state.dirty = true;
    text("gain-value", formatNumber(Number(ui["gain-input"].value)));
    clearTimeout(gainTimer);
    // Slider edits are staged and debounced; no camera command is sent until Apply.
    gainTimer = setTimeout(markDirty, 180);
  });
  ui["gain-input"].addEventListener("change", () => { clearTimeout(gainTimer); markDirty(); });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.repeat || event.isComposing || event.ctrlKey || event.altKey || event.metaKey || ui["settings-dialog"].open) return;
    if (event.target?.closest?.("button, input, select, textarea, a, summary, [contenteditable]")) return;
    if (canScan()) { event.preventDefault(); recognize(); }
  });
  window.addEventListener("pagehide", dispose, { once: true });
  renderResult();
  render();
  bootstrap();
})();
