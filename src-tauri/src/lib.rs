use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

/// 与 app/config.py 的 _default_data_dir() 保持一致：Windows 下用 LOCALAPPDATA。
const APP_DIR_NAME: &str = "auto_clip_video";
/// 只写裸名：安装包把 sidecar 平铺在主程序旁边，`shell().sidecar()` 也是按
/// 「exe 同级目录 + 该名字」解析的。写成 `binaries/...` 会去找不存在的子目录，
/// 表现为启动即退出（外壳是 windows_subsystem 程序，没有任何错误可见）。
const SIDECAR_NAME: &str = "auto-clip-video-server";
/// 前端 `bootstrapBackend()` 等就绪的时间上限。
///
/// 实测本机「双击外壳 → 端口落盘」热启动要约 10 秒（PyInstaller onefile 先解包再拉起
/// Python），首次安装后还要多给杀毒软件扫描 59 MB 单文件的时间，20 秒太紧会让正常启动
/// 被报成「本地服务未就绪」。
const READY_TIMEOUT: Duration = Duration::from_secs(45);

#[derive(Serialize)]
struct BackendInfo {
    port: u16,
    token: String,
}

struct Sidecar {
    child: Mutex<Option<CommandChild>>,
    /// 持有 Job Object 句柄；取出并 drop 即回收整棵进程树
    job: Mutex<Option<Reaper>>,
}

#[cfg(windows)]
type Reaper = sidecar_job::SidecarJob;
#[cfg(not(windows))]
type Reaper = ();

#[cfg(windows)]
mod sidecar_job {
    use std::ffi::c_void;
    use std::mem::{size_of_val, zeroed};
    use std::ptr::null;

    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, HANDLE, INVALID_HANDLE_VALUE,
    };
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE};

    /// 一个带 KILL_ON_JOB_CLOSE 的 Job Object。
    ///
    /// PyInstaller 的 onefile 产物是两个同名进程：引导进程 + 真正的 Python 进程。
    /// `CommandChild::kill()` 只打得到引导进程，Python 会留在后台继续监听端口、
    /// 占着数据库，连它派生的 ffmpeg 也不会停。作业对象由内核在句柄关闭时回收整棵树，
    /// 外壳被强杀或崩溃同样生效。
    pub struct SidecarJob(HANDLE);

    // 只经由 Mutex 取出，取出的那个线程负责 drop
    unsafe impl Send for SidecarJob {}

    impl Drop for SidecarJob {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe { CloseHandle(self.0) };
            }
        }
    }

    fn win_error(context: &str) -> String {
        format!("{context}（GetLastError={}）", unsafe { GetLastError() })
    }

    pub fn assign(pid: u32) -> Result<SidecarJob, String> {
        unsafe {
            let job = CreateJobObjectW(null(), null());
            if job.is_null() || job == INVALID_HANDLE_VALUE {
                return Err(win_error("创建 Job Object 失败"));
            }
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const c_void,
                size_of_val(&limits) as u32,
            ) == 0
            {
                CloseHandle(job);
                return Err(win_error("设置 Job Object 信息失败"));
            }
            let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
            if process.is_null() || process == INVALID_HANDLE_VALUE {
                CloseHandle(job);
                return Err(win_error("打开 sidecar 进程失败"));
            }
            let assigned = AssignProcessToJobObject(job, process);
            CloseHandle(process);
            if assigned == 0 {
                CloseHandle(job);
                // 常见原因是进程已被别的作业占用（例如从 cargo/终端的作业里启动）：
                // 这种情况退回到「只杀引导进程」，与加这段代码之前一致
                return Err(win_error("把 sidecar 加入作业失败"));
            }
            Ok(SidecarJob(job))
        }
    }
}

#[cfg(not(windows))]
mod sidecar_job {
    pub fn assign(_pid: u32) -> Result<(), String> {
        Ok(())
    }
}

fn data_dir() -> Result<PathBuf, String> {
    let base = std::env::var("LOCALAPPDATA")
        .map_err(|_| "环境变量 LOCALAPPDATA 不可用，无法定位应用数据目录".to_string())?;
    Ok(PathBuf::from(base).join(APP_DIR_NAME))
}

