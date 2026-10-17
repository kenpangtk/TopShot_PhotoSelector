use serde::Serialize;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent, State};

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct EngineInfo {
    port: u16,
    token: String,
}

#[derive(Clone, Default)]
struct EngineState {
    info: Arc<Mutex<Option<EngineInfo>>>,
    error: Arc<Mutex<Option<String>>>,
    child: Arc<Mutex<Option<Child>>>,
}

impl EngineState {
    fn stop(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }

    fn set_error(&self, message: impl Into<String>) {
        let mut error = self.error.lock().unwrap();
        if error.is_none() {
            *error = Some(message.into());
        }
    }
}

fn project_root() -> Option<PathBuf> {
    if let Ok(value) = std::env::var("TOPSHOT_ROOT") {
        let path = PathBuf::from(value);
        if path.join("engine/topshot_engine/server.py").exists() {
            return Some(path);
        }
    }
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    if let Some(parent) = manifest.parent() {
        if parent.join("engine/topshot_engine/server.py").exists() {
            return Some(parent.to_path_buf());
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        for candidate in [cwd.clone(), cwd.join("..")] {
            if candidate.join("engine/topshot_engine/server.py").exists() {
                return Some(candidate);
            }
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            for candidate in [exe_dir.to_path_buf(), exe_dir.join("../Resources")] {
                if candidate.join("engine/topshot_engine/server.py").exists() {
                    return Some(candidate);
                }
            }
        }
    }
    None
}

fn python_for(root: &Path) -> PathBuf {
    if let Ok(value) = std::env::var("TOPSHOT_ENGINE_PYTHON") {
        return PathBuf::from(value);
    }
    let venv = root.join("engine/.venv/bin/python");
    if venv.exists() {
        return venv;
    }
    PathBuf::from("python3.11")
}

fn start_engine(state: EngineState) {
    thread::spawn(move || {
        let Some(root) = project_root() else {
            state.set_error(
                "Could not locate the Topshot Python engine. Run the app from the repository or set TOPSHOT_ROOT.",
            );
            return;
        };
        let python = python_for(&root);
        let engine_dir = root.join("engine");
        let mut command = Command::new(&python);
        command
            .arg("-u")
            .arg("-m")
            .arg("topshot_engine.server")
            .arg("--port")
            .arg("0")
            .current_dir(&engine_dir)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env("TOPSHOT_ROOT", &root);
        if let Ok(data_dir) = std::env::var("TOPSHOT_DATA_DIR") {
            command.env("TOPSHOT_DATA_DIR", data_dir);
        }

        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                state.set_error(format!(
                    "Could not start the Python engine at {}: {error}. Install engine dependencies first (see README).",
                    python.display()
                ));
                return;
            }
        };

        let stdout = match child.stdout.take() {
            Some(stdout) => stdout,
            None => {
                state.set_error("Python engine has no stdout pipe.");
                return;
            }
        };
        let stderr = match child.stderr.take() {
            Some(stderr) => stderr,
            None => {
                state.set_error("Python engine has no stderr pipe.");
                return;
            }
        };

        if let Ok(mut guard) = state.child.lock() {
            *guard = Some(child);
        }

        thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                eprintln!("[engine] {line}");
            }
        });

        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let mut ready = false;
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => break,
                Ok(_) => {
                    let trimmed = line.trim();
                    if trimmed.is_empty() {
                        continue;
                    }
                    if let Ok(value) = serde_json::from_str::<serde_json::Value>(trimmed) {
                        let port = value.get("port").and_then(|value| value.as_u64());
                        let token = value.get("token").and_then(|value| value.as_str());
                        if let (Some(port), Some(token)) = (port, token) {
                            if let Ok(mut info) = state.info.lock() {
                                *info = Some(EngineInfo {
                                    port: port as u16,
                                    token: token.to_string(),
                                });
                            }
                            ready = true;
                            break;
                        }
                    }
                    eprintln!("[engine] {trimmed}");
                }
                Err(error) => {
                    state.set_error(format!("Could not read engine output: {error}"));
                    break;
                }
            }
        }

        if !ready {
            state.set_error("The local engine exited before it became ready. Check the engine log.");
        }

        thread::spawn(move || {
            for line in reader.lines().map_while(Result::ok) {
                eprintln!("[engine] {line}");
            }
        });
    });
}

fn wait_for_engine(state: &EngineState) -> Result<EngineInfo, String> {
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        if let Ok(info) = state.info.lock() {
            if let Some(info) = info.clone() {
                return Ok(info);
            }
        }
        if let Ok(error) = state.error.lock() {
            if let Some(error) = error.clone() {
                return Err(error);
            }
        }
        if Instant::now() >= deadline {
            return Err(
                "The local engine did not start within 30 seconds. Check engine/.venv and the terminal log."
                    .to_string(),
            );
        }
        thread::sleep(Duration::from_millis(120));
    }
}

#[tauri::command]
async fn engine_info(state: State<'_, EngineState>) -> Result<EngineInfo, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || wait_for_engine(&state))
        .await
        .map_err(|error| error.to_string())?
}

#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    let path = PathBuf::from(path);
    if !path.is_dir() {
        return Err("The destination folder no longer exists.".to_string());
    }
    Command::new("open")
        .arg(&path)
        .spawn()
        .map_err(|error| format!("Could not open destination: {error}"))?;
    Ok(())
}

pub fn run() {
    let state = EngineState::default();
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(state.clone())
        .invoke_handler(tauri::generate_handler![engine_info, open_path])
        .setup(move |_app| {
            start_engine(state.clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Topshot")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                app_handle.state::<EngineState>().stop();
            }
        });
}
