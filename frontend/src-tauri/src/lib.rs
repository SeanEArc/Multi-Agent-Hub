//! Multi-Agent Hub desktop shell.
//!
//! The shell starts the Python backend as a child process on a free loopback port with a fresh
//! token (passed in `HUB_TOKEN`, never on the command line), waits until `/api/health` answers,
//! restarts the backend if it exits, and hands the port and token to the UI through the
//! `hub_connection` command. Each backend run is placed in a Windows job object, so the backend
//! and every agent CLI it started are killed when it crashes or the shell closes.

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, RunEvent, State};

mod job;

/// The backend's exit code when another server already holds the data folder (app/server.py).
const EXIT_ALREADY_RUNNING: i32 = 3;
/// More crashes than this within CRASH_WINDOW and the shell stops restarting.
const MAX_CRASHES: usize = 5;
const CRASH_WINDOW: Duration = Duration::from_secs(120);
const START_TIMEOUT: Duration = Duration::from_secs(60);
const LOG_ROTATE_BYTES: u64 = 5 * 1024 * 1024;

#[derive(Clone, Serialize)]
struct BackendStatus {
    /// starting | ready | restarting | failed | stopped
    state: &'static str,
    message: Option<String>,
}

struct Backend {
    port: u16,
    token: String,
    log_file: PathBuf,
    status: Mutex<BackendStatus>,
    child: Mutex<Option<Child>>,
    stopping: AtomicBool,
    supervising: AtomicBool,
}

#[derive(Serialize)]
struct Connection {
    base_url: String,
    token: String,
}

#[tauri::command]
fn hub_connection(backend: State<'_, Arc<Backend>>) -> Connection {
    Connection {
        base_url: format!("http://127.0.0.1:{}", backend.port),
        token: backend.token.clone(),
    }
}

#[tauri::command]
fn backend_status(backend: State<'_, Arc<Backend>>) -> BackendStatus {
    backend.status.lock().unwrap().clone()
}

/// Starts the backend again after it failed for good (the UI's Retry button).
#[tauri::command]
fn restart_backend(app: AppHandle, backend: State<'_, Arc<Backend>>) {
    start_supervisor(app, backend.inner().clone());
}

fn set_status(app: &AppHandle, backend: &Backend, state: &'static str, message: Option<String>) {
    let status = BackendStatus { state, message };
    *backend.status.lock().unwrap() = status.clone();
    let _ = app.emit("backend-status", status);
}

fn start_supervisor(app: AppHandle, backend: Arc<Backend>) {
    if backend.supervising.swap(true, Ordering::SeqCst) {
        return;
    }
    thread::spawn(move || {
        supervise(&app, &backend);
        backend.supervising.store(false, Ordering::SeqCst);
    });
}

/// Runs the backend, and runs it again whenever it exits, until the shell closes or it keeps crashing.
fn supervise(app: &AppHandle, backend: &Backend) {
    let mut crashes: Vec<Instant> = Vec::new();
    loop {
        if backend.stopping.load(Ordering::SeqCst) {
            return;
        }
        if crashes.is_empty() {
            set_status(app, backend, "starting", None);
        }
        let child = match backend_command(app, backend).and_then(|mut c| c.spawn().map_err(|e| e.to_string())) {
            Ok(child) => child,
            Err(error) => {
                set_status(app, backend, "failed", Some(format!("Could not start the backend: {error}")));
                return;
            }
        };
        // Dropped when this run ends, which kills anything the backend left running.
        let _job = match job::Job::for_process(&child) {
            Ok(job) => Some(job),
            Err(error) => {
                log_line(backend, &format!("could not create job object: {error}"));
                None
            }
        };
        *backend.child.lock().unwrap() = Some(child);

        let (exit, message) = wait_for_exit(app, backend);
        if let Some(mut child) = backend.child.lock().unwrap().take() {
            let _ = child.wait();
        }
        if backend.stopping.load(Ordering::SeqCst) {
            return;
        }
        let code = exit.and_then(|status| status.code());
        if code == Some(EXIT_ALREADY_RUNNING) {
            set_status(
                app,
                backend,
                "failed",
                Some(
                    "Another Multi-Agent Hub server is already running (for example `uv run python -m app.main`). \
                     Close it, then retry."
                        .into(),
                ),
            );
            return;
        }

        let now = Instant::now();
        crashes.retain(|at| now.duration_since(*at) < CRASH_WINDOW);
        crashes.push(now);
        let reason = message.unwrap_or_else(|| match code {
            Some(code) => format!("The backend exited with code {code}."),
            None => "The backend stopped.".into(),
        });
        log_line(backend, &format!("shell: {reason}"));
        if crashes.len() > MAX_CRASHES {
            set_status(
                app,
                backend,
                "failed",
                Some(format!(
                    "{reason} It crashed {} times in two minutes, so it was not restarted. See {}",
                    crashes.len(),
                    backend.log_file.display()
                )),
            );
            return;
        }
        set_status(app, backend, "restarting", Some(format!("{reason} Restarting…")));
        let backoff = Duration::from_secs(1 << (crashes.len() - 1).min(3));
        thread::sleep(backoff);
    }
}

