#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Black Eclipse — native shell.
//!
//! Scope: a WebView2 window and the lifecycle of ONE child process (the Node
//! server bootstrap in `scripts/desktop-server.js`). The Next server already
//! supervises the Python harness, so this shell deliberately knows nothing
//! about Python.
//!
//! Origin: the window loads `http://127.0.0.1:3100`. Loopback is already a
//! secure context, so notifications, camera and microphone keep working
//! without the TLS reverse proxy the browser path uses.
//!
//! The browser front door (`npm run dev`, `npm start`, `black eclipse`,
//! `start-eclipse.vbs`) is untouched by this crate.

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{
    AppHandle, Emitter, Listener, Manager, RunEvent, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
    WindowEvent,
};

/// Stable channel port.
const APP_PORT: u16 = 3100;

/// Dev channel port. Both ports are listed in `capabilities/default.json`, so
/// notifications work in either window; other ports do not.
const DEV_PORT: u16 = 3101;

/// `BE_DESKTOP_PORT` lets a test (or an operator) move both the bootstrap and
/// the window together; without it the shell and the bootstrap could disagree.
fn app_port() -> u16 {
    std::env::var("BE_DESKTOP_PORT")
        .ok()
        .and_then(|v| v.trim().parse::<u16>().ok())
        .unwrap_or(APP_PORT)
}

/// Escapes the app's `X-Frame-Options`/CSP concerns: the splash is local
/// bundled HTML served over Tauri's own asset protocol.
const SPLASH: &str = "index.html";

/// Injected before any application script runs. WebView2 does not implement
/// the Notification API, so the app's alerts settings screen would report "not
/// supported" inside the desktop window. This maps the standard constructor onto
/// a native toast, so the existing browser-notifier code keeps working unchanged.
///
/// IMPORTANT: this talks to Rust through **events**, not `invoke`. Tauri 2 denies
/// app-defined commands to remote origins (the window loads
/// `http://127.0.0.1:3100`, which is remote as far as the ACL is concerned) —
/// `invoke('desktop_notify')` fails with "not allowed. Plugin not found". Core
/// event commands ARE permitted to a capability-listed remote origin, so
/// `be-notify` / `be-shortcut` are the transport.
const INIT_SCRIPT: &str = r#"
(function () {
  var T = window.__TAURI__;
  var emit = T && T.event && typeof T.event.emit === 'function' ? T.event.emit : null;
  // If the global API is not exposed (an older build, or a remote origin the
  // capability did not list), fall back to the raw IPC invoke. Core event
  // commands are the one surface Tauri guarantees to a capability-listed
  // remote origin, so window chrome and notifications keep working.
  if (!emit) {
    var I = window.__TAURI_INTERNALS__;
    if (I && typeof I.invoke === 'function') {
      emit = function (name, payload) {
        return I.invoke('plugin:event|emit', { event: name, payload: payload });
      };
    }
  }
  if (!emit) return;

  // The tab strip is part of the page, not a separate title bar: keep it
  // transparent. Injected because this shell may load a payload whose CSS still
  // painted it with the page-background token.
  try {
    var stripStyle = document.createElement('style');
    stripStyle.textContent = '.desktop-tab-bar{background-color:transparent!important}';
    (document.head || document.documentElement).appendChild(stripStyle);
  } catch (e) {}

  // Desktop default: turn OS notifications ON for a first run, without ever
  // overriding a user who has explicitly turned them off. The storage key is the
  // one `src/lib/alerts/browser-notifier.ts` reads. The browser keeps its opt-in
  // behaviour because this script only runs inside the shell.
  try {
    if (window.localStorage.getItem('be_alerts_browser_v1') === null) {
      window.localStorage.setItem('be_alerts_browser_v1', '1');
    }
  } catch (e) {}

  // Desktop default: open with the FULL sidebar. The icon rail is a choice the
  // user can still make at runtime (Ctrl/Cmd+B); it must not be restored from an
  // earlier session and leave the desktop shell looking collapsed.
  try {
    window.localStorage.setItem('be_sidebar_collapsed_v1', '0');
  } catch (e) {}

  function NativeNotification(title, opts) {    opts = opts || {};
    this.title = title;
    this.body = opts.body || '';
    this.tag = opts.tag || '';
    this.icon = opts.icon || '';
    this.onclick = null;
    this.onclose = null;
    this.onerror = null;
    try {
      emit('be-notify', {
        title: String(title == null ? 'Black Eclipse' : title),
        body: String(opts.body == null ? '' : opts.body)
      });
    } catch (e) {}
  }
  NativeNotification.permission = 'granted';
  NativeNotification.maxActions = 2;
  NativeNotification.requestPermission = function () {
    return Promise.resolve('granted');
  };
  NativeNotification.prototype.close = function () {};
  try {
    Object.defineProperty(window, 'Notification', {
      value: NativeNotification,
      writable: true,
      configurable: true
    });
  } catch (e) {}

  // Shell-level shortcuts. Deliberately no Ctrl+R: reloading the app would
  // discard unsaved composer state, and the app's own shortcuts already cover
  // navigation.
  window.addEventListener('keydown', function (e) {
    try {
      if (e.key === 'F11') {
        e.preventDefault();
        emit('be-shortcut', { action: 'fullscreen' });
      } else if (e.ctrlKey && e.shiftKey && (e.key === 'I' || e.key === 'i')) {
        e.preventDefault();
        emit('be-shortcut', { action: 'devtools' });
      }
    } catch (err) {}
  }, true);

  // Frameless window chrome. The app's tab strip marks empty space with
  // `data-be-drag`; interactive children opt out with `data-be-no-drag`. Window
  // buttons (and the splash) use the same `be-window` event -> Rust bridge.
  function windowAction(action) {
    try { emit('be-window', { action: action }); } catch (e) {}
  }
  window.addEventListener('be-window-action', function (e) {
    var detail = (e && e.detail) || {};
    if (detail.action) emit('be-window', detail);
  });
  document.addEventListener('mousedown', function (e) {
    if (e.button !== 0) return;
    var target = e.target;
    if (!target || !target.closest) return;
    if (target.closest('[data-be-no-drag]')) return;
    if (!target.closest('[data-be-drag]')) return;
    windowAction('start-drag');
  }, true);
  document.addEventListener('dblclick', function (e) {
    var target = e.target;
    if (!target || !target.closest) return;
    if (target.closest('[data-be-no-drag]') || !target.closest('[data-be-drag]')) return;
    windowAction('toggle-maximize');
  }, true);
})();
"#;

/// Current prefs schema. Bump when a stored *default* must be re-interpreted.
/// v1: `keep_warm` defaults to false — see `migrate_prefs`.
const PREFS_VERSION: u32 = 1;

#[derive(Serialize, Deserialize, Clone, Debug)]
struct Prefs {
    /// Absent in files written before v1 (`#[serde(default)]` ⇒ 0), which is
    /// what the one-time close-behaviour migration keys on.
    #[serde(default)]
    version: u32,
    keep_warm: bool,
    /// Start (minimized) when the operator signs in. Set together with
    /// `keep_warm` by the one tray switch: autostart without keep-warm still
    /// quits on close, and keep-warm without autostart does not survive reboot,
    /// so neither alone is "always on".
    #[serde(default)]
    auto_start: bool,
    width: u32,
    height: u32,
    x: Option<i32>,
    y: Option<i32>,
    maximized: bool,
    /// Translucent, acrylic-blurred window (desktop only). The app's sidebar
    /// stays opaque; see `apply_window_glass`.
    #[serde(default = "default_window_glass")]
    window_glass: bool,
}

fn default_window_glass() -> bool {
    true
}

impl Default for Prefs {
    fn default() -> Self {
        Self {
            // Closing the window must close the app. Keep-warm is opt-in from
            // the tray (and remembered); it is not the out-of-the-box answer,
            // because a window that only minimizes on close reads as a bug.
            keep_warm: false,
            auto_start: false,
            version: PREFS_VERSION,
            width: 1440,
            height: 900,
            x: None,
            y: None,
            maximized: false,
            window_glass: true,
        }
    }
}

/// Older builds defaulted `keep_warm` to true and persisted it on every bounds
/// save, so a stored `true` cannot be told apart from an explicit choice.
/// Reset it exactly once (files without a `version` field) instead of making
/// every existing install keep minimizing on close forever.
fn migrate_prefs(path: &PathBuf, prefs: &mut Prefs) {
    if prefs.version >= PREFS_VERSION {
        return;
    }
    prefs.keep_warm = false;
    prefs.version = PREFS_VERSION;
    save_prefs(path, prefs);
    log_line("[shell] migrated prefs: close now quits (keep-warm is opt-in)");
}

struct AppState {
    root: PathBuf,
    /// Runtime state (locks, log, prefs). Its parent is the data directory.
    runtime_dir: PathBuf,
    prefs_file: PathBuf,
    prefs: Mutex<Prefs>,
    child: Mutex<Option<Child>>,
    /// Throttles bounds writes: Windows emits drag/resize events continuously.
    last_bounds_save: Mutex<Instant>,
    /// One supervisor at a time; the retry button must not stack boot attempts.
    supervising: AtomicBool,
    /// True only when this shell started the server. An attached server belongs
    /// to the browser path and must never be killed on quit.
    owns_server: AtomicBool,
    quitting: AtomicBool,
    /// Presentation state for the borderless window. Windows' native `maximize`
    /// fills the whole monitor (taskbar included) for a frameless window, so the
    /// shell emulates maximize by filling the monitor's WORK AREA instead.
    maximized: AtomicBool,
    /// Last unread total the page asked the taskbar to show. Held here so the
    /// badge survives a hide/show cycle: Windows destroys the taskbar button
    /// when the window hides (keep-warm) and does not restore its overlay.
    taskbar_unread: AtomicI64,
    /// Whether a stream was working when the page last spoke. Re-applied on
    /// focus for the same reason as the badge.
    taskbar_working: AtomicBool,
}

fn log_line(line: &str) {
    // Bounded, append-only. Never let a chatty server fill the disk.
    let path = std::env::var("BE_DESKTOP_LOG").ok().map(PathBuf::from);
    if let Some(path) = path {
        if let Ok(meta) = fs::metadata(&path) {
            if meta.len() > 8 * 1024 * 1024 {
                let _ = fs::write(&path, b"");
            }
        }
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&path) {
            let _ = writeln!(f, "{line}");
        }
    }
}

