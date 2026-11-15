#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    io::{BufRead, BufReader, Write},
    mem::size_of,
    os::windows::{
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
        process::CommandExt,
    },
    path::Path,
    process::{Child, ChildStdin, Command, Stdio},
    ptr::null,
    sync::{mpsc, Mutex},
    thread,
    time::{Duration, Instant},
};

use reqwest::{Client, Method, Url};
use serde::{Deserialize, Serialize};
use tauri::{Manager, RunEvent, State, WebviewWindow};
use windows::{
    core::HSTRING,
    Data::Xml::Dom::XmlDocument,
    UI::Notifications::{NotificationSetting, ToastNotification, ToastNotificationManager},
};
use windows_sys::Win32::System::{
    JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    },
    Threading::CREATE_NO_WINDOW,
};

const TOKEN_HEADER: &str = "x-soulseed-desktop-token";
const START_ERROR: &str = "Soulseed 无法启动本地服务。请关闭应用后重试。若问题持续，请确认当前 Windows 账户可写入应用数据目录，或重新安装。";
const REQUEST_ERROR: &str = "本地服务连接中断。请先查看记录，确认操作是否已完成。";

struct Backend {
    client: Client,
    origin: Url,
    token: String,
    process: Mutex<Option<Process>>,
}

struct Process {
    child: Child,
    input: Option<ChildStdin>,
    _job: OwnedHandle,
}

impl Drop for Process {
    fn drop(&mut self) {
        drop(self.input.take());
        let deadline = Instant::now() + Duration::from_secs(4);
        while Instant::now() < deadline {
            match self.child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) => thread::sleep(Duration::from_millis(50)),
                Err(_) => break,
            }
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn process_job() -> std::io::Result<OwnedHandle> {
    let handle = unsafe { CreateJobObjectW(null(), null()) };
    if handle.is_null() {
        return Err(std::io::Error::last_os_error());
    }
    let job = unsafe { OwnedHandle::from_raw_handle(handle) };
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    let configured = unsafe {
        SetInformationJobObject(
            job.as_raw_handle(),
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    };
    if configured == 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(job)
}

#[derive(Deserialize)]
struct BootstrapReply {
    port: u16,
}

#[derive(Deserialize)]
struct Health {
    status: String,
    environment: String,
    build_id: String,
}

impl Backend {
    fn start(executable: &Path, data_dir: &Path) -> Result<Self, Box<dyn std::error::Error>> {
        std::fs::create_dir_all(data_dir)?;
        let mut random = [0u8; 32];
        getrandom::fill(&mut random).map_err(|_| "desktop token generation failed")?;
        let token: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let job = process_job()?;
        let mut command = Command::new(executable);
        command
            .current_dir(data_dir)
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW);
        for name in ["SystemRoot", "WINDIR", "TEMP", "TMP"] {
            if let Some(value) = std::env::var_os(name) {
                command.env(name, value);
            }
        }
        let mut child = command.spawn()?;
        // The one-directory bootloader waits for the private pipe before
        // initializing the database or binding. Assign it before sending input.
        if unsafe { AssignProcessToJobObject(job.as_raw_handle(), child.as_raw_handle()) } == 0 {
            let error = std::io::Error::last_os_error();
            let _ = child.kill();
            let _ = child.wait();
            return Err(error.into());
        }
        let input = child.stdin.take();
        let output = child.stdout.take();
        let mut process = Process {
            child,
            input,
            _job: job,
        };
        let output = output.ok_or("missing backend output pipe")?;
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        thread::spawn(move || {
            let mut line = String::new();
            let reply = BufReader::new(output)
                .read_line(&mut line)
                .ok()
                .and_then(|_| serde_json::from_str::<BootstrapReply>(&line).ok());
            let _ = ready_tx.send(reply);
        });
        let input = process.input.as_mut().ok_or("missing backend input pipe")?;
        serde_json::to_writer(
            &mut *input,
            &serde_json::json!({"data_dir": data_dir, "token": token}),
        )?;
        input.write_all(b"\n")?;
        let port = ready_rx
            .recv_timeout(Duration::from_secs(15))?
            .ok_or("backend bootstrap failed")?
            .port;
        if port == 0 {
            return Err("invalid backend port".into());
        }
        let origin = Url::parse(&format!("http://127.0.0.1:{port}/"))?;
        let client = Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_millis(500))
            .timeout(Duration::from_secs(75))
            .build()?;
        let health_url = origin.join("api/health")?;
        tauri::async_runtime::block_on(async {
            let deadline = Instant::now() + Duration::from_secs(15);
            while Instant::now() < deadline {
                if process.child.try_wait()?.is_some() {
                    break;
                }
                let response = client
                    .get(health_url.clone())
                    .header(TOKEN_HEADER, &token)
                    .timeout(Duration::from_millis(600))
                    .send()
                    .await;
                if let Ok(response) = response.and_then(reqwest::Response::error_for_status) {
                    if let Ok(health) = response.json::<Health>().await {
                        if health.status == "ok"
                            && health.environment == "production"
                            && health.build_id.starts_with("desktop-")
                        {
                            return Ok::<_, Box<dyn std::error::Error>>(());
                        }
                    }
                }
                thread::sleep(Duration::from_millis(100));
            }
            Err("backend health check failed".into())
        })?;
        Ok(Self {
            client,
            origin,
            token,
            process: Mutex::new(Some(process)),
        })
    }
}

