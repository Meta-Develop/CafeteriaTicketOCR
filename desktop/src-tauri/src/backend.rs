use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs::{File, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{sync_channel, SyncSender},
        Arc, Mutex,
    },
    thread,
    time::Duration,
};
use tokio::sync::oneshot;

const MAX_LINE: usize = 2 * 1024 * 1024;
const MAX_PENDING: usize = 16;
const WAIT: Duration = Duration::from_secs(15);
type Reply = Result<Value, String>;
type Emit = Arc<dyn Fn(&str, Value) + Send + Sync>;
struct Pending {
    action: String,
    reply: oneshot::Sender<Reply>,
}

#[derive(Default)]
struct Preview {
    session: Option<String>,
    latest: Option<Value>,
}

impl Preview {
    fn camera_state(&mut self, payload: &Value, emit: &Emit) {
        let session = payload["connected"]
            .as_bool()
            .filter(|connected| *connected)
            .and_then(|_| payload["session_id"].as_str())
            .map(str::to_owned);
        if session != self.session || session.is_none() {
            self.latest = None;
        }
        self.session = session;
        emit("camera_state", payload.clone());
    }

    fn camera_frame(&mut self, payload: &Value) {
        if self
            .session
            .as_deref()
            .is_some_and(|session| payload["session_id"].as_str() == Some(session))
        {
            self.latest = Some(payload.clone());
        }
    }

    fn emit_latest(&mut self, emit: &Emit) {
        if let Some(payload) = self.latest.take() {
            emit("camera_frame", payload);
        }
    }
}

pub struct Backend {
    child: Mutex<Option<Child>>,
    writer: Mutex<Option<SyncSender<Vec<u8>>>>,
    pending: Mutex<HashMap<u64, Pending>>,
    closed: AtomicBool,
    error: Mutex<String>,
    next_id: AtomicU64,
    preview: Mutex<Preview>,
}