/// Where user data lives, mirroring `scripts/desktop-server.js`. Used only when
/// the install directory cannot be written to.
fn data_fallback_dir(root: &std::path::Path) -> PathBuf {
    if let Ok(dir) = std::env::var("BE_DATA_DIR") {
        if !dir.trim().is_empty() {
            return PathBuf::from(dir);
        }
    }
    #[cfg(windows)]
    {
        if let Ok(local) = std::env::var("LOCALAPPDATA") {
            return PathBuf::from(local).join("BlackEclipse").join("data");
        }
    }
    #[cfg(target_os = "macos")]
    {
        if let Ok(home) = std::env::var("HOME") {
            return PathBuf::from(home).join("Library/Application Support/BlackEclipse/data");
        }
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        if let Ok(xdg) = std::env::var("XDG_DATA_HOME") {
            if !xdg.trim().is_empty() {
                return PathBuf::from(xdg).join("BlackEclipse/data");
            }
        }
        if let Ok(home) = std::env::var("HOME") {
            return PathBuf::from(home).join(".local/share/BlackEclipse/data");
        }
    }
    root.to_path_buf()
}

/// Locate the repository root by walking up from the executable and looking for
/// the bootstrap script. Works for `target/debug`, `target/release` and a
/// future packaged layout without any hard-coded depth.
fn resolve_root() -> Option<PathBuf> {
    // Packaged layout: <exe dir>\app\scripts\desktop-server.js
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let packaged = dir.join("app");
            if packaged.join("scripts/desktop-server.js").exists() {
                return Some(packaged);
            }
        }
    }
    if let Ok(explicit) = std::env::var("BE_DESKTOP_ROOT") {
        let p = PathBuf::from(explicit);
        if p.join("scripts/desktop-server.js").exists() {
            return Some(p);
        }
    }
    let mut dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
    for _ in 0..10 {
        if dir.join("scripts/desktop-server.js").exists() {
            return Some(dir);
        }
        dir = dir.parent()?.to_path_buf();
    }
    None
}

/// Node is required. A packaged install ships `<exe dir>\runtime\node.exe`, so it
/// never depends on the user having Node; a repository checkout uses PATH first.
fn find_node() -> Option<String> {
    // A packaged install ships its own Node, named per platform.
    let node_name = if cfg!(windows) { "node.exe" } else { "node" };
    let bundled = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|d| d.join("runtime").join(node_name)));
    if let Some(path) = bundled {
        if path.exists() {
            return Some(path.to_string_lossy().to_string());
        }
    }

    if Command::new("node")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
    {
        return Some("node".to_string());
    }

    let mut candidates: Vec<PathBuf> = Vec::new();

    #[cfg(windows)]
    {
        if let Ok(pf) = std::env::var("ProgramFiles") {
            candidates.push(PathBuf::from(pf).join("nodejs").join("node.exe"));
        }
        if let Ok(pf) = std::env::var("ProgramFiles(x86)") {
            candidates.push(PathBuf::from(pf).join("nodejs").join("node.exe"));
        }
        if let Ok(la) = std::env::var("LOCALAPPDATA") {
            candidates.push(
                PathBuf::from(la)
                    .join("Programs")
                    .join("nodejs")
                    .join("node.exe"),
            );
        }
    }

    #[cfg(not(windows))]
    {
        // Common install locations for a developer checkout. A packaged install
        // always finds its bundled runtime above and never reaches here.
        for p in ["/usr/local/bin/node", "/opt/homebrew/bin/node", "/usr/bin/node"] {
            candidates.push(PathBuf::from(p));
        }
    }

    candidates
        .into_iter()
        .find(|p| p.exists())
        .map(|p| p.to_string_lossy().to_string())
}

fn emit_status(app: &AppHandle, kind: &str, message: &str) {
    let _ = app.emit(
        "be-status",
        serde_json::json!({ "kind": kind, "message": message }),
    );
}

fn load_prefs(path: &PathBuf) -> Prefs {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str::<Prefs>(&s).ok())
        .unwrap_or_default()
}

fn save_prefs(path: &PathBuf, prefs: &Prefs) {
    if let Ok(json) = serde_json::to_string_pretty(prefs) {
        let _ = fs::write(path, json);
    }
}

/// The HKCU Run value that launches this build at sign-in.
#[cfg(windows)]
const LOGIN_ITEM_NAME: &str = "Black Eclipse";

/// The exact `reg.exe` invocation. Pure so the value name, the
/// `--background` argument and the quoted path are testable without touching a
/// real registry.
#[cfg(windows)]
fn login_item_command(enabled: bool, exe: &std::path::Path) -> (String, Vec<String>) {
    let key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run";
    if enabled {
        (
            "reg".to_string(),
            vec![
                "add".to_string(),
                key.to_string(),
                "/v".to_string(),
                LOGIN_ITEM_NAME.to_string(),
                "/t".to_string(),
                "REG_SZ".to_string(),
                "/d".to_string(),
                format!("\"{}\" --background", exe.display()),
                "/f".to_string(),
            ],
        )
    } else {
        (
            "reg".to_string(),
            vec![
                "delete".to_string(),
                key.to_string(),
                "/v".to_string(),
                LOGIN_ITEM_NAME.to_string(),
                "/f".to_string(),
            ],
        )
    }
}

#[cfg(windows)]
fn login_item_present() -> bool {
    Command::new("reg")
        .args([
            "query",
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
            "/v",
            LOGIN_ITEM_NAME,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

/// Register/unregister the sign-in launch. Returns an error instead of
/// pretending: the tray switch is only turned on when the registry write
/// actually succeeded.
#[cfg(windows)]
fn set_login_item(enabled: bool) -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|err| format!("current_exe: {err}"))?;
    let (program, args) = login_item_command(enabled, &exe);
    let output = Command::new(&program)
        .args(&args)
        .output()
        .map_err(|err| format!("{program} failed to start: {err}"))?;
    if output.status.success() {
        return Ok(());
    }
    // Deleting a value that is already absent is success, not a lie.
    if !enabled && !login_item_present() {
        return Ok(());
    }
    Err(format!(
        "{program} exited {}: {}",
        output.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&output.stderr).trim()
    ))
}

/// Only Windows ships today (the browser lane is deleted and no macOS/Linux
/// shell exists). If one lands, replace this one function with the platform's
/// login-item API; nothing else knows how autostart is implemented.
#[cfg(not(windows))]
fn set_login_item(_enabled: bool) -> Result<(), String> {
    Err("starting at sign-in is not implemented on this platform".to_string())
}

/// One switch, two prefs: "always on" means start at sign-in AND keep running
/// when the window closes. Either alone leaves a silent gap. Nothing is written
/// unless the registry write succeeded.
fn set_background(state: &AppState, enabled: bool) -> Result<(), String> {
    let mut prefs = state
        .prefs
        .lock()
        .map_err(|_| "prefs lock poisoned".to_string())?;
    set_login_item(enabled)?;
    prefs.auto_start = enabled;
    prefs.keep_warm = enabled;
    save_prefs(&state.prefs_file, &prefs);
    Ok(())
}

/// Kill the whole process tree. `next start` runs as a grandchild (Rust →
/// bootstrap → next), and `Child::kill` does not reap descendants on Windows.
fn kill_tree(state: &AppState) {
    if !state.owns_server.load(Ordering::SeqCst) {
        return;
    }
    let mut guard = match state.child.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    if let Some(mut child) = guard.take() {
        let pid = child.id();
        #[cfg(windows)]
        {
            let _ = Command::new("taskkill")
                .args(["/PID", &pid.to_string(), "/T", "/F"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        // On Unix the child was started in its own session (see `spawn_bootstrap`),
        // so signalling the process group takes the Next server and the Python
        // harness with it — `Child::kill` alone would orphan both.
        #[cfg(unix)]
        {
            unsafe {
                libc::kill(-(pid as i32), libc::SIGKILL);
            }
        }
        let _ = child.kill();
        let _ = child.wait();
        log_line(&format!("[shell] server tree {pid} stopped"));
    }
}

/// One-time, dismissible notice injected after the app loads.
///
/// The installed app keeps its data in the platform data directory, which is NOT
/// the repository's `jdds.db`. A user coming from the browser version opens the
/// desktop app and sees an empty workspace with no explanation. This says where
/// the data is, that nothing was imported, and that nothing was deleted.
fn maybe_show_first_run_notice(app: &AppHandle, data_dir: &std::path::Path) {
    let marker = data_dir.join("first-run-notice-shown");
    if marker.exists() {
        return;
    }
    let win = match app.get_webview_window("main") {
        Some(w) => w,
        None => return,
    };
    let shown = serde_json::to_string(&data_dir.to_string_lossy().to_string())
        .unwrap_or_else(|_| "\"\"".to_string());
    // Placement and timing are deliberate:
    //   * TOP-right, never bottom-right: the setup wizard's primary
    //     ("Finish setup") lives in the bottom-right of its card and this card
    //     used to sit directly on top of it, swallowing the click.
    //   * Never shown while the setup wizard is on screen: a first run has not
    //     reached the data story yet, and it must stay the only thing visible.
    //     `OnboardingShell` also contains a shim for builds that predate this.
    //   * Never intercepts pointer events; only the dismiss button is clickable.
    //   * Shown ONCE, remembered on both sides: the legacy marker file (an
    //     install that already saw it) and webview localStorage (written only
    //     when the card was actually appended).
    let script = format!(
        r#"(function () {{
  if (document.getElementById('be-first-run')) return;
  try {{ if (localStorage.getItem('be-first-run-notice') === '1') return; }} catch (e) {{}}
  if (document.querySelector('[role="dialog"][aria-label="BlackEclipse setup"]')) return;
  var wrap = document.createElement('div');
  wrap.id = 'be-first-run';
  wrap.style.cssText = 'position:fixed;top:18px;right:18px;z-index:2147483000;max-width:400px;padding:16px 18px;border-radius:14px;border:1px solid rgba(255,255,255,0.12);background:rgba(24,24,27,0.96);color:#fafafa;font-family:"Segoe UI",system-ui,sans-serif;font-size:12.5px;line-height:1.55;box-shadow:0 18px 50px -12px rgba(0,0,0,0.8);pointer-events:none';
  var title = document.createElement('div');
  title.textContent = 'Where your data lives';
  title.style.cssText = 'font-weight:600;font-size:13px;margin-bottom:6px';
  var body = document.createElement('div');
  body.style.cssText = 'color:rgba(250,250,250,0.66)';
  body.textContent = 'This app stores its data in ' + {shown} + '. If you used the browser version before, that data was not imported and nothing was deleted — it is still where it was.';
  var row = document.createElement('div');
  row.style.cssText = 'margin-top:12px;display:flex;gap:8px;justify-content:flex-end';
  var btn = document.createElement('button');
  btn.textContent = 'Got it';
  btn.style.cssText = 'font:inherit;font-size:12px;padding:6px 12px;border-radius:8px;border:1px solid rgba(255,255,255,0.14);background:rgba(255,255,255,0.08);color:#fafafa;cursor:pointer;pointer-events:auto';
  btn.addEventListener('click', function () {{ wrap.remove(); }});
  row.appendChild(btn);
  wrap.appendChild(title);
  wrap.appendChild(body);
  wrap.appendChild(row);
  document.body.appendChild(wrap);
  try {{ localStorage.setItem('be-first-run-notice', '1'); }} catch (e) {{}}
}})();"#
    );
    let _ = win.eval(script);
    // The card may decline to appear (setup is open). Marking the file here
    // would then lose the notice forever, so the webview records "shown".
    log_line("[shell] first-run data notice requested");
}