/// Polls the running backend: reports "ready" once it answers, and returns when it exits
/// (or when it fails to come up in time, in which case it is killed).
fn wait_for_exit(app: &AppHandle, backend: &Backend) -> (Option<ExitStatus>, Option<String>) {
    let started = Instant::now();
    let mut ready = false;
    loop {
        {
            let mut guard = backend.child.lock().unwrap();
            let Some(child) = guard.as_mut() else { return (None, None) };
            match child.try_wait() {
                Ok(Some(status)) => return (Some(status), None),
                Ok(None) => {}
                Err(error) => return (None, Some(format!("Lost track of the backend: {error}."))),
            }
        }
        if !ready {
            if health_ok(backend.port, &backend.token) {
                ready = true;
                set_status(app, backend, "ready", None);
            } else if started.elapsed() > START_TIMEOUT {
                kill_backend(backend);
                return (None, Some(format!("The backend did not start within {} s.", START_TIMEOUT.as_secs())));
            }
        }
        thread::sleep(Duration::from_millis(if ready { 500 } else { 200 }));
    }
}

/// A plain HTTP request, so the shell needs no HTTP client library.
fn health_ok(port: u16, token: &str) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(500)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let request = format!(
        "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut head = [0u8; 12];
    stream.read_exact(&mut head).is_ok() && head.starts_with(b"HTTP/1.1 200")
}

fn backend_command(app: &AppHandle, backend: &Backend) -> Result<Command, String> {
    let mut command = if let Ok(exe) = std::env::var("HUB_BACKEND_EXE") {
        Command::new(exe)
    } else if cfg!(debug_assertions) {
        // `npm run tauri dev`: run the backend from source.
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..").join("backend");
        let mut command = Command::new("uv");
        command.args(["run", "python", "-m", "app.main"]).current_dir(dir);
        command
    } else {
        let dir = app.path().resource_dir().map_err(|e| e.to_string())?.join("backend");
        let mut command = Command::new(dir.join("hub-backend.exe"));
        command.current_dir(dir);
        command
    };
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&backend.log_file)
        .map_err(|e| format!("cannot open {}: {e}", backend.log_file.display()))?;
    let log_err = log.try_clone().map_err(|e| e.to_string())?;
    command
        .env("HUB_TOKEN", &backend.token)
        .env("HUB_PORT", backend.port.to_string())
        .env("HUB_HOST", "127.0.0.1")
        .env("HUB_RELOAD", "false")
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(log_err);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    Ok(command)
}

/// Kills the backend and everything it started.
fn kill_backend(backend: &Backend) {
    let mut guard = backend.child.lock().unwrap();
    let Some(child) = guard.as_mut() else { return };
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let _ = Command::new("taskkill")
            .args(["/T", "/F", "/PID", &child.id().to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(0x0800_0000)
            .status();
    }
    let _ = child.kill();
}

fn log_line(backend: &Backend, line: &str) {
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(&backend.log_file) {
        let _ = writeln!(file, "{line}");
    }
}

fn free_port() -> std::io::Result<u16> {
    Ok(TcpListener::bind("127.0.0.1:0")?.local_addr()?.port())
}

fn new_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).expect("the OS random number generator failed");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn prepare_log_file(app: &AppHandle) -> PathBuf {
    let dir = app
        .path()
        .local_data_dir()
        .unwrap_or_else(|_| std::env::temp_dir())
        .join("MultiAgentHub")
        .join("logs");
    let _ = fs::create_dir_all(&dir);
    let file = dir.join("backend.log");
    if fs::metadata(&file).map(|m| m.len() > LOG_ROTATE_BYTES).unwrap_or(false) {
        let _ = fs::rename(&file, dir.join("backend.old.log"));
    }
    file
}

pub fn run() {
    let app = tauri::Builder::default()
        // Opening the app again focuses the running window instead of starting a second backend.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .invoke_handler(tauri::generate_handler![hub_connection, backend_status, restart_backend])
        .setup(|app| {
            let backend = Arc::new(Backend {
                port: free_port()?,
                token: new_token(),
                log_file: prepare_log_file(app.handle()),
                status: Mutex::new(BackendStatus { state: "starting", message: None }),
                child: Mutex::new(None),
                stopping: AtomicBool::new(false),
                supervising: AtomicBool::new(false),
            });
            app.manage(backend.clone());
            start_supervisor(app.handle().clone(), backend);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Tauri app");

    app.run(|app, event| {
        if let RunEvent::Exit = event {
            let backend = app.state::<Arc<Backend>>();
            backend.stopping.store(true, Ordering::SeqCst);
            kill_backend(&backend);
        }
    });
}
