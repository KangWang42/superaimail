# 安装与构建

## Windows

需要 Windows 10/11 x64、系统已有的 .NET Framework 4.8 与 Microsoft Edge WebView2 Runtime。Windows 11 通常已具备 WebView2；缺少时请从[微软官方页面](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)安装，应用不会自动修改系统运行时。

1. 在 [Releases](https://github.com/KangWang42/superaimail/releases) 下载 `SuperAIMail-0.1.1-Windows-Setup.exe`，核对同页 SHA-256。
2. 打开安装器，选择空目录或已有的 SuperAIMail 安装目录。默认 `%LOCALAPPDATA%/Programs/SuperAIMail` 不需要管理员权限。
3. 从开始菜单打开 SuperAIMail，首次设置工作台密码，再添加邮箱。
4. 需要与手机共享后台时，在「服务 → 连接本机或共享服务」填写自己部署的 HTTPS 来源地址，重启应用生效；留空恢复本机独立运行。

也可以解压 `Windows-x64.zip` 到一个空文件夹运行 `SuperAIMail.exe`。程序包不包含任何预置账号。发布包没有商业代码签名；请只使用本仓库的正式 Release，不把未知来源的重新打包文件当作官方版本。

个人数据保存在 `%LOCALAPPDATA%/SuperAIMail/data`，窗口会话保存在相邻的 `webview/`，共享服务地址保存在 `desktop.json`。关闭窗口后，本机后台退出，不再持续收件。应用没有默认开机启动、托盘驻留或自动更新；需要常在线收件时使用共享后台。卸载只移除程序，保留这些个人数据。

## Android

需要 Android 8.0+、64 位 ARM 或 x86_64 设备，以及可用的 Android System WebView。APK 同时包含 `arm64-v8a` 和 `x86_64` 的 Python 后台。

1. 从本仓库 Release 下载 `SuperAIMail-0.1.1-Android-universal.apk`，检查校验值。
2. 按设备系统流程允许当前安装来源并安装。它不是 Google Play 版本。
3. 首次打开显示「本机独立运行」，创建工作台密码，添加邮箱即可；不需要电脑在线。
4. 点击顶部「连接设置」，填 HTTPS 服务地址可与电脑共用后台；留空恢复本机。切换连接保留原来的本地数据。

手机后台收件受 Android 省电和进程回收限制。本版本没有后台前台服务、保活闹钟和系统推送，不保证锁屏持续收件；重新打开后恢复索引。需要持续在线，推荐使用自托管后台。

Android 10+ 附件保存到公共「下载/SuperAIMail」。Android 8/9 使用应用专用下载目录，不申请整盘存储权限；可用系统文件管理器访问对应应用目录，具体可见性取决于设备。卸载 Android 应用会删除其私有邮箱数据和凭据；当前未提供应用内全量备份导出。升级时在同一应用上覆盖安装，不要先卸载。后续官方版本会沿用同一签名密钥。

## iOS、macOS 与 Linux

iOS 使用 Safari 访问共享服务并添加到主屏幕；本版本不提供独立 iOS 后台或 IPA。macOS/Linux 可以直接运行 Python 后台并用浏览器访问。没有经验证的 macOS 桌面安装包。

## 从源码构建 Windows

已有 Windows x64、Python 3.13、.NET Framework 编译器和 WebView2 时：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r desktop/requirements-build.txt
.venv/Scripts/python tools/package_windows.py
```

输出位于 `dist/`。打包器使用现有系统编译器，不下载 .NET SDK。验证打包后的后台：

```powershell
# PowerShell 中 GUI 程序需要等待退出，才可读取退出码。
$check = Start-Process -FilePath '.build/windows/app/SuperAIMail/SuperAIMail.exe' -ArgumentList @('--smoke','--data','.build/smoke') -WindowStyle Hidden -Wait -PassThru
$check.ExitCode
```

应返回 0，且 `.build/smoke/smoke.json` 的 `ok` 为 true。真实窗口验收另行完成，不能用该检查替代。缺少 WebView2 时不会自动下载运行时。

## 从源码构建 Android

需要已有 Python 3.13、JDK 17 或 21、Android SDK 的 `platforms;android-36` 与 `build-tools;36.0.0`。不需要 Android Studio 或 NDK。设置 `JAVA_HOME`、`ANDROID_HOME` 后运行：

```sh
python tools/package_android.py
```

也可显式传 `--jdk /path/to/jdk --sdk /path/to/sdk`。脚本将 Gradle 8.13 下载到项目 `.build/tooling/`，用官方分发站校验和检查下载；AGP 8.13.2、Chaquopy 17.2.0 及编译依赖下载到 `.build/gradle-home/`。不会安装或升级系统 Java、Python 或 Android SDK。

首次 release 构建生成项目专用签名密钥和随机密码，保存在被 Git 忽略的 `.local-secrets/android/`。请离线备份这一目录；丢失后无法用新密钥覆盖安装旧版。密码通过进程环境传递，不打印到构建日志。贡献者自行构建的包与官方签名不同，不能覆盖官方包。

`--debug` 使用开发签名；`--connected-test --debug` 在已连接的隔离测试设备上运行 Android 测试，不要指向存有私人数据的设备。测试会在应用内创建合成的工作台密码。

官方依据：[Chaquopy Android 集成](https://chaquo.com/chaquopy/doc/current/android.html)、[版本兼容表](https://chaquo.com/chaquopy/doc/current/versions.html)、[AGP 8.13 兼容性](https://developer.android.com/build/releases/agp-8-13-0-release-notes)。