fn navigate_to_app(app: &AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let url: tauri::Url = match format!("http://127.0.0.1:{}/", app_port()).parse() {
            Ok(u) => u,
            Err(_) => return,
        };
        let _ = win.navigate(url);
        maybe_run_diagnostics(app);

        // The notice is injected after the page has had time to render; it is
        // written once, so it never nags on later launches.
        let handle = app.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(5));
            let state = handle.state::<AppState>();
            if let Some(data_dir) = state.runtime_dir.parent() {
                let data_dir = data_dir.to_path_buf();
                maybe_show_first_run_notice(&handle, &data_dir);
            }
        });
    }
}

/// Replace the current page with an in-place failure panel. Used when an owned
/// server dies after the app is already showing: a dead UI that silently
/// swallows every click is the worst possible outcome.
fn show_failure_overlay(app: &AppHandle, message: &str) {
    let win = match app.get_webview_window("main") {
        Some(w) => w,
        None => return,
    };
    let quoted = serde_json::to_string(message).unwrap_or_else(|_| "\"Server stopped\"".to_string());
    let script = format!(
        r#"(function () {{
  if (document.getElementById('be-failure')) return;
  var wrap = document.createElement('div');
  wrap.id = 'be-failure';
  wrap.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;background:rgba(8,8,10,0.94);font-family:"Segoe UI",system-ui,sans-serif;color:#fafafa';
  var card = document.createElement('div');
  card.style.cssText = 'max-width:440px;padding:28px 30px;border:1px solid rgba(255,255,255,0.1);border-radius:16px;background:rgba(24,24,27,0.92);text-align:center';
  var title = document.createElement('div');
  title.textContent = 'Black Eclipse';
  title.style.cssText = 'font-size:15px;font-weight:600;margin-bottom:8px';
  var body = document.createElement('div');
  body.textContent = {quoted};
  body.style.cssText = 'font-size:12.5px;color:rgba(250,250,250,0.62);line-height:1.55;margin-bottom:16px';
  var btn = document.createElement('button');
  btn.textContent = 'Reload';
  btn.style.cssText = 'font:inherit;font-size:12px;padding:7px 14px;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:rgba(255,255,255,0.08);color:#fafafa;cursor:pointer';
  btn.addEventListener('click', function () {{ location.reload(); }});
  card.appendChild(title);
  card.appendChild(body);
  card.appendChild(btn);
  wrap.appendChild(card);
  document.body.appendChild(wrap);
}})();"#
    );
    let _ = win.eval(script);
}

/// WebView2 is part of Windows 11 but not guaranteed on Windows 10. Detect it
/// before building the window so a missing runtime produces instructions rather
/// than a shell that opens nothing.
#[cfg(windows)]
fn webview2_version() -> Option<String> {
    const KEYS: [&str; 3] = [
        r"HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        r"HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        r"HKCU\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    ];
    for key in KEYS {
        let out = Command::new("reg").args(["query", key, "/v", "pv"]).output();
        if let Ok(out) = out {
            if out.status.success() {
                let text = String::from_utf8_lossy(&out.stdout);
                if let Some(line) = text.lines().find(|l| l.contains("REG_SZ")) {
                    if let Some(pv) = line.split("REG_SZ").nth(1) {
                        let v = pv.trim().to_string();
                        if !v.is_empty() {
                            return Some(v);
                        }
                    }
                }
            }
        }
    }
    None
}

#[cfg(windows)]
fn message_box(text: &str) {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;

    #[link(name = "user32")]
    extern "system" {
        fn MessageBoxW(hwnd: *mut std::ffi::c_void, text: *const u16, caption: *const u16, utype: u32) -> i32;
    }

    let wide = |s: &str| -> Vec<u16> { OsStr::new(s).encode_wide().chain(Some(0)).collect() };
    let title = wide("Black Eclipse");
    let body = wide(text);
    // 0x10 = MB_ICONERROR, 0x0 = MB_OK, foreground
    unsafe {
        MessageBoxW(std::ptr::null_mut(), body.as_ptr(), title.as_ptr(), 0x10);
    }
}

/// Thread: run one supervision attempt, then allow the splash's retry button to
/// start another. A stuck `supervising` flag would make "Try again" dead.
fn start_supervisor(app: AppHandle) {
    std::thread::spawn(move || {
        if app.state::<AppState>().supervising.swap(true, Ordering::SeqCst) {
            return;
        }
        supervise(app.clone());
        app.state::<AppState>()
            .supervising
            .store(false, Ordering::SeqCst);
    });
}

/// Spawn the bootstrap, stream its output to the log, and translate the
/// `@@BE:*@@` markers into splash events.
fn supervise(app: AppHandle) {
    let state = app.state::<AppState>();
    let root = state.root.clone();

        let node = match find_node() {
            Some(n) => n,
            None => {
                emit_status(
                    &app,
                    "error",
                    "Node.js was not found in this installation. Reinstall Black Eclipse from the setup file, or install Node.js 20+ from nodejs.org, then click Try again.",
                );
                return;
            }
        };

        emit_status(&app, "info", "Starting Black Eclipse…");

        let mut cmd = Command::new(&node);
        cmd.arg("scripts/desktop-server.js")
            .current_dir(&root)
            .env("BE_DESKTOP_SHELL", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            // CREATE_NO_WINDOW: no console flash for the child.
            cmd.creation_flags(0x0800_0000);
        }
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            // Own session, so `kill_tree` can signal the whole group and take the
            // Next server and the Python harness down with the bootstrap.
            unsafe {
                cmd.pre_exec(|| {
                    if libc::setsid() == -1 {
                        return Err(std::io::Error::last_os_error());
                    }
                    Ok(())
                });
            }
        }

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                emit_status(&app, "error", &format!("Could not start the server: {e}"));
                return;
            }
        };

        let stdout = child.stdout.take();
        let stderr = child.stderr.take();

        // Store the handle IMMEDIATELY. The server keeps writing to stdout for
        // its whole life, so the read loop below never returns while it is
        // healthy — deferring the store would leave the process unkillable.
        if let Ok(mut guard) = state.child.lock() {
            *guard = Some(child);
        }

        if let Some(err) = stderr {
            std::thread::spawn(move || {
                // Byte-wise, lossy: Next and the Python harness can emit bytes
                // that are not valid UTF-8 on a Windows console. `lines()` would
                // return an error and end the stream, silently killing the log.
                let mut reader = BufReader::new(err);
                let mut buf = Vec::new();
                loop {
                    buf.clear();
                    match reader.read_until(b'\n', &mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {
                            let line = String::from_utf8_lossy(&buf);
                            log_line(&format!("[server:err] {}", line.trim_end()));
                        }
                    }
                }
            });
        }

        let mut ready = false;

        if let Some(out) = stdout {
            let mut reader = BufReader::new(out);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match reader.read_until(b'\n', &mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {}
                }
                let line = String::from_utf8_lossy(&buf).trim_end().to_string();
                if let Some(rest) = line.strip_prefix("@@BE:") {
                    if let Some((kind, payload)) = rest.split_once("@@") {
                        match kind {
                            "ATTACH" => {
                                state.owns_server.store(false, Ordering::SeqCst);
                                // The bootstrapper exits right after this marker:
                                // release the handle so we never taskkill a
                                // server that belongs to the browser path.
                                if let Ok(mut guard) = state.child.lock() {
                                    guard.take();
                                }
                                log_line(&format!(
                                    "[shell] attaching to an already-running server ({payload})"
                                ));
                                emit_status(&app, "info", "Connecting to the running server...");
                                navigate_to_app(&app);
                                ready = true;
                            }
                            "CONFLICT" => {
                                state.owns_server.store(false, Ordering::SeqCst);
                                if let Ok(mut guard) = state.child.lock() {
                                    guard.take();
                                }
                                log_line(&format!("[shell] conflict: {payload}"));
                                emit_status(&app, "error", payload);
                            }
                            "WARN" => {
                                log_line(&format!("[shell] warn: {payload}"));
                                emit_status(&app, "info", payload);
                            }
                            "ERROR" => {
                                log_line(&format!("[shell] error: {payload}"));
                                emit_status(&app, "error", payload);
                            }
                            "READY" => {
                                state.owns_server.store(true, Ordering::SeqCst);
                                emit_status(&app, "ready", "Opening...");
                                navigate_to_app(&app);
                                ready = true;
                            }
                            _ => {}
                        }
                        continue;
                    }
                }
                log_line(&format!("[server] {line}"));
            }
        }

        // Reap the bootstrap if it exited on its own; a live server keeps its
        // handle in state for shutdown.
        let exited = match state.child.lock() {
            Ok(mut guard) => guard.take(),
            Err(_) => None,
        };
        if let Some(mut child) = exited {
            let _ = child.wait();
        }

        if ready && state.owns_server.load(Ordering::SeqCst) {
            // The server died AFTER the app was showing. Swap the page for an
            // in-place failure panel rather than leaving a dead UI that silently
            // swallows every click.
            log_line("[shell] owned server exited after readiness");
            show_failure_overlay(
                &app,
                "The local server stopped. Your data is safe — nothing was deleted. Click Reload to start it again. If this keeps happening, open the log folder from the start screen to see the first error.",
            );
            emit_status(
                &app,
                "error",
                "The local server stopped. Click Try again. Your data is safe.",
            );
        } else if !ready {
            emit_status(
                &app,
                "error",
                "The server stopped before the app was ready. Click Try again; if it fails twice, use Open log folder and send the last error line.",
            );
        }
}

