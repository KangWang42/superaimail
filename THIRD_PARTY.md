# 第三方组件

SuperAIMail 的原创代码采用 MIT 许可。发布包同时包含各自独立授权的运行组件：

| 组件 | 用途 | 许可与来源 |
|---|---|---|
| Python / CPython | Windows、Android 内置后台 | [PSF License](https://docs.python.org/3/license.html) |
| pywebview | Windows 原生窗口 | [BSD 3-Clause](https://github.com/r0x0r/pywebview/blob/master/LICENSE) |
| pythonnet / clr-loader | Windows WebView2 互操作 | [MIT](https://github.com/pythonnet/pythonnet/blob/master/LICENSE)、[MIT](https://github.com/pythonnet/clr-loader/blob/master/LICENSE) |
| Microsoft WebView2 | Windows 浏览器组件 | [微软许可](https://www.nuget.org/packages/Microsoft.Web.WebView2/)；使用系统已有 Runtime |
| PyInstaller | Windows 构建与启动器 | [GPL with bootloader exception](https://pyinstaller.org/en/stable/license.html) |
| Chaquopy | Android 内置 Python | [MIT](https://github.com/chaquo/chaquopy/blob/master/LICENSE)；其 Python 原生依赖保留各自许可 |

Python 自带 OpenSSL、SQLite、zlib 等组件；具体许可见 Python 许可页和包内许可证。构建工具不改变应用源码的 MIT 许可。邮箱服务和模型服务需要使用者自己的账号，并适用对应服务条款。应用不附带邮箱数据或模型权重。