struct DesktopState(Result<Backend, &'static str>);
struct NotificationWatcher(Mutex<Option<tauri::async_runtime::JoinHandle<()>>>);

#[derive(Serialize)]
struct ApiResponse {
    status: u16,
    body: String,
    request_id: Option<String>,
    server_timing: Option<String>,
}

#[tauri::command]
async fn desktop_request(
    window: WebviewWindow,
    state: State<'_, DesktopState>,
    path: String,
    method: String,
    body: Option<String>,
) -> Result<ApiResponse, String> {
    if window.label() != "main" {
        return Err(REQUEST_ERROR.into());
    }
    let backend = state.0.as_ref().map_err(|error| error.to_string())?;
    let method = match method.as_str() {
        "GET" => Method::GET,
        "POST" => Method::POST,
        "PUT" => Method::PUT,
        "DELETE" => Method::DELETE,
        _ => return Err(REQUEST_ERROR.into()),
    };
    if !path.starts_with('/')
        || path.len() > 4096
        || body.as_ref().is_some_and(|value| value.len() > 256 * 1024)
    {
        return Err(REQUEST_ERROR.into());
    }
    let url = backend
        .origin
        .join(&format!("api{path}"))
        .map_err(|_| REQUEST_ERROR)?;
    if url.origin() != backend.origin.origin() || !url.path().starts_with("/api/") {
        return Err(REQUEST_ERROR.into());
    }
    if url.path() == "/api/desktop/native/contact-notification" {
        return Err(REQUEST_ERROR.into());
    }
    let mut request = backend
        .client
        .request(method, url)
        .header(TOKEN_HEADER, &backend.token);
    if let Some(body) = body {
        request = request
            .header("Content-Type", "application/json")
            .body(body);
    }
    let response = request.send().await.map_err(|_| REQUEST_ERROR)?;
    let header = |name| {
        response
            .headers()
            .get(name)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
    };
    Ok(ApiResponse {
        status: response.status().as_u16(),
        request_id: header("x-request-id"),
        server_timing: header("server-timing"),
        body: response.text().await.map_err(|_| REQUEST_ERROR)?,
    })
}

#[derive(Deserialize)]
struct ContactNotification {
    message_id: Option<String>,
}

fn in_foreground(window: &WebviewWindow) -> bool {
    window.is_focused().unwrap_or(true) && !window.is_minimized().unwrap_or(false)
}

fn show_contact_notification(app_id: &str, message_id: &str) -> windows::core::Result<bool> {
    let notifier = ToastNotificationManager::CreateToastNotifierWithId(&HSTRING::from(app_id))?;
    if let Ok(setting) = notifier.Setting() {
        if setting != NotificationSetting::Enabled {
            return Ok(false);
        }
    }
    let xml = XmlDocument::new()?;
    // Keep the lock screen and notification history free of chat and model data.
    xml.LoadXml(&HSTRING::from(
        "<toast><visual><binding template=\"ToastGeneric\"><text>Soulseed</text><text>大肥鱼发来了一条新消息</text></binding></visual></toast>",
    ))?;
    let toast = ToastNotification::CreateToastNotification(&xml)?;
    let tag: String = message_id
        .chars()
        .filter(|ch| ch.is_ascii_hexdigit())
        .take(16)
        .collect();
    toast.SetTag(&HSTRING::from(tag))?;
    notifier.Show(&toast)?;
    Ok(true)
}

async fn watch_contact_notifications(app: tauri::AppHandle) {
    loop {
        tokio::time::sleep(Duration::from_secs(3)).await;
        let (send, receive) = tokio::sync::oneshot::channel();
        let handle = app.clone();
        if app
            .run_on_main_thread(move || {
                let _ = send.send(
                    handle
                        .get_webview_window("main")
                        .map(|window| in_foreground(&window)),
                );
            })
            .is_err()
        {
            return;
        }
        let Ok(Ok(Some(foreground))) = tokio::time::timeout(Duration::from_secs(2), receive).await
        else {
            return;
        };
        let state = app.state::<DesktopState>();
        let Ok(backend) = &state.0 else { return };
        let Ok(url) = backend
            .origin
            .join("api/desktop/native/contact-notification")
        else {
            return;
        };
        let response = backend
            .client
            .post(url)
            .query(&[("foreground", foreground)])
            .header(TOKEN_HEADER, &backend.token)
            .timeout(Duration::from_secs(2))
            .send()
            .await;
        let Ok(response) = response.and_then(reqwest::Response::error_for_status) else {
            continue;
        };
        let Ok(ContactNotification {
            message_id: Some(id),
        }) = response.json().await
        else {
            continue;
        };
        let app_id = app.config().identifier.clone();
        let handle = app.clone();
        let _ = app.run_on_main_thread(move || {
            let result = if handle
                .get_webview_window("main")
                .is_some_and(|window| !in_foreground(&window))
            {
                show_contact_notification(&app_id, &id)
            } else {
                Ok(false)
            };
            #[cfg(feature = "qa-contact")]
            {
                let _ = std::fs::write(
                    std::path::Path::new(env!("SOULSEED_QA_DATA")).join("toast-result.txt"),
                    format!("{result:?}"),
                );
            }
            #[cfg(not(feature = "qa-contact"))]
            let _ = result;
        });
    }
}

fn main() {
    let context = tauri::generate_context!();
    let app_id: Vec<u16> = context
        .config()
        .identifier
        .encode_utf16()
        .chain(Some(0))
        .collect();
    // Match the AppUserModelID on the NSIS Start menu shortcut.
    unsafe {
        windows_sys::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID(app_id.as_ptr());
    }
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .setup(|app| {
            let result = app
                .path()
                .resource_dir()
                .and_then(|resources| {
                    Ok((resources.join("sidecar").join("soulseed-backend.exe"), {
                        #[cfg(feature = "qa-contact")]
                        let data = std::path::PathBuf::from(env!("SOULSEED_QA_DATA"));
                        #[cfg(not(feature = "qa-contact"))]
                        let data = app.path().app_local_data_dir()?.join("data");
                        data
                    }))
                })
                .map_err(|_| START_ERROR)
                .and_then(|(executable, data)| {
                    Backend::start(&executable, &data).map_err(|_| START_ERROR)
                });
            app.manage(DesktopState(result));
            let watcher =
                tauri::async_runtime::spawn(watch_contact_notifications(app.handle().clone()));
            app.manage(NotificationWatcher(Mutex::new(Some(watcher))));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![desktop_request])
        .build(context)
        .expect("could not initialize Soulseed window")
        .run(|app, event| {
            if matches!(event, RunEvent::Exit) {
                if let Ok(mut watcher) = app.state::<NotificationWatcher>().0.lock() {
                    if let Some(task) = watcher.take() {
                        task.abort();
                    }
                }
                if let Ok(backend) = &app.state::<DesktopState>().0 {
                    if let Ok(mut process) = backend.process.lock() {
                        drop(process.take());
                    }
                }
            }
        });
}