/// After the window has navigated to the app, prove the whole notification
/// chain: init script installed → `Notification` shim → native command → OS
/// toast. Gated by `BE_DESKTOP_DIAGNOSTICS=1` so it can never fire in normal use.
///
/// The probe reports through the webview URL (`history.replaceState`, no reload)
/// rather than through IPC, because proving IPC is one of the things under test.
/// If IPC were the channel, a broken IPC would look identical to a broken probe.
fn maybe_run_diagnostics(app: &AppHandle) {
    if std::env::var("BE_DESKTOP_DIAGNOSTICS").ok().as_deref() != Some("1") {
        return;
    }
    let handle = app.clone();
    std::thread::spawn(move || {
        // The app is a heavy client; give hydration a moment.
        std::thread::sleep(Duration::from_secs(6));
        let win = match handle.get_webview_window("main") {
            Some(w) => w,
            None => return,
        };
        log_line("[diag] injecting notification probe into the app origin");

        // Safe tokens only: the result travels in the URL, so no decoding.
        let probe = r#"(function () {
  var safe = function (v) {
    return String(v == null ? 'none' : v).replace(/[^A-Za-z0-9_.-]/g, '');
  };
  var out = [];
  var T = window.__TAURI__;
  var invoke = T && T.core && T.core.invoke;
  out.push('hn:' + (typeof Notification !== 'undefined' ? 1 : 0));
  out.push('perm:' + safe(typeof Notification !== 'undefined' ? Notification.permission : 'undefined'));
  out.push('sec:' + (window.isSecureContext === true ? 1 : 0));
  out.push('tauri:' + (invoke ? 1 : 0));
  out.push('ctor:' + safe(typeof Notification !== 'undefined' && Notification.name ? Notification.name : 'none'));
  try {
    out.push('pref:' + safe(window.localStorage.getItem('be_alerts_browser_v1')));
  } catch (e) {
    out.push('pref:locked');
  }

  var shimCall = 'skipped';
  try {
    new Notification('Black Eclipse', { body: 'Diag: shim path.' });
    shimCall = 'dispatched';
  } catch (e) {
    shimCall = 'err-' + safe(e && e.message ? e.message : e);
  }
  out.push('shimcall:' + shimCall);

  var direct = (T && T.event && typeof T.event.emit === 'function')
    ? T.event.emit('be-notify', { title: 'Black Eclipse', body: 'Diag: direct event path.' })
        .then(function () { return 'ok'; })
        .catch(function (e) { return 'err-' + safe(e && e.message ? e.message : e); })
    : Promise.resolve('noevent');

  direct.then(function (result) {
    out.push('direct:' + result);
    try {
      history.replaceState(null, '', '/?diag=' + out.join('~'));
    } catch (e) {}
  });
})();"#;
        let _ = win.eval(probe);

        // Poll the webview URL for the report.
        for _ in 0..40 {
            std::thread::sleep(Duration::from_millis(500));
            if let Ok(url) = win.url() {
                let text = url.as_str();
                if let Some(idx) = text.find("diag=") {
                    let rest = &text[idx + 5..];
                    let payload = rest.split('&').next().unwrap_or(rest);
                    log_line(&format!("[diag] {payload}"));
                    return;
                }
            }
        }
        log_line("[diag] no report received (probe blocked or page replaced)");
    });
}

#[tauri::command]
fn desktop_diag_report(payload: String) {
    log_line(&format!("[diag] {payload}"));
}

/// Where this shell writes its own log. The splash shows it when startup fails;
/// a path the user can read is worth more than "check the logs".
#[tauri::command]
fn desktop_log_path() -> serde_json::Value {
    let log = std::env::var("BE_DESKTOP_LOG").ok();
    let dir = log
        .as_deref()
        .and_then(|value| std::path::Path::new(value).parent())
        .map(|p| p.to_string_lossy().to_string());
    serde_json::json!({ "log": log, "dir": dir })
}

/// Open the folder that holds the log (and the runtime state) in the OS file
/// manager. Nothing is executed; the path comes from this process's own env.
#[tauri::command]
fn desktop_open_log() {
    let Some(log) = std::env::var("BE_DESKTOP_LOG").ok() else {
        return;
    };
    let path = std::path::PathBuf::from(&log);
    let dir = path.parent().map(|p| p.to_path_buf()).unwrap_or(path);
    let opened = {
        #[cfg(windows)]
        {
            Command::new("explorer").arg(&dir).spawn()
        }
        #[cfg(target_os = "macos")]
        {
            Command::new("open").arg(&dir).spawn()
        }
        #[cfg(all(unix, not(target_os = "macos")))]
        {
            Command::new("xdg-open").arg(&dir).spawn()
        }
    };
    match opened {
        Ok(_) => log_line(&format!("[shell] opened the log folder {}", dir.to_string_lossy())),
        Err(err) => log_line(&format!("[shell] could not open the log folder: {err}")),
    }
}

/// Reveal a file in the OS file manager. The webview asks for this through the
/// `be-reveal-path` core event, the same transport as notifications. Only an
/// existing ABSOLUTE path is acted on, and nothing is executed: the file manager
/// highlights the item. A malicious payload can therefore reveal a path the user
/// already has, but cannot run a program.
fn reveal_path(raw: &str) {
    let path = PathBuf::from(raw);
    if !path.is_absolute() || !path.exists() {
        log_line(&format!(
            "[reveal] refused (an absolute, existing path is required): {raw}"
        ));
        return;
    }
    let opened = {
        #[cfg(windows)]
        {
            if path.is_dir() {
                Command::new("explorer").arg(&path).spawn()
            } else {
                Command::new("explorer")
                    .arg(format!("/select,{}", path.to_string_lossy()))
                    .spawn()
            }
        }
        #[cfg(target_os = "macos")]
        {
            Command::new("open").arg("-R").arg(&path).spawn()
        }
        #[cfg(all(unix, not(target_os = "macos")))]
        {
            let dir = if path.is_dir() {
                path.clone()
            } else {
                path.parent()
                    .map(|p| p.to_path_buf())
                    .unwrap_or_else(|| path.clone())
            };
            Command::new("xdg-open").arg(&dir).spawn()
        }
    };
    match opened {
        Ok(_) => log_line(&format!("[reveal] {}", path.to_string_lossy())),
        Err(err) => log_line(&format!("[reveal] failed: {err}")),
    }
}

/// Open an http(s) URL in the person's real browser.
///
/// The desktop window is a single WebView2 instance: a `target="_blank"` link
/// (the update download, a release page) is a new-window request the shell does
/// not host, so without this the click does nothing. Only http(s) is accepted —
/// a forged payload can open a web page, never run a local program.
fn open_external(raw: &str) {
    let url = raw.trim();
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        log_line(&format!("[external] refused (http/https only): {raw}"));
        return;
    }
    let opened = {
        #[cfg(windows)]
        {
            Command::new("cmd").args(["/C", "start", "", url]).spawn()
        }
        #[cfg(target_os = "macos")]
        {
            Command::new("open").arg(url).spawn()
        }
        #[cfg(all(unix, not(target_os = "macos")))]
        {
            Command::new("xdg-open").arg(url).spawn()
        }
    };
    match opened {
        Ok(_) => log_line(&format!("[external] opened {url}")),
        Err(err) => log_line(&format!("[external] failed: {err}")),
    }
}

/// Save a file the page produced into the user's Downloads folder.
///
/// Why this exists: the desktop window is WebView2, which has no download shelf
/// of its own — an `<a download>` click or a blob download can silently do
/// nothing. Every download button in the app therefore sends its bytes here
/// through the `be-save-download` event, and Rust writes a real file, reveals
/// it and reports the absolute path back over `be-download-saved`. The browser
/// lane keeps using the ordinary anchor download.
fn sanitise_file_name(raw: &str) -> String {
    let name: String = raw
        .chars()
        .map(|c| {
            if c.is_control() || matches!(c, '\\' | '/' | ':' | '<' | '>' | '"' | '|' | '?' | '*') {
                '-'
            } else {
                c
            }
        })
        .collect();
    let name = name.trim_matches(|c: char| c == ' ' || c == '.').replace("..", "-");
    let name = if name.is_empty() { "eclipse-download".to_string() } else { name };
    name.chars().take(180).collect()
}

fn unique_path_in(dir: &std::path::Path, name: &str) -> PathBuf {
    let mut target = dir.join(name);
    if !target.exists() {
        return target;
    }
    let base = std::path::Path::new(name);
    let stem = base.file_stem().and_then(|s| s.to_str()).unwrap_or("eclipse-download");
    let suffix = base.extension().and_then(|s| s.to_str()).map(|s| format!(".{s}")).unwrap_or_default();
    for index in 2..1000 {
        target = dir.join(format!("{stem} ({index}){suffix}"));
        if !target.exists() {
            break;
        }
    }
    target
}

/// Minimal standard-alphabet base64 decoder (no new dependency for one call).
fn decode_base64(text: &str) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(text.len() / 4 * 3);
    let mut buffer: u32 = 0;
    let mut bits: u32 = 0;
    for byte in text.bytes() {
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            b'=' => break,
            b'\r' | b'\n' | b' ' | b'\t' => continue,
            _ => return None,
        };
        buffer = (buffer << 6) | u32::from(value);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buffer >> bits) as u8);
            buffer &= (1u32 << bits) - 1;
        }
    }
    Some(out)
}

fn write_download(dir: &std::path::Path, raw_name: &str, data: &[u8]) -> Result<PathBuf, String> {
    if data.is_empty() {
        return Err("the download was empty".into());
    }
    if data.len() > 512 * 1024 * 1024 {
        return Err("the download is larger than 512 MB".into());
    }
    std::fs::create_dir_all(dir).map_err(|err| format!("could not create {}: {err}", dir.display()))?;
    let target = unique_path_in(dir, &sanitise_file_name(raw_name));
    std::fs::write(&target, data).map_err(|err| format!("could not write {}: {err}", target.display()))?;
    Ok(target)
}

