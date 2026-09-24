#![windows_subsystem = "windows"]
//! VenTapes Windows Launcher
//! Sets up the MSYS2 environment and launches the Python app.

use std::env;
use std::path::PathBuf;
use std::process::Command;

fn main() {
    let exe_path = env::current_exe().unwrap_or_default();
    let fallback = PathBuf::from(".");
    let base_dir = exe_path.parent().unwrap_or(&fallback);
    let base = base_dir.to_string_lossy();

    // Build PATH
    let sys_path = env::var("PATH").unwrap_or_default();
    let new_path = format!("{}\\runtime\\bin;{}", base, sys_path);

    // Try pythonw.exe first (no console), fallback to python3.exe
    let pythonw = base_dir.join("runtime").join("bin").join("pythonw.exe");
    let python3 = base_dir.join("runtime").join("bin").join("python3.exe");
    let python_exe = if pythonw.exists() { pythonw } else { python3 };
    let main_py = base_dir.join("src").join("main.py");

    let status = Command::new(&python_exe)
        .arg(&main_py)
        .env("PATH", &new_path)
        .env("PYTHONHOME", format!("{}\\runtime", base))
        .env("PYTHONPATH", format!("{}\\src", base))
        .env(
            "GI_TYPELIB_PATH",
            format!("{}\\runtime\\lib\\girepository-1.0", base),
        )
        .env(
            "GST_PLUGIN_PATH",
            format!("{}\\runtime\\lib\\gstreamer-1.0", base),
        )
        .env(
            "GIO_MODULE_DIR",
            format!("{}\\runtime\\lib\\gio\\modules", base),
        )
        .env(
            "GSETTINGS_SCHEMA_DIR",
            format!("{}\\runtime\\share\\glib-2.0\\schemas", base),
        )
        .env(
            "SSL_CERT_FILE",
            format!("{}\\runtime\\ssl\\certs\\ca-bundle.crt", base),
        )
        .env("XDG_DATA_DIRS", format!("{}\\runtime\\share", base))
        .spawn();

    match status {
        Ok(mut child) => {
            // Don't wait — let the app run independently
            let _ = child.wait();
        }
        Err(e) => {
            // Show error dialog via Windows API
            use std::ffi::OsStr;
            use std::os::windows::ffi::OsStrExt;
            let msg = format!(
                "Failed to start VenTapes.\n\nError: {}\n\nEnsure the runtime directory is intact.",
                e
            );
            let wide_msg: Vec<u16> = OsStr::new(&msg).encode_wide().chain(Some(0)).collect();
            let wide_title: Vec<u16> = OsStr::new("VenTapes")
                .encode_wide()
                .chain(Some(0))
                .collect();
            unsafe {
                windows_sys::Win32::UI::WindowsAndMessaging::MessageBoxW(
                    std::ptr::null_mut(),
                    wide_msg.as_ptr(),
                    wide_title.as_ptr(),
                    0x10, // MB_ICONERROR
                );
            }
        }
    }
}