fn read_trimmed(path: &PathBuf) -> Option<String> {
    std::fs::read_to_string(path)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

/// 前端 bootstrapBackend() 调用的命令：端口与令牌由后端落盘，这里等它就绪。
/// 令牌不写日志，也不进入任何 URL（媒体流另有短期票据）。
#[tauri::command]
async fn backend_info() -> Result<BackendInfo, String> {
    let dir = data_dir()?;
    let token_path = dir.join("api.token");
    let port_path = dir.join("runtime.port");
    let deadline = Instant::now() + READY_TIMEOUT;

    loop {
        if let (Some(token), Some(port)) = (read_trimmed(&token_path), read_trimmed(&port_path)) {
            let port = port
                .parse::<u16>()
                .map_err(|_| format!("端口文件内容异常: {port}"))?;
            return Ok(BackendInfo { port, token });
        }
        if Instant::now() >= deadline {
            return Err(format!(
                "本地服务在 {:?} 内未就绪，请检查日志 {}",
                READY_TIMEOUT,
                dir.join("logs").join("app.log").display()
            ));
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
}

/// 启动本地服务，失败时把原因落到数据目录。
///
/// 外壳是 `windows_subsystem="windows"` 程序，没有控制台：setup 返回 Err 只会表现成
/// 「双击后闪退、什么提示都没有」。所以启动失败必须先写盘再冒泡。
fn start_sidecar(app: &tauri::App) -> Result<(CommandChild, Option<Reaper>), String> {
    let dir = data_dir()?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("创建数据目录失败: {e}"))?;
    // 端口文件描述的是「当前这个」本地服务。上次如果是被强杀的，lifespan 的收尾没跑，
    // 旧端口会留在盘上；前端一上来就照着旧端口连，而端口未必还是同一个，于是永远连不上。
    let _ = std::fs::remove_file(dir.join("runtime.port"));

    let outcome = (|| {
        // 不带任何命令行参数启动 sidecar：JS 侧没有 shell 权限，参数面也就无从被注入
        let sidecar = app
            .shell()
            .sidecar(SIDECAR_NAME)
            .map_err(|e| format!("找不到 sidecar {SIDECAR_NAME}: {e}"))?;
        sidecar
            .env("ACV_DATA_DIR", &dir)
            .spawn()
            .map(|(_events, child)| child)
            .map_err(|e| format!("启动本地服务失败: {e}"))
    })();

    if let Err(e) = &outcome {
        write_startup_error(&dir, e);
    }
    // 进不了作业（例如外壳本身被别的作业占用）不是致命问题：退化成只杀引导进程
    let child = outcome?;
    let job = sidecar_job::assign(child.pid()).ok();
    Ok((child, job))
}

fn write_startup_error(dir: &Path, message: &str) {
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // 写不进去就只能忽略：这已经是最后的诊断手段
    let _ = std::fs::write(dir.join("shell-error.log"), format!("[{stamp}] {message}\n"));
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let (child, job) = start_sidecar(app)?;
            app.manage(Sidecar {
                child: Mutex::new(Some(child)),
                job: Mutex::new(job),
            });

            #[cfg(debug_assertions)]
            if let Some(window) = app.get_webview_window("main") {
                window.open_devtools();
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_info])
        .build(tauri::generate_context!())
        .expect("构建 Tauri 应用失败");

    app.run(|app_handle, event| {
        // 窗口关了但 Python 还在监听端口，下次启动会撞车，所以退出前必须回收子进程
        if let RunEvent::Exit = event {
            if let Some(state) = app_handle.try_state::<Sidecar>() {
                if let Some(child) = state.child.lock().ok().and_then(|mut g| g.take()) {
                    let _ = child.kill();
                }
                // 关掉作业句柄才会连带收掉 Python 子进程与它派生的 ffmpeg
                if let Ok(mut guard) = state.job.lock() {
                    drop(guard.take());
                }
            }
        }
    });
}