fn save_download(app: &AppHandle, raw_name: &str, data: &[u8]) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .download_dir()
        .map_err(|err| format!("no Downloads folder: {err}"))?;
    write_download(&dir, raw_name, data)
}

/// Window chrome actions. The app is served from a remote (loopback) origin,
/// where app-defined commands are denied, so the webview reaches this through
/// the `be-window` event; the bundled splash can invoke the command directly.
/// The active monitor's work area as `(x, y, width, height)` in physical pixels.
fn work_area_of(app: &AppHandle) -> Option<(i32, i32, u32, u32)> {
    let win = app.get_webview_window("main")?;
    let monitor = win
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| app.primary_monitor().ok().flatten())?;
    let work = monitor.work_area();
    Some((work.position.x, work.position.y, work.size.width, work.size.height))
}

/// Emulated maximize for a frameless window: fill the monitor's WORK AREA so the
/// Windows taskbar stays visible. Native `maximize()` on `decorations(false)`
/// covers the taskbar, which hid the bottom of the app.
fn fill_work_area(app: &AppHandle) {
    let Some(win) = app.get_webview_window("main") else {
        return;
    };
    let Some((x, y, width, height)) = work_area_of(app) else {
        return;
    };
    // A frameless window still has invisible resize borders: size the INNER
    // area so the visible content (not the invisible frame) ends at the work
    // area's bottom edge.
    let (border_w, border_h) = match (win.outer_size(), win.inner_size()) {
        (Ok(outer), Ok(inner)) => (
            outer.width.saturating_sub(inner.width),
            outer.height.saturating_sub(inner.height),
        ),
        _ => (0, 0),
    };
    let _ = win.set_position(tauri::PhysicalPosition::new(x, y));
    let _ = win.set_size(tauri::PhysicalSize::new(
        width.saturating_sub(border_w),
        height.saturating_sub(border_h),
    ));
    log_line(&format!(
        "[window] fill work area {width}x{height} at {x},{y} (border {border_w}x{border_h}) -> inner={:?} outer={:?}",
        win.inner_size().ok(),
        win.outer_size().ok()
    ));
}

/// Restore the last windowed bounds saved before the emulated maximize.
fn restore_windowed(app: &AppHandle) {
    let Some(win) = app.get_webview_window("main") else {
        return;
    };
    let state = app.state::<AppState>();
    let (width, height, x, y) = match state.prefs.lock() {
        Ok(prefs) => (prefs.width, prefs.height, prefs.x, prefs.y),
        Err(_) => return,
    };
    let _ = win.set_size(tauri::PhysicalSize::new(width, height));
    if let (Some(x), Some(y)) = (x, y) {
        let _ = win.set_position(tauri::PhysicalPosition::new(x, y));
    }
}

fn toggle_maximize(app: &AppHandle) {
    let state = app.state::<AppState>();
    if state.maximized.load(Ordering::SeqCst) {
        state.maximized.store(false, Ordering::SeqCst);
        if let Ok(mut prefs) = state.prefs.lock() {
            prefs.maximized = false;
            save_prefs(&state.prefs_file, &prefs);
        }
        restore_windowed(app);
    } else {
        // Remember the windowed geometry BEFORE filling the work area; the
        // Resized event that follows must not overwrite it (persist_bounds
        // skips while `maximized` is set).
        if let Some(win) = app.get_webview_window("main") {
            if let (Ok(size), Ok(pos)) = (win.inner_size(), win.outer_position()) {
                if let Ok(mut prefs) = state.prefs.lock() {
                    prefs.width = size.width;
                    prefs.height = size.height;
                    prefs.x = Some(pos.x);
                    prefs.y = Some(pos.y);
                    prefs.maximized = true;
                    save_prefs(&state.prefs_file, &prefs);
                }
            }
        }
        state.maximized.store(true, Ordering::SeqCst);
        fill_work_area(app);
    }
}

/// Apply or clear the OS-level glass behind the WebView. Acrylic is the real
/// blur (the CSS cannot blur the desktop); `apply_blur` is the older fallback.
#[cfg(windows)]
fn apply_window_glass(win: &WebviewWindow, enabled: bool) {
    use window_vibrancy::{apply_acrylic, apply_blur, clear_acrylic, clear_blur};
    if enabled {
        match apply_acrylic(win, Some((8, 9, 12, 175))) {
            Ok(()) => log_line("[window] acrylic enabled"),
            Err(err) => {
                log_line(&format!("[window] acrylic failed: {err}; trying blur"));
                match apply_blur(win, Some((10, 10, 12, 200))) {
                    Ok(()) => log_line("[window] blur fallback enabled"),
                    Err(err) => log_line(&format!("[window] blur failed: {err}")),
                }
            }
        }
    } else {
        let _ = clear_acrylic(win);
        let _ = clear_blur(win);
        log_line("[window] glass disabled");
    }
}

#[cfg(not(windows))]
fn apply_window_glass(_win: &WebviewWindow, _enabled: bool) {}

fn set_window_glass(app: &AppHandle, enabled: bool) {
    if let Some(win) = app.get_webview_window("main") {
        apply_window_glass(&win, enabled);
    }
    let state = app.state::<AppState>();
    if let Ok(mut prefs) = state.prefs.lock() {
        if prefs.window_glass != enabled {
            prefs.window_glass = enabled;
            save_prefs(&state.prefs_file, &prefs);
        }
    };
}

fn perform_window_action(app: &AppHandle, action: &str) {
    let win = match app.get_webview_window("main") {
        Some(win) => win,
        None => return,
    };
    log_line(&format!("[window] {action}"));
    match action {
        "minimize" => {
            let _ = win.minimize();
        }
        "toggle-maximize" => toggle_maximize(app),
        "close" => {
            let _ = win.close();
        }
        "start-drag" => {
            let _ = win.start_dragging();
        }
        _ => {}
    }
}

#[tauri::command]
fn desktop_window_action(app: AppHandle, action: String) {
    perform_window_action(&app, &action);
}

fn notify_native(app: &AppHandle, title: &str, body: &str) {
    use tauri_plugin_notification::NotificationExt;
    match app.notification().builder().title(title).body(body).show() {
        Ok(()) => log_line(&format!("[notify] sent: {title}")),
        Err(err) => log_line(&format!("[notify] failed: {err}")),
    }
}

#[tauri::command]
fn desktop_notify(app: AppHandle, title: String, body: String) {
    notify_native(&app, &title, &body);
}

// ── Taskbar presence ───────────────────────────────────────────────────────
//
// The page owns the state (it knows the unread count and the preference); Rust
// translates one `be-taskbar` event into the affordances a taskbar button has:
// an overlay badge on the icon (`SetOverlayIcon`) and a one-shot attention
// flash (`FlashWindowEx`) when the operator is elsewhere.
//
// The overlay is drawn, not decoded: one dot does not justify the PNG decoder
// dependency, and nothing from the page crosses the IPC bridge.
//
// Focus deliberately does NOT clear the badge. The page re-emits on focus, and
// a Rust-side clear would race that emit and win often enough to leave a stale
// badge on a page that still considers the item unread. `markTabRead` in the
// notification ledger is the single place unread goes down.

/// A filled dot with a darker rim, drawn here instead of shipped as a PNG.
/// Rasterising one 32px asset avoids pulling Tauri's `image-png` decoder into
/// the shell, and supersampling makes it crisp at any taskbar scale.
#[cfg(target_os = "windows")]
fn overlay_unread_image() -> tauri::image::Image<'static> {
    const SIZE: u32 = 32;
    const SAMPLES: u32 = 4;
    const DOT: [f32; 3] = [239.0, 68.0, 68.0]; // red-500
    const RIM: [f32; 3] = [127.0, 29.0, 29.0]; // red-800

    let mut rgba = vec![0u8; (SIZE * SIZE * 4) as usize];
    let center = (SIZE as f32 - 1.0) / 2.0;
    let outer = 11.0_f32;
    let inner = 9.0_f32;
    let samples = (SAMPLES * SAMPLES) as f32;

    for y in 0..SIZE {
        for x in 0..SIZE {
            let (mut covered, mut dot, mut rim) = (0.0_f32, 0.0_f32, 0.0_f32);
            for sy in 0..SAMPLES {
                for sx in 0..SAMPLES {
                    let px = x as f32 + (sx as f32 + 0.5) / SAMPLES as f32;
                    let py = y as f32 + (sy as f32 + 0.5) / SAMPLES as f32;
                    let dist = ((px - center).powi(2) + (py - center).powi(2)).sqrt();
                    if dist <= outer {
                        covered += 1.0;
                        if dist <= inner {
                            dot += 1.0;
                        } else {
                            rim += 1.0;
                        }
                    }
                }
            }
            if covered <= 0.0 {
                continue;
            }
            let idx = ((y * SIZE + x) * 4) as usize;
            rgba[idx] = ((dot * DOT[0] + rim * RIM[0]) / covered) as u8;
            rgba[idx + 1] = ((dot * DOT[1] + rim * RIM[1]) / covered) as u8;
            rgba[idx + 2] = ((dot * DOT[2] + rim * RIM[2]) / covered) as u8;
            rgba[idx + 3] = (covered / samples * 255.0).round() as u8;
        }
    }
    tauri::image::Image::new_owned(rgba, SIZE, SIZE)
}

/// `count == 0` removes the badge. On Windows it is a dot, so only zero/non-zero
/// matters; macOS and Linux show the number itself where the DE supports it.
fn set_taskbar_badge(app: &AppHandle, count: i64) {
    let count = count.max(0);
    app.state::<AppState>()
        .taskbar_unread
        .store(count, Ordering::SeqCst);

    let Some(win) = app.get_webview_window("main") else {
        return;
    };

    #[cfg(target_os = "windows")]
    {
        let icon = if count > 0 {
            Some(overlay_unread_image())
        } else {
            None
        };
        if let Err(err) = win.set_overlay_icon(icon) {
            log_line(&format!("[taskbar] overlay failed: {err}"));
        }
    }

    #[cfg(not(target_os = "windows"))]
    {
        if let Err(err) = win.set_badge_count(if count > 0 { Some(count) } else { None }) {
            log_line(&format!("[taskbar] badge failed: {err}"));
        }
    }
}