fn project_paths() -> Result<(PathBuf, PathBuf, PathBuf), String> {
    let root = std::env::var_os("CAFETERIA_PROJECT_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .canonicalize()
        .map_err(|_| "プロジェクトの場所を確認できません。".to_string())?;
    let python = root.join(".venv/bin/python");
    let backend = root.join("host/desktop_backend.py");
    if !root.join(".venv/pyvenv.cfg").is_file()
        || !root.join("desktop/src-tauri/tauri.conf.json").is_file()
        || !python.is_file()
        || !backend.is_file()
    {
        return Err(
            "Python環境または認識バックエンドがありません。--check で確認してください。".into(),
        );
    }
    let backend = backend
        .canonicalize()
        .map_err(|_| "バックエンドを確認できません。")?;
    if !backend.starts_with(root.join("host")) {
        return Err("バックエンドの場所がプロジェクト外です。".into());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if python
            .metadata()
            .map_err(|_| "Pythonを確認できません。")?
            .permissions()
            .mode()
            & 0o111
            == 0
        {
            return Err("Pythonを実行できません。".into());
        }
    }
    Ok((root, python, backend))
}

fn diagnostics(root: &std::path::Path) -> Option<File> {
    // A headless check must not truncate diagnostics owned by a running app.
    if std::env::args().nth(1).as_deref() == Some("--check-sidecar") {
        return None;
    }
    let path = root.join(".agents/storage/tauri-rebuild");
    std::fs::create_dir_all(&path).ok()?;
    let mut options = OpenOptions::new();
    options.create(true).write(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path.join("backend-stderr.log")).ok()
}

impl Backend {
    pub fn launch(emit: Emit) -> Arc<Self> {
        let state = Arc::new(Self {
            child: Mutex::new(None),
            writer: Mutex::new(None),
            pending: Mutex::new(HashMap::new()),
            closed: AtomicBool::new(false),
            error: Mutex::new(String::new()),
            next_id: AtomicU64::new(1),
            preview: Mutex::new(Preview::default()),
        });
        if let Err(error) = state.spawn(emit.clone()) {
            state.stop(&error);
            emit("backend_error", json!({"message": error}));
        }
        state
    }

    fn spawn(self: &Arc<Self>, emit: Emit) -> Result<(), String> {
        let (root, python, script) = project_paths()?;
        let mut child = Command::new(python)
            .args(["-B"])
            .arg(script)
            .current_dir(&root)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("PYTHONUNBUFFERED", "1")
            .env("OMP_NUM_THREADS", "1")
            .env("OPENBLAS_NUM_THREADS", "1")
            .env("MKL_NUM_THREADS", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| {
                "認識バックエンドを起動できません。Python環境を確認してください。".to_string()
            })?;
        let mut stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let mut stderr = child.stderr.take().unwrap();
        *self.child.lock().unwrap() = Some(child);
        let (send, receive) = sync_channel::<Vec<u8>>(MAX_PENDING);
        *self.writer.lock().unwrap() = Some(send);
        let state = self.clone();
        thread::spawn(move || {
            while let Ok(line) = receive.recv() {
                if state.closed.load(Ordering::Acquire) {
                    break;
                }
                if stdin.write_all(&line).and_then(|_| stdin.flush()).is_err() {
                    state.stop("認識バックエンドとの接続が切れました。");
                    break;
                }
            }
        });
        let mut log = diagnostics(&root);
        thread::spawn(move || {
            let mut buffer = [0; 4096];
            let mut remaining = 512 * 1024;
            while let Ok(count) = stderr.read(&mut buffer) {
                if count == 0 {
                    break;
                }
                if let Some(file) = log.as_mut() {
                    let keep = count.min(remaining);
                    let _ = file.write_all(&buffer[..keep]);
                    remaining -= keep;
                }
            }
        });
        // Only one preview frame is retained, independent of protocol replies.
        let state = self.clone();
        let frame_emit = emit.clone();
        thread::spawn(move || {
            loop {
                {
                    // Serialize the session check, transition and emit. WebView
                    // events already queued are also filtered by the frontend.
                    let mut preview = state.preview.lock().unwrap();
                    if state.closed.load(Ordering::Acquire) {
                        break;
                    }
                    preview.emit_latest(&frame_emit);
                }
                thread::sleep(Duration::from_millis(34));
            }
        });
        let state = self.clone();
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = Vec::new();
            let error = loop {
                line.clear();
                match reader
                    .by_ref()
                    .take((MAX_LINE + 1) as u64)
                    .read_until(b'\n', &mut line)
                {
                    Ok(0) => break "認識バックエンドが終了しました。アプリを再起動してください。",
                    Ok(_) if line.len() <= MAX_LINE && line.last() == Some(&b'\n') => {}
                    _ => break "認識バックエンドからの応答を読み取れませんでした。",
                }
                let message: Value = match serde_json::from_slice(&line) {
                    Ok(Value::Object(map)) => Value::Object(map),
                    _ => break "認識バックエンドの応答形式が正しくありません。",
                };
                if let Some(event) = message.get("event").and_then(Value::as_str) {
                    let Some(payload) = message.get("payload").filter(|p| p.is_object()) else {
                        break "認識バックエンドのイベント形式が正しくありません。";
                    };
                    match event {
                        "camera_frame" if valid_frame(payload) => {
                            let mut preview = state.preview.lock().unwrap();
                            if !state.closed.load(Ordering::Acquire) {
                                preview.camera_frame(payload);
                            }
                        }
                        "camera_state" if valid_camera(payload) => {
                            let mut preview = state.preview.lock().unwrap();
                            if !state.closed.load(Ordering::Acquire) {
                                preview.camera_state(payload, &emit);
                            }
                        }
                        "recognition_result" | "ticket_detected" if valid_recognition(payload) => {
                            emit(event, payload.clone())
                        }
                        "live_recognition" if valid_live_recognition(payload) => {
                            emit(event, payload.clone())
                        }
                        "live_state" if valid_live_state(payload) => emit(event, payload.clone()),
                        _ => break "認識バックエンドのイベント形式が正しくありません。",
                    }
                } else if let Some(id) = message.get("id").and_then(Value::as_u64) {
                    if let Some(pending) = state.pending.lock().unwrap().remove(&id) {
                        let result = validate_response(&pending.action, &message);
                        let _ = pending.reply.send(result);
                    }
                } else {
                    break "認識バックエンドの応答IDがありません。";
                }
            };
            state.stop(error);
            emit("backend_error", json!({"message": error}));
        });
        Ok(())
    }

    pub async fn request(&self, mut request: Value) -> Reply {
        let action = validate_request(&request)?.to_owned();
        if self.closed.load(Ordering::Acquire) {
            return Err(self.error.lock().unwrap().clone());
        }
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        request["id"] = json!(id);
        let (send, receive) = oneshot::channel();
        {
            let mut pending = self.pending.lock().unwrap();
            if self.closed.load(Ordering::Acquire) {
                return Err(self.error.lock().unwrap().clone());
            }
            if pending.len() >= MAX_PENDING {
                return Err("処理が混み合っています。少し待って再試行してください。".into());
            }
            pending.insert(
                id,
                Pending {
                    action: action.clone(),
                    reply: send,
                },
            );
        }
        let mut line = serde_json::to_vec(&request).map_err(|_| "要求を送信できません。")?;
        line.push(b'\n');
        let sent = self
            .writer
            .lock()
            .unwrap()
            .as_ref()
            .is_some_and(|writer| writer.try_send(line).is_ok());
        if !sent {
            self.pending.lock().unwrap().remove(&id);
            return Err("認識バックエンドに要求を送信できません。".into());
        }
        let result = match tokio::time::timeout(WAIT, receive).await {
            Ok(Ok(result)) => result,
            _ => {
                self.pending.lock().unwrap().remove(&id);
                Err(
                    "認識バックエンドの応答がありません。再試行またはアプリを再起動してください。"
                        .into(),
                )
            }
        };
        if action == "shutdown" {
            self.stop("認識バックエンドを終了しました。");
        }
        result
    }

    pub fn stop(&self, error: &str) {
        let mut reason = self.error.lock().unwrap();
        {
            let mut preview = self.preview.lock().unwrap();
            if self.closed.swap(true, Ordering::AcqRel) {
                return;
            }
            *preview = Preview::default();
        }
        *reason = error.to_owned();
        drop(reason);
        self.writer.lock().unwrap().take();
        for (_, pending) in self.pending.lock().unwrap().drain() {
            let _ = pending.reply.send(Err(error.to_owned()));
        }
        if let Some(mut child) = self.child.lock().unwrap().take() {
            // The stored Child is the only process this application may terminate.
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn validate_request(request: &Value) -> Result<&str, String> {
    let bad = || "要求の形式が正しくありません。".to_string();
    let object = request.as_object().ok_or_else(bad)?;
    let action = object
        .get("action")
        .and_then(Value::as_str)
        .ok_or_else(bad)?;
    let fields: &[&str] = match action {
        "status" | "stop_camera" | "recognize" | "shutdown" => &["action"],
        "start_camera" => &["action", "device"],
        "set_camera" => &["action", "exposure_mode", "exposure_us", "gain", "controls"],
        "set_live" => &[
            "action",
            "enabled",
            "confirm_passes",
            "dedup_gap",
            "roi",
            "scale",
        ],
        _ => return Err("対応していない操作です。".into()),
    };
    if object.keys().any(|key| !fields.contains(&key.as_str())) {
        return Err(bad());
    }
    if let Some(device) = object.get("device") {
        if !device
            .as_str()
            .is_some_and(|s| !s.is_empty() && s.len() <= 512 && !s.chars().any(char::is_control))
        {
            return Err(bad());
        }
    }
    if let Some(mode) = object.get("exposure_mode") {
        if !matches!(mode.as_str(), Some("short" | "auto" | "manual")) {
            return Err(bad());
        }
    }
    for field in ["exposure_us", "gain"] {
        if let Some(value) = object.get(field) {
            if !value.as_f64().is_some_and(|v| {
                ((if field == "gain" {
                    -1_000_000_000.0
                } else {
                    0.0
                })..=1_000_000_000.0)
                    .contains(&v)
            }) {
                return Err(bad());
            }
        }
    }
    if action == "set_camera"
        && object.get("exposure_mode").is_some_and(|m| m == "manual")
        && !object.contains_key("exposure_us")
    {
        return Err(bad());
    }
    if let Some(controls) = object.get("controls") {
        let values = controls.as_object().ok_or_else(bad)?;
        if values.is_empty()
            || values.len() > 32
            || values.iter().any(|(key, value)| {
                key.is_empty()
                    || key.len() > 64
                    || !key.as_bytes()[0].is_ascii_lowercase()
                    || !key
                        .bytes()
                        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
                    || !value
                        .as_i64()
                        .is_some_and(|v| (-1_000_000_000..=1_000_000_000).contains(&v))
            })
        {
            return Err(bad());
        }
    }
    if action == "set_live" {
        if object.get("enabled").is_some_and(|v| !v.is_boolean())
            || object
                .get("confirm_passes")
                .is_some_and(|v| !v.as_u64().is_some_and(|n| n <= 10))
        {
            return Err(bad());
        }
        for (field, min, max) in [
            ("dedup_gap", 0.1, 30.0),
            ("roi", 0.1, 1.0),
            ("scale", 0.1, 1.0),
        ] {
            if object.get(field).is_some_and(|v| {
                !v.as_f64()
                    .is_some_and(|n| n.is_finite() && (min..=max).contains(&n))
            }) {
                return Err(bad());
            }
        }
    }
    if matches!(action, "set_camera" | "set_live") && object.len() == 1 {
        return Err(bad());
    }
    Ok(action)
}

fn nullable_number(v: &Value) -> bool {
    v.is_null() || v.is_number()
}
fn nullable_string(v: &Value) -> bool {
    v.is_null() || v.is_string()
}
fn valid_camera(v: &Value) -> bool {
    v.is_object()
        && if v["connected"] == true {
            v["session_id"].as_str().is_some_and(|s| !s.is_empty())
        } else {
            v.get("session_id").is_some_and(Value::is_null)
        }
        && v.get("device").is_some_and(nullable_string)
        && v.get("error").is_some_and(nullable_string)
        && ["connected", "auto_exposure"]
            .iter()
            .all(|k| v[*k].is_boolean())
        && ["stream_fps", "preview_fps", "width", "height"]
            .iter()
            .all(|k| v[*k].is_number())
        && [
            "exposure_us",
            "min_exposure_us",
            "gain",
            "gain_min",
            "gain_max",
            "brightness",
        ]
        .iter()
        .all(|k| v.get(*k).is_some_and(nullable_number))
}
fn valid_frame(v: &Value) -> bool {
    v["jpeg"]
        .as_str()
        .is_some_and(|s| !s.is_empty() && s.len() < MAX_LINE)
        && v["width"].as_u64().is_some_and(|n| n > 0 && n <= 640)
        && v["height"].as_u64().is_some_and(|n| n > 0 && n <= 360)
        && v["timestamp_ms"].is_number()
}
fn valid_recognition(v: &Value) -> bool {
    ["capture_id", "text", "message"]
        .iter()
        .all(|k| v[*k].is_string())
        && matches!(v["status"].as_str(), Some("matched" | "no_match" | "error"))
        && v.get("name").is_some_and(nullable_string)
        && ["price", "score", "confidence"]
            .iter()
            .all(|k| v.get(*k).is_some_and(nullable_number))
        && v["elapsed_ms"].is_number()
}
fn valid_live_state(v: &Value) -> bool {
    v["enabled"].is_boolean()
        && v["running"].is_boolean()
        && v.get("error").is_some_and(nullable_string)
        && v["confirm_passes"].as_u64().is_some_and(|n| n <= 10)
        && [
            ("dedup_gap", 0.1, 30.0),
            ("roi", 0.1, 1.0),
            ("scale", 0.1, 1.0),
        ]
        .iter()
        .all(|(key, min, max)| {
            v[*key]
                .as_f64()
                .is_some_and(|n| n.is_finite() && (*min..=*max).contains(&n))
        })
}
fn valid_live_recognition(v: &Value) -> bool {
    matches!(v["status"].as_str(), Some("matched" | "no_match"))
        && v["text"].is_string()
        && v.get("name").is_some_and(nullable_string)
        && ["price", "score"]
            .iter()
            .all(|k| v.get(*k).is_some_and(nullable_number))
        && ["ocr_latency_ms", "passes_per_second", "timestamp_ms"]
            .iter()
            .all(|k| v[*k].as_f64().is_some_and(|n| n >= 0.0))
        && v["session_id"].as_str().is_some_and(|s| !s.is_empty())
}
fn validate_response(action: &str, message: &Value) -> Reply {
    match message["ok"].as_bool() {
        Some(false) => {
            return Err(message["error"]
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 4096)
                .unwrap_or("認識バックエンドでエラーが発生しました。")
                .to_owned())
        }
        Some(true) => {}
        None => return Err("認識バックエンドの応答形式が正しくありません。".into()),
    }
    let result = &message["result"];
    let valid = result.is_object()
        && match action {
            "status" | "start_camera" | "stop_camera" => {
                valid_camera(&result["camera"])
                    && result["devices"].as_array().is_some_and(|items| {
                        items
                            .iter()
                            .all(|v| v["id"].is_string() && v["label"].is_string())
                    })
                    && result["busy"].is_boolean()
                    && result["ocr"]["ready"].is_boolean()
                    && result["ocr"]["menu_count"].is_u64()
                    && result["ocr"].get("error").is_some_and(nullable_string)
                    && valid_live_state(&result["live"])
            }
            "recognize" => result["accepted"] == true && result["capture_id"].is_string(),
            "set_camera" | "shutdown" => result["accepted"] == true,
            "set_live" => result["accepted"] == true && valid_live_state(result),
            _ => false,
        };
    if valid {
        Ok(result.clone())
    } else {
        Err("認識バックエンドの応答形式が正しくありません。".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn camera(connected: bool, session: Option<&str>) -> Value {
        json!({"connected":connected,"session_id":session,"device":"/dev/video4",
            "stream_fps":0,"preview_fps":0,"width":640,"height":360,
            "exposure_us":100,"min_exposure_us":100,"gain":24,"gain_min":0,
            "gain_max":64,"auto_exposure":false,"brightness":80,"error":null})
    }

    fn frame(session: Option<&str>, timestamp: u64) -> Value {
        json!({"session_id":session,"jpeg":"test-jpeg","width":640,"height":360,
            "timestamp_ms":timestamp})
    }

    #[test]
    fn camera_transitions_discard_retained_and_late_old_frames() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let recorded = events.clone();
        let emit: Emit = Arc::new(move |event, payload| {
            recorded.lock().unwrap().push((event.to_owned(), payload));
        });
        let mut preview = Preview::default();
        preview.camera_frame(&frame(Some("A"), 0));
        assert!(
            preview.latest.is_none(),
            "no frames before a connected state"
        );
        preview.camera_state(&camera(true, Some("A")), &emit);
        preview.camera_frame(&frame(Some("A"), 1));
        assert!(preview.latest.is_some());
        preview.camera_state(&camera(false, None), &emit);
        assert!(preview.session.is_none());
        assert!(
            preview.latest.is_none(),
            "disconnect clears a retained A frame"
        );
        preview.camera_frame(&frame(Some("A"), 2));
        preview.emit_latest(&emit);
        preview.camera_state(&camera(true, Some("B")), &emit);
        for invalid in [
            frame(Some("A"), 3),
            frame(None, 4),
            json!({"jpeg":"missing-session"}),
        ] {
            preview.camera_frame(&invalid);
        }
        assert!(
            preview.latest.is_none(),
            "late or unidentified frames must be dropped"
        );
        preview.camera_frame(&frame(Some("B"), 5));
        preview.emit_latest(&emit);
        let events = events.lock().unwrap();
        assert_eq!(events.len(), 4);
        assert_eq!(events[2], ("camera_state".into(), camera(true, Some("B"))));
        assert_eq!(events[3], ("camera_frame".into(), frame(Some("B"), 5)));
    }

    #[test]
    fn previews_remain_latest_only_and_clear_on_direct_session_switch() {
        let emit: Emit = Arc::new(|_, _| {});
        let mut preview = Preview::default();
        preview.camera_state(&camera(true, Some("A")), &emit);
        for timestamp in 0..1000 {
            preview.camera_frame(&frame(Some("A"), timestamp));
        }
        preview.camera_state(&camera(true, Some("A")), &emit);
        assert_eq!(preview.latest, Some(frame(Some("A"), 999)));
        preview.camera_state(&camera(true, Some("B")), &emit);
        assert!(preview.latest.is_none());
        assert!(valid_camera(&camera(true, Some("B"))));
        assert!(valid_camera(&camera(false, None)));
        assert!(!valid_camera(&camera(true, None)));
        assert!(!valid_camera(&camera(true, Some(""))));
        assert!(!valid_camera(&camera(false, Some("A"))));
        let mut missing = camera(false, None);
        missing.as_object_mut().unwrap().remove("session_id");
        assert!(!valid_camera(&missing));
    }

    #[test]
    fn transition_cannot_overtake_a_frame_emit_in_progress() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let recorded = events.clone();
        let (entered, frame_started) = std::sync::mpsc::channel();
        let (release, unblocked) = std::sync::mpsc::channel();
        let unblocked = Mutex::new(unblocked);
        let emit: Emit = Arc::new(move |event, payload| {
            if event == "camera_frame" {
                entered.send(()).unwrap();
                unblocked
                    .lock()
                    .unwrap()
                    .recv_timeout(Duration::from_secs(2))
                    .unwrap();
            }
            recorded.lock().unwrap().push((event.to_owned(), payload));
        });
        let preview = Arc::new(Mutex::new(Preview {
            session: Some("A".into()),
            latest: Some(frame(Some("A"), 1)),
        }));
        let frame_preview = preview.clone();
        let frame_emit = emit.clone();
        let emitter = thread::spawn(move || frame_preview.lock().unwrap().emit_latest(&frame_emit));
        frame_started.recv_timeout(Duration::from_secs(2)).unwrap();
        let (attempted, transition_started) = std::sync::mpsc::channel();
        let transition = thread::spawn(move || {
            attempted.send(()).unwrap();
            preview
                .lock()
                .unwrap()
                .camera_state(&camera(false, None), &emit);
        });
        transition_started
            .recv_timeout(Duration::from_secs(2))
            .unwrap();
        release.send(()).unwrap();
        emitter.join().unwrap();
        transition.join().unwrap();
        let events = events.lock().unwrap();
        assert_eq!(events[0].0, "camera_frame");
        assert_eq!(events[1], ("camera_state".into(), camera(false, None)));
    }

    #[test]
    fn rejects_command_injection_and_invalid_requests() {
        for request in [
            json!({"action":"exec","command":"echo x"}),
            json!({"action":"status","id":4}),
            json!({"action":"start_camera","device":"x\nshutdown"}),
            json!({"action":"set_camera","gain":"24"}),
            json!({"action":"set_camera","exposure_mode":"shell"}),
            json!({"action":"set_camera","exposure_us":-1}),
        ] {
            assert!(validate_request(&request).is_err());
        }
        assert!(validate_request(
            &json!({"action":"set_camera","exposure_mode":"short","gain":24})
        )
        .is_ok());
    }

    #[test]
    fn live_settings_and_native_controls_are_validated() {
        for request in [
            json!({"action":"set_live"}),
            json!({"action":"set_live","enabled":1}),
            json!({"action":"set_live","confirm_passes":1.5}),
            json!({"action":"set_live","confirm_passes":11}),
            json!({"action":"set_live","dedup_gap":0}),
            json!({"action":"set_live","roi":1.1}),
            json!({"action":"set_live","scale":0}),
            json!({"action":"set_live","path":"private"}),
            json!({"action":"set_camera","controls":{"gain;exec":1}}),
            json!({"action":"set_camera","controls":{"gain":true}}),
            json!({"action":"set_camera","controls":{}}),
            json!({"action":"set_camera","exposure_mode":"manual"}),
        ] {
            assert!(validate_request(&request).is_err(), "{request}");
        }
        assert!(
            validate_request(&json!({"action":"set_live","enabled":false,
            "confirm_passes":0,"dedup_gap":1.5,"roi":0.75,"scale":0.5}))
            .is_ok()
        );
        assert!(validate_request(&json!({"action":"set_camera","gain":-10,
            "controls":{"brightness":-4,"focus_auto":0}}))
        .is_ok());
        let live = json!({"enabled":true,"running":false,"error":null,
            "confirm_passes":0,"dedup_gap":1.5,"roi":1,"scale":1,"accepted":true});
        assert!(validate_response("set_live", &json!({"ok":true,"result":live})).is_ok());
        assert!(valid_live_state(&live));
        let pass = json!({"status":"matched","name":"カレー","price":350,"score":1.1,
            "text":"カレー","session_id":"A","ocr_latency_ms":34,
            "passes_per_second":20,"timestamp_ms":1});
        assert!(valid_live_recognition(&pass));
        let mut invalid = pass;
        invalid["session_id"] = Value::Null;
        assert!(!valid_live_recognition(&invalid));
    }
    #[test]
    fn rejects_malformed_responses_and_forwards_errors() {
        assert!(validate_response("status", &json!({"ok":true,"result":{}})).is_err());
        assert!(validate_response(
            "recognize",
            &json!({"ok":true,"result":{"accepted":true,"capture_id":"1"}})
        )
        .is_ok());
        assert_eq!(
            validate_response("status", &json!({"ok":false,"error":"カメラ未接続"})),
            Err("カメラ未接続".into())
        );
        assert!(!valid_frame(
            &json!({"jpeg":"x","width":1920,"height":1080,"timestamp_ms":1})
        ));
    }

    #[test]
    fn closing_rejects_waiters_and_reaps_only_owned_child() {
        let (_, python, _) = project_paths().unwrap();
        let owned = Command::new(&python)
            .args(["-B", "-c", "import time; time.sleep(30)"])
            .spawn()
            .unwrap();
        let mut other = Command::new(&python)
            .args(["-B", "-c", "import time; time.sleep(30)"])
            .spawn()
            .unwrap();
        let (send, mut receive) = oneshot::channel();
        let state = Backend {
            child: Mutex::new(Some(owned)),
            writer: Mutex::new(None),
            pending: Mutex::new(HashMap::from([(
                1,
                Pending {
                    action: "status".into(),
                    reply: send,
                },
            )])),
            closed: AtomicBool::new(false),
            error: Mutex::new(String::new()),
            next_id: AtomicU64::new(2),
            preview: Mutex::new(Preview {
                session: Some("A".into()),
                latest: Some(frame(Some("A"), 1)),
            }),
        };
        state.stop("closed");
        let other_running = other.try_wait().unwrap().is_none();
        let _ = other.kill();
        let _ = other.wait();
        assert!(other_running, "unowned child must be preserved");
        assert_eq!(receive.try_recv().unwrap(), Err("closed".into()));
        assert!(state.child.lock().unwrap().is_none());
        assert!(state.pending.lock().unwrap().is_empty());
        assert!(state.preview.lock().unwrap().session.is_none());
        assert!(state.preview.lock().unwrap().latest.is_none());
        state.stop("second close");
        assert_eq!(*state.error.lock().unwrap(), "closed");
    }
}
