#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;

use backend::Backend;
use serde_json::Value;
use std::sync::Arc;
use tauri::{Emitter, Manager};

#[tauri::command]
async fn backend_request(
    request: Value,
    state: tauri::State<'_, Arc<Backend>>,
) -> Result<Value, String> {
    state.request(request).await
}

fn main() {
    // This check never initializes GTK, opens a camera, or downloads anything.
    if std::env::args().nth(1).as_deref() == Some("--check-sidecar") {
        let service = Backend::launch(Arc::new(|_, _| {}));
        let result = tauri::async_runtime::block_on(
            service.request(serde_json::json!({"action": "status"})),
        )
        .and_then(|status| {
            tauri::async_runtime::block_on(
                service.request(serde_json::json!({"action": "set_live", "enabled": false})),
            )?;
            Ok(status)
        });
        service.stop("アプリケーションを終了しました。");
        match result {
            Ok(status) => println!(
                "native_sidecar_check: ok (ocr_ready={}, menu_count={}, set_live=ok)",
                status["ocr"]["ready"], status["ocr"]["menu_count"]
            ),
            Err(error) => {
                eprintln!("native_sidecar_check: {error}");
                std::process::exit(1);
            }
        }
        return;
    }

    let app = tauri::Builder::default()
        .setup(|app| {
            let handle = app.handle().clone();
            app.manage(Backend::launch(Arc::new(move |event, payload| {
                let _ = handle.emit(event, payload);
            })));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_request])
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::Destroyed) {
                window
                    .state::<Arc<Backend>>()
                    .stop("ウィンドウを閉じました。");
            }
        })
        .build(tauri::generate_context!())
        .expect("ネイティブアプリケーションを初期化できませんでした。");
    app.run(|handle, event| {
        if matches!(
            event,
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
        ) {
            handle
                .state::<Arc<Backend>>()
                .stop("アプリケーションを終了しました。");
        }
    });
}