/// Indeterminate taskbar progress while a stream is doing work. `None` clears
/// it. Windows treats this as the green sweeping bar on the button, which is the
/// same cue ChatGPT and Teams use for "thinking"; Linux/macOS apply it app-wide
/// where the launcher supports it.
fn set_taskbar_progress(app: &AppHandle, working: bool) {
    app.state::<AppState>()
        .taskbar_working
        .store(working, Ordering::SeqCst);

    let Some(win) = app.get_webview_window("main") else {
        return;
    };
    let state = tauri::window::ProgressBarState {
        status: if working {
            Some(tauri::window::ProgressBarStatus::Indeterminate)
        } else {
            Some(tauri::window::ProgressBarStatus::None)
        },
        progress: None,
    };
    if let Err(err) = win.set_progress_bar(state) {
        log_line(&format!("[taskbar] progress failed: {err}"));
    }
}

/// Ask for attention once. `Critical` keeps flashing until the window is
/// focused; `Focused(true)` clears it. A hidden (keep-warm) window has no
/// taskbar button, so there is nothing to flash.
fn flash_taskbar(app: &AppHandle) {
    let Some(win) = app.get_webview_window("main") else {
        return;
    };
    if !win.is_visible().unwrap_or(true) || win.is_focused().unwrap_or(false) {
        return;
    }
    if let Err(err) = win.request_user_attention(Some(tauri::UserAttentionType::Critical)) {
        log_line(&format!("[taskbar] attention request failed: {err}"));
    }
}

/// Re-assert the badge/progress and stop any flash after the window regains
/// focus. Windows drops both when it recreates the taskbar button.
fn on_window_focused(app: &AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.request_user_attention(None);
    }
    let state = app.state::<AppState>();
    let count = state.taskbar_unread.load(Ordering::SeqCst);
    let working = state.taskbar_working.load(Ordering::SeqCst);
    if count > 0 {
        set_taskbar_badge(app, count);
    }
    if working {
        set_taskbar_progress(app, true);
    }
}

#[tauri::command]
fn desktop_toggle_fullscreen(app: AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let full = win.is_fullscreen().unwrap_or(false);
        let _ = win.set_fullscreen(!full);
    }
}

#[tauri::command]
fn desktop_devtools(app: AppHandle) {
    #[cfg(debug_assertions)]
    if let Some(win) = app.get_webview_window("main") {
        win.open_devtools();
    }
    #[cfg(not(debug_assertions))]
    let _ = &app;
}

#[tauri::command]
fn desktop_retry(app: AppHandle) {
    start_supervisor(app);
}

#[tauri::command]
fn desktop_info(state: tauri::State<AppState>) -> serde_json::Value {
    serde_json::json!({
        "root": state.root.to_string_lossy(),
        "port": app_port(),
        "keepWarm": state.prefs.lock().map(|p| p.keep_warm).unwrap_or(false),
    })
}

fn show_window(app: &AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

/// Saved bounds can exceed the monitor's WORK AREA (a previous resize, a
/// different taskbar size, a resolution change). Restoring them verbatim puts
/// the bottom of the frameless window under the Windows taskbar. Clamp both the
/// size and the position to the work area of the monitor the window belongs to.
fn clamped_bounds(app: &AppHandle, prefs: &Prefs) -> (f64, f64, Option<(f64, f64)>) {
    // Pick the monitor that physically contains the saved point. `monitor_from_point`
    // takes LOGICAL coordinates, while prefs are physical, so scan the monitor
    // rects directly and fall back to the primary monitor.
    let monitor = app
        .available_monitors()
        .ok()
        .and_then(|monitors| {
            monitors.into_iter().find(|monitor| {
                let origin = monitor.position();
                let size = monitor.size();
                match (prefs.x, prefs.y) {
                    (Some(x), Some(y)) => {
                        x >= origin.x
                            && x < origin.x + size.width as i32
                            && y >= origin.y
                            && y < origin.y + size.height as i32
                    }
                    _ => false,
                }
            })
        })
        .or_else(|| app.primary_monitor().ok().flatten());

    let Some(monitor) = monitor else {
        return (
            prefs.width as f64,
            prefs.height as f64,
            prefs.x.zip(prefs.y).map(|(x, y)| (x as f64, y as f64)),
        );
    };

    let work = monitor.work_area();
    // Builder sizes/positions are LOGICAL; prefs and the work area are physical.
    let scale = monitor.scale_factor().max(0.5);
    let min_w = 880.0 * scale;
    let min_h = 620.0 * scale;
    let max_w = (work.size.width as f64).max(min_w);
    let max_h = (work.size.height as f64).max(min_h);
    let width_px = (prefs.width as f64).clamp(min_w, max_w);
    let height_px = (prefs.height as f64).clamp(min_h, max_h);

    let position = prefs.x.zip(prefs.y).map(|(x, y)| {
        let max_x = (work.position.x + work.size.width as i32 - width_px as i32).max(work.position.x);
        let max_y = (work.position.y + work.size.height as i32 - height_px as i32).max(work.position.y);
        (
            x.clamp(work.position.x, max_x) as f64 / scale,
            y.clamp(work.position.y, max_y) as f64 / scale,
        )
    });

    (width_px / scale, height_px / scale, position)
}

fn build_window(app: &AppHandle, prefs: &Prefs) -> tauri::Result<WebviewWindow> {
    let (width, height, position) = clamped_bounds(app, prefs);
    let mut builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App(SPLASH.into()))
        .title("Black Eclipse")
        .inner_size(width, height)
        .min_inner_size(880.0, 620.0)
        // Frameless: the app's own tab strip is the title bar (drag region +
        // minimize/maximize/close). The native title text/logo is gone; the
        // title stays for the taskbar and accessibility.
        .decorations(false)
        // WebView2 replaces the page's HTML5 drag-and-drop handler with Tauri's
        // own file-drop handler. That silently kills every `draggable` element
        // in the app (tab reordering, workspace widgets, chart symbol drops).
        // The app uses HTML5 DnD, never Tauri's DragDropEvent, so opt out.
        .disable_drag_drop_handler()
        .initialization_script(INIT_SCRIPT);

    // A transparent window is what lets the acrylic/blur layer show through the
    // WebView. The app paints an opaque background when the glass setting is
    // off, so transparency itself is always safe.
    #[cfg(windows)]
    {
        builder = builder
            .transparent(true)
            .background_color(tauri::window::Color(0, 0, 0, 0));
    }
    #[cfg(not(windows))]
    {
        builder = builder.background_color(tauri::window::Color(10, 10, 12, 255));
    }

    if let Some((x, y)) = position {
        builder = builder.position(x, y);
    } else {
        builder = builder.center();
    }

    let win = builder.build()?;

    apply_window_glass(&win, prefs.window_glass);

    log_line(&format!(
        "[window] restore inner={}x{} logical at {:?}; actual={:?}",
        width.round(),
        height.round(),
        position,
        win.inner_size().ok()
    ));

    if prefs.maximized {
        // Re-assert after the first paint: Windows can apply the builder size
        // asynchronously, which would otherwise undo the work-area fill.
        let app_for_fill = app.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            fill_work_area(&app_for_fill);
        });
    }

    Ok(win)
}

fn persist_bounds(app: &AppHandle) {
    let state = app.state::<AppState>();
    // Emulated maximize must not overwrite the saved WINDOWED geometry.
    if state.maximized.load(Ordering::SeqCst) {
        return;
    }
    let prefs_file = state.prefs_file.clone();
    let win = match app.get_webview_window("main") {
        Some(w) => w,
        None => return,
    };

    if win.is_maximized().unwrap_or(false) {
        // Never record the maximized geometry as the restore geometry.
        if let Ok(mut prefs) = state.prefs.lock() {
            prefs.maximized = true;
            save_prefs(&prefs_file, &prefs);
        }
        return;
    }

    // Windows emits drag/resize events continuously; write at most ~2/second.
    let allowed = match state.last_bounds_save.lock() {
        Ok(mut last) => {
            if last.elapsed() < Duration::from_millis(400) {
                false
            } else {
                *last = Instant::now();
                true
            }
        }
        Err(_) => true,
    };
    if !allowed {
        return;
    }

    let size = win.inner_size().ok();
    let pos = win.outer_position().ok();
    write_bounds(&state, size, pos);
}

/// Split out of `persist_bounds` so the `MutexGuard` temporary is dropped at
/// the call boundary instead of at the end of the caller's scope.
fn write_bounds(state: &AppState, size: Option<tauri::PhysicalSize<u32>>, pos: Option<tauri::PhysicalPosition<i32>>) {
    if let Ok(mut prefs) = state.prefs.lock() {
        if let Some(size) = size {
            if size.width > 200 && size.height > 200 {
                prefs.width = size.width;
                prefs.height = size.height;
            }
        }
        if let Some(pos) = pos {
            prefs.x = Some(pos.x);
            prefs.y = Some(pos.y);
        }
        prefs.maximized = false;
        save_prefs(&state.prefs_file, &prefs);
    }
}

