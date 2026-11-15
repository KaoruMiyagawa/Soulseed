# 构建 Soulseed Windows 体验版

使用 Windows x64 和 PowerShell 7。构建机需要 Python x64、Node.js、Rust MSVC stable、MSVC C++ x64 工具、Windows SDK 和 WebView2。安装端只依赖 WebView2，NSIS 可为缺少运行时的电脑补装。

```powershell
.venv\Scripts\python.exe -m pip install -r backend/requirements.txt -r backend/requirements-desktop.txt
npm.cmd --prefix frontend ci
.\build-windows.ps1
```

产物输出到 `desktop-build/release/<版本>/`。版本使用 `frontend/src-tauri/tauri.conf.json` 和 `Cargo.toml`，正式发布时两处一致。首个预发布编号 `2.9.1-beta.1` 沿用 2.9.0 开发检查点的产品版本线。保持 `com.soulseed.desktop` 和当前用户安装方式，供后续安装包识别已有程序和数据。

PyInstaller 仅以现有后端入口、人格模板和运行时依赖构建 Sidecar。桌面端 Vite 产物独立于浏览器开发构建。Tauri 用受限 IPC 将前端 API 请求转发到 Sidecar 的动态回环端口。每次启动生成新的随机凭据，经继承管道传递。Windows Job Object 在宿主异常退出时回收 Sidecar。SQLite 使用当前 Windows 账户的本地数据目录，只迁移带桌面应用标识的数据库，升级前创建 SQLite 一致性备份。

发行验收应在全新的 Windows 10 和 11 x64 环境执行，并覆盖 WebView2 补装、开始菜单入口、同步对话、退出、升级保留与卸载。公开 Beta 前应决定签名和下载渠道。经确认后，在获准的发行位置建立 `v2.9.1-beta.1` 预发布，使用发行说明并上传安装包、校验文件与用户指南，然后在未登录浏览器中核验下载及 SHA256。