fn main() {
    // WebView2 draws its own media-consent dialog in the top-left corner of the
    // window: it cannot be themed, moved, or anchored to the mic that asked.
    // The app takes responsibility for that consent in the composer instead
    // (VoiceInputButton's "Allow" card), so the native dialog is suppressed for
    // this process. The window only ever loads our own loopback origin, and
    // this switch affects the consent UI only — the real microphone is used.
    #[cfg(windows)]
    {
        const FLAG: &str = "--use-fake-ui-for-media-stream";
        let existing = std::env::var("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS").unwrap_or_default();
        if !existing.contains(FLAG) {
            let merged = if existing.trim().is_empty() {
                FLAG.to_string()
            } else {
                format!("{} {}", existing.trim(), FLAG)
            };
            std::env::set_var("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", merged);
        }
    }

    let root = match resolve_root() {
        Some(r) => r,
        None => {
            // Nothing to attach to the UI yet; fail loudly in the log.
            eprintln!("[black-eclipse] could not locate the application root (scripts/desktop-server.js)");
            return;
        }
    };

    // Runtime state prefers the install directory (works for a per-user install
    // and for a checkout) but falls back beside the user's data when the install
    // is read-only — a system-wide .deb lives under /usr/lib, owned by root.
    let mut runtime_dir = root.join(".black-eclipse");
    if fs::create_dir_all(&runtime_dir).is_err() {
        let fallback = data_fallback_dir(&root).join(".runtime");
        match fs::create_dir_all(&fallback) {
            Ok(()) => {
                runtime_dir = fallback;
                eprintln!(
                    "[black-eclipse] install directory is not writable; runtime state goes to {}",
                    runtime_dir.to_string_lossy()
                );
            }
            Err(err) => eprintln!("[black-eclipse] no writable runtime directory: {err}"),
        }
    }
    let prefs_file = runtime_dir.join("desktop-prefs.json");
    let log_file = runtime_dir.join("desktop-native.log");
    std::env::set_var("BE_DESKTOP_LOG", &log_file);

    let mut prefs = load_prefs(&prefs_file);
    migrate_prefs(&prefs_file, &mut prefs);

    log_line(&format!(
        "[shell] starting — root={} port={}",
        root.to_string_lossy(),
        app_port()
    ));

    // Tauri's capability URL patterns cannot wildcard the port, so IPC (and with
    // it desktop notifications) is only granted on the canonical port. Say so
    // instead of letting a non-default port fail silently.
    if app_port() != APP_PORT && app_port() != DEV_PORT {
        log_line(&format!(
            "[shell] WARNING: port {} is not a channel port ({APP_PORT} stable / {DEV_PORT} dev); \
             desktop notifications will not work because the app origin is not in the IPC allowlist",
            app_port()
        ));
    }

    // §7: never open a window that cannot render. WebView2 ships with Windows 11
    // but not every Windows 10 machine.
    #[cfg(windows)]
    match webview2_version() {
        Some(version) => log_line(&format!("[shell] WebView2 runtime {version}")),
        None => {
            log_line("[shell] WebView2 runtime not found");
            message_box(
                "Black Eclipse needs the Microsoft Edge WebView2 Runtime, which is not installed.\n\n\
                 Windows 10 does not always include it. Install it once from:\n\
                 https://developer.microsoft.com/microsoft-edge/webview2/\n\n\
                 Then open Black Eclipse again. The installer can also do this for you: \
                 run BlackEclipse-Setup.exe again and it will install the runtime.",
            );
            return;
        }
    }

    // macOS (WKWebView) and Linux (WebKitGTK) use an OS-provided webview, so
    // there is nothing to detect there.
    #[cfg(not(windows))]
    log_line("[shell] using the system webview (no runtime probe needed)");

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_window(app);
        }))
        .plugin(tauri_plugin_notification::init())
        .manage(AppState {
            root: root.clone(),
            runtime_dir: runtime_dir.clone(),
            prefs_file: prefs_file.clone(),
            prefs: Mutex::new(prefs.clone()),
            child: Mutex::new(None),
            last_bounds_save: Mutex::new(Instant::now() - Duration::from_secs(2)),
            supervising: AtomicBool::new(false),
            owns_server: AtomicBool::new(false),
            quitting: AtomicBool::new(false),
            maximized: AtomicBool::new(prefs.maximized),
            taskbar_unread: AtomicI64::new(0),
            taskbar_working: AtomicBool::new(false),
        })
        .invoke_handler(tauri::generate_handler![
            desktop_notify,
            desktop_retry,
            desktop_window_action,
            desktop_toggle_fullscreen,
            desktop_devtools,
            desktop_diag_report,
            desktop_info,
            desktop_log_path,
            desktop_open_log
        ])
        .setup(move |app| {
            let handle = app.handle().clone();
            let win = build_window(&handle, &prefs)?;

            // Launched by the sign-in entry: show on the taskbar, minimized —
            // never hidden, because a hidden window has no taskbar button and
            // the operator needs to see that it is running.
            if std::env::args().any(|arg| arg == "--background") {
                let _ = win.minimize();
                log_line("[shell] started with --background (minimized)");
            }

            // --- window lifecycle -------------------------------------------------
            let win_for_events = win.clone();
            let app_for_events = handle.clone();
            win.on_window_event(move |event| match event {
                WindowEvent::CloseRequested { api, .. } => {
                    let state = app_for_events.state::<AppState>();
                    let keep_warm = state
                        .prefs
                        .lock()
                        .map(|p| p.keep_warm)
                        .unwrap_or(false);
                    let quitting = state.quitting.load(Ordering::SeqCst);
                    if keep_warm && !quitting {
                        // Keep the server warm so reopening is instant. Minimize,
                        // not hide: a hidden window has no taskbar button, which
                        // would remove the badge, the progress bar and the flash —
                        // exactly the affordances a background app needs. This is
                        // also what the reference apps do.
                        api.prevent_close();
                        log_line("[shell] window minimized (keep-warm)");
                        let _ = win_for_events.minimize();
                    } else {
                        log_line("[shell] window closed — quitting");
                        let _ = win_for_events.hide();
                        let state = app_for_events.state::<AppState>();
                        state.quitting.store(true, Ordering::SeqCst);
                        kill_tree(&state);
                        app_for_events.exit(0);
                    }
                }
                WindowEvent::Moved(_) | WindowEvent::Resized(_) => {
                    persist_bounds(&app_for_events);
                }
                WindowEvent::Focused(true) => on_window_focused(&app_for_events),
                _ => {}
            });

            // --- tray -------------------------------------------------------------
            let open_item = MenuItem::with_id(&handle, "open", "Open Black Eclipse", true, None::<&str>)?;
            let background_item = CheckMenuItem::with_id(
                &handle,
                "background",
                "Run in background (start with Windows, keep running when closed)",
                true,
                prefs.auto_start,
                None::<&str>,
            )?;
            let quit_item = MenuItem::with_id(&handle, "quit", "Quit Black Eclipse", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(&handle)?;
            let menu = Menu::with_items(&handle, &[&open_item, &background_item, &separator, &quit_item])?;

            // The check item is the source of truth: the platform toggles it on
            // click, and mirroring our own `!value` could drift from what the
            // menu shows.
            let background_switch = background_item.clone();

            let mut tray = TrayIconBuilder::with_id("main-tray")
                .tooltip("Black Eclipse")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "open" => show_window(app),
                    "background" => {
                        let state = app.state::<AppState>();
                        // On a read error fall back to "off": closing must quit.
                        let next = background_switch.is_checked().unwrap_or(false);
                        if let Err(err) = set_background(&state, next) {
                            // The registry write failed: put the switch back so the
                            // menu never claims a background that will not start.
                            log_line(&format!("[shell] background mode failed: {err}"));
                            let _ = background_switch.set_checked(!next);
                        } else {
                            log_line(&format!("[shell] background mode = {next}"));
                        }
                    }
                    "quit" => {
                        let state = app.state::<AppState>();
                        state.quitting.store(true, Ordering::SeqCst);
                        kill_tree(&state);
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_window(tray.app_handle());
                    }
                });

            if let Some(icon) = handle.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(&handle)?;

            // --- events from the app origin -------------------------------------
            // App commands are denied to remote origins, so the injected script
            // talks to us through core events (`core:default` includes emit).
            let notify_app = handle.clone();
            handle.listen("be-notify", move |event| {
                let payload = event.payload();
                match serde_json::from_str::<serde_json::Value>(payload) {
                    Ok(value) => {
                        let title = value
                            .get("title")
                            .and_then(|v| v.as_str())
                            .unwrap_or("Black Eclipse");
                        let body = value.get("body").and_then(|v| v.as_str()).unwrap_or("");
                        notify_native(&notify_app, title, body);
                    }
                    Err(err) => log_line(&format!("[notify] bad payload: {err}")),
                }
            });

            // --- reveal a reviewed file in the OS file manager --------------------
            // The Code review tree asks to show a changed file where it lives.
            // Rust validates the path and only reveals it; execution is never an
            // option here, so a forged payload cannot launch anything.
            handle.listen("be-reveal-path", move |event| {
                let value = match serde_json::from_str::<serde_json::Value>(event.payload()) {
                    Ok(value) => value,
                    Err(err) => {
                        log_line(&format!("[reveal] bad payload: {err}"));
                        return;
                    }
                };
                if let Some(path) = value.get("path").and_then(|v| v.as_str()) {
                    reveal_path(path);
                }
            });

            // --- open an external page in the real browser ------------------------
            // A `target="_blank"` download/release link is a new-window request the
            // single-window shell does not host, so it used to do nothing.
            handle.listen("be-open-external", move |event| {
                let value = match serde_json::from_str::<serde_json::Value>(event.payload()) {
                    Ok(value) => value,
                    Err(err) => {
                        log_line(&format!("[external] bad payload: {err}"));
                        return;
                    }
                };
                if let Some(url) = value.get("url").and_then(|v| v.as_str()) {
                    open_external(url);
                }
            });

            // --- save a produced file to Downloads -------------------------------
            // WebView2 in the shell has no download shelf, so a blob/anchor
            // download can silently fail. The page sends the bytes; Rust writes
            // a real file, reveals it and reports its absolute path.
            let save_app = handle.clone();
            handle.listen("be-save-download", move |event| {
                let value = match serde_json::from_str::<serde_json::Value>(event.payload()) {
                    Ok(value) => value,
                    Err(err) => {
                        log_line(&format!("[download] bad payload: {err}"));
                        return;
                    }
                };
                let name = value.get("name").and_then(|v| v.as_str()).unwrap_or("eclipse-download");
                let Some(encoded) = value.get("dataBase64").and_then(|v| v.as_str()) else {
                    log_line("[download] refused: no dataBase64");
                    return;
                };
                let Some(bytes) = decode_base64(encoded) else {
                    log_line("[download] refused: dataBase64 is not base64");
                    return;
                };
                match save_download(&save_app, name, &bytes) {
                    Ok(path) => {
                        log_line(&format!("[download] saved {}", path.to_string_lossy()));
                        let _ = save_app.emit(
                            "be-download-saved",
                            serde_json::json!({ "path": path.to_string_lossy(), "name": name }),
                        );
                        reveal_path(&path.to_string_lossy());
                    }
                    Err(err) => {
                        log_line(&format!("[download] failed: {err}"));
                        let _ = save_app.emit(
                            "be-download-saved",
                            serde_json::json!({ "error": err, "name": name }),
                        );
                    }
                }
            });

            // --- taskbar badge / attention ---------------------------------------
            // One channel, two OS affordances. The page decides; Rust renders.
            let taskbar_app = handle.clone();
            handle.listen("be-taskbar", move |event| {
                let value = match serde_json::from_str::<serde_json::Value>(event.payload()) {
                    Ok(value) => value,
                    Err(err) => {
                        log_line(&format!("[taskbar] bad payload: {err}"));
                        return;
                    }
                };
                let state = value.get("state").and_then(|v| v.as_str()).unwrap_or("idle");
                let count = value.get("count").and_then(|v| v.as_i64()).unwrap_or(0);
                let flash = value
                    .get("flash")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                // Unknown states are treated as idle: a future state from a newer
                // page must never leave a stale badge behind.
                let unread = state == "unread" && count > 0;
                let working = value.get("working").and_then(|v| v.as_bool()).unwrap_or(false);
                set_taskbar_badge(&taskbar_app, if unread { count } else { 0 });
                set_taskbar_progress(&taskbar_app, working);
                if unread && flash {
                    flash_taskbar(&taskbar_app);
                }
                log_line(&format!(
                    "[taskbar] state={state} count={} flash={flash} working={working}",
                    if unread { count } else { 0 }
                ));
            });

            let window_app = handle.clone();
            handle.listen("be-window", move |event| {
                if let Ok(value) = serde_json::from_str::<serde_json::Value>(event.payload()) {
                    if let Some(action) = value.get("action").and_then(|v| v.as_str()) {
                        if action == "set-glass" {
                            let enabled = value
                                .get("enabled")
                                .and_then(|v| v.as_bool())
                                .unwrap_or(true);
                            set_window_glass(&window_app, enabled);
                        } else {
                            perform_window_action(&window_app, action);
                        }
                    }
                }
            });

            let shortcut_app = handle.clone();
            handle.listen("be-shortcut", move |event| {                let payload = event.payload();
                if let Ok(value) = serde_json::from_str::<serde_json::Value>(payload) {
                    match value.get("action").and_then(|v| v.as_str()) {
                        Some("fullscreen") => desktop_toggle_fullscreen(shortcut_app.clone()),
                        Some("devtools") => desktop_devtools(shortcut_app.clone()),
                        _ => {}
                    }
                }
            });

            // --- staged update: restart onto the new build ------------------------
            // The page stages an update (git tag + build) and then asks for this.
            // Promotion kills the window (`stopShell`) and `taskkill /T`s the
            // server tree (`stopOwnedServer`), so it MUST be spawned here: this
            // shell is not in the server's tree, and `stopShell` kills only the
            // window pid, not its children. A promote spawned by the server
            // would be killed halfway through, after the copy but possibly
            // before the pointer moved.
            let update_app = handle.clone();
            handle.listen("be-update", move |event| {
                let value = match serde_json::from_str::<serde_json::Value>(event.payload()) {
                    Ok(value) => value,
                    Err(err) => {
                        log_line(&format!("[update] bad payload: {err}"));
                        return;
                    }
                };
                let action = value
                    .get("action")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                if action != "apply" && action != "restart" {
                    return;
                }
                let channel = value
                    .get("channel")
                    .and_then(|v| v.as_str())
                    .unwrap_or("stable")
                    .to_string();
                // The version this window is serving. The shell cannot read the
                // pinned build, so the page sends it; without it the updater
                // compared against the checkout on disk and could decide
                // "nothing to install" while the running UI was older.
                let running_version = value
                    .get("currentVersion")
                    .and_then(|v| v.as_str())
                    .map(|v| v.to_string());
                let app = update_app.clone();
                std::thread::spawn(move || {
                    let root = app.state::<AppState>().root.clone();
                    let node = match find_node() {
                        Some(node) => node,
                        None => {
                            log_line("[update] node not found; cannot run the updater");
                            return;
                        }
                    };
                    // Both stages run from the shell so nothing the app's own
                    // supervisor kills (`taskkill /T` on the server tree) can
                    // take them down mid-build.
                    let script = if action == "apply" {
                        log_line(&format!("[update] staging the {channel} release"));
                        "update-apply.js"
                    } else {
                        log_line(&format!(
                            "[update] promoting the staged {channel} build and restarting"
                        ));
                        "desktop-refresh.js"
                    };
                    let mut command = Command::new(node);
                    command
                        .arg(root.join("scripts").join(script))
                        .arg("--channel")
                        .arg(&channel)
                        .current_dir(&root)
                        .stdin(Stdio::null())
                        .stdout(Stdio::null())
                        .stderr(Stdio::null());
                    if action == "restart" {
                        command.arg("--no-build");
                    }
                    if action == "apply" {
                        if let Some(version) = running_version.as_deref() {
                            command.arg("--running-version").arg(version);
                        }
                    }
                    if let Err(err) = command.spawn() {
                        log_line(&format!("[update] could not start {script}: {err}"));
                    }
                });
            });

            // --- boot -------------------------------------------------------------
            start_supervisor(handle.clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the Black Eclipse shell");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            let state = handle.state::<AppState>();
            if let Ok(prefs) = state.prefs.lock() {
                save_prefs(&state.prefs_file, &prefs);
            }
            kill_tree(&state);
        }
    });
}

/// The download bridge is pure stdlib work (base64 + a safe file name), so it
/// can be checked without a window: a regression here is the "download button
/// does nothing" bug coming back.
#[cfg(test)]
mod download_tests {
    use super::{decode_base64, sanitise_file_name, unique_path_in};

    #[test]
    fn base64_round_trips_the_shapes_the_page_sends() {
        assert_eq!(decode_base64("aGVsbG8=").unwrap(), b"hello");
        assert_eq!(decode_base64("JVBERi0xLjQ=").unwrap(), b"%PDF-1.4");
        assert_eq!(decode_base64("aGVs\nbG8=").unwrap(), b"hello");
        assert!(decode_base64("not base64!!").is_none());
    }

    #[test]
    fn a_download_lands_on_disk_with_its_real_bytes() {
        // The write path the window uses, into a throwaway "Downloads" folder.
        let dir = std::env::temp_dir().join(format!("be-dl-write-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let first = super::write_download(&dir, "Q3 report.pdf", b"%PDF-1.4 hello").unwrap();
        assert!(first.is_absolute());
        assert_eq!(first.file_name().unwrap(), "Q3 report.pdf");
        assert_eq!(std::fs::read(&first).unwrap(), b"%PDF-1.4 hello");
        let second = super::write_download(&dir, "Q3 report.pdf", b"second").unwrap();
        assert_eq!(second.file_name().unwrap(), "Q3 report (2).pdf");
        assert!(super::write_download(&dir, "empty.bin", b"").is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_download_name_can_never_escape_downloads() {
        let sanitised = sanitise_file_name("../../evil.pdf");
        assert!(!sanitised.contains(".."));
        assert!(!sanitised.contains('/') && !sanitised.contains('\\'));
        assert!(sanitised.ends_with("evil.pdf"));
        assert_eq!(sanitise_file_name("Q3: review?.pdf"), "Q3- review-.pdf");
        assert_eq!(sanitise_file_name("   "), "eclipse-download");
    }

    #[test]
    fn a_second_download_never_overwrites_the_first() {
        let dir = std::env::temp_dir().join(format!("be-download-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let first = unique_path_in(&dir, "report.pdf");
        assert_eq!(first.file_name().unwrap(), "report.pdf");
        std::fs::write(&first, b"x").unwrap();
        let second = unique_path_in(&dir, "report.pdf");
        assert_eq!(second.file_name().unwrap(), "report (2).pdf");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

/// The badge is pure geometry, so it can be checked without a window or a
/// taskbar. Shape regressions here would otherwise only show up by eye.
#[cfg(all(test, target_os = "windows"))]
mod taskbar_icon_tests {
    use super::overlay_unread_image;

    fn pixel(img: &tauri::image::Image<'_>, x: u32, y: u32) -> (u8, u8, u8, u8) {
        let i = ((y * 32 + x) * 4) as usize;
        let rgba = img.rgba();
        (rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3])
    }

    #[test]
    fn badge_is_a_centered_disc_with_a_darker_rim_on_transparency() {
        let img = overlay_unread_image();
        assert_eq!((img.width(), img.height()), (32, 32));

        let centre = pixel(&img, 16, 16);
        assert_eq!(centre.3, 255, "centre must be fully opaque");
        assert!(
            centre.0 > 200 && centre.1 < 100 && centre.2 < 100,
            "centre must be the app red, got {centre:?}"
        );

        assert_eq!(pixel(&img, 0, 0).3, 0, "corner must stay transparent");
        assert_eq!(pixel(&img, 31, 31).3, 0, "corner must stay transparent");
        assert_eq!(pixel(&img, 0, 16).3, 0, "left edge must stay clear of the disc");

        // 9.5px above centre: inside the outer radius, outside the inner one.
        let rim = pixel(&img, 16, 6);
        assert!(rim.3 > 0, "rim must be drawn, not a hole");
        assert!(rim.0 < centre.0, "rim must be darker than the dot");
    }
}

/// The sign-in entry is a string contract with Windows: the value name, the
/// quoted path and `--background` must match what the shell parses at startup.
#[cfg(all(test, windows))]
mod login_item_tests {
    use super::login_item_command;

    #[test]
    fn sign_in_entry_launches_this_exe_minimized() {
        let exe = std::path::Path::new(r"C:\Program Files\Black Eclipse\black-eclipse.exe");
        let (program, add) = login_item_command(true, exe);
        assert_eq!(program, "reg");
        assert_eq!(add[0], "add");
        assert!(add.iter().any(|arg| arg == "REG_SZ"));
        let data = &add[add.iter().position(|arg| arg == "/d").expect("/d") + 1];
        assert_eq!(
            data,
            "\"C:\\Program Files\\Black Eclipse\\black-eclipse.exe\" --background"
        );
    }

    #[test]
    fn disabling_removes_the_same_value() {
        let exe = std::path::Path::new(r"C:\Black Eclipse\black-eclipse.exe");
        let (_, delete) = login_item_command(false, exe);
        assert_eq!(delete[0], "delete");
        assert!(delete.iter().any(|arg| arg == "Black Eclipse"));
    }
}
