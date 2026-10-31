"""Native desktop window around the same local application used by the CLI."""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.parse

from . import __version__
from .__main__ import default_data
from .app import Application


def server_url(value):
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("共享服务地址应为 https:// 开头的完整地址，不含路径、用户名或密码")
    return value.strip().rstrip("/")


def main():
    parser = argparse.ArgumentParser(description="SuperAIMail desktop")
    parser.add_argument("--data", type=Path, default=default_data())
    parser.add_argument("--server", default="")
    parser.add_argument("--smoke", action="store_true", help="Start and verify the packaged backend without opening a window")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    args.data.mkdir(parents=True, exist_ok=True)
    if args.smoke:
        import urllib.request
        app = Application(args.data, port=0)
        try:
            url = app.start()
            result = json.loads(urllib.request.urlopen(url + "/api/session", timeout=10).read())
            if result["version"] != __version__:
                raise RuntimeError("Packaged version does not match")
            if urllib.request.urlopen(url + "/app.js", timeout=10).status != 200:
                raise RuntimeError("Packaged web assets missing")
            (args.data / "smoke.json").write_text(json.dumps({"version": __version__, "ok": True}), encoding="utf-8")
        finally:
            app.stop()
        return
    import webview
    from webview.menu import Menu, MenuAction

    preferences = args.data.parent / "desktop.json"
    saved = json.loads(preferences.read_text(encoding="utf-8")) if preferences.exists() else {}
    remote = server_url(args.server or saved["server"]) if args.server or saved.get("server") else ""
    app = None if remote else Application(args.data)
    url = remote or app.start()
    window = webview.create_window("SuperAIMail", url, width=1320, height=880, min_size=(360, 560), text_select=True, zoomable=True)

    def choose_server():
        # A native dialog avoids exposing a Python bridge to a remote service.
        import tkinter
        from tkinter import simpledialog, messagebox
        root = tkinter.Tk()
        root.withdraw()
        value = simpledialog.askstring("连接共享服务", "输入 HTTPS 服务地址；留空使用本机。更改将在下次启动生效。", initialvalue=remote, parent=root)
        if value is not None:
            try:
                address = server_url(value) if value.strip() else ""
                preferences.write_text(json.dumps({"server": address}), encoding="utf-8")
                messagebox.showinfo("连接已保存", "关闭并重新打开 SuperAIMail 后生效。", parent=root)
            except ValueError as error:
                messagebox.showerror("地址无效", str(error), parent=root)
        root.destroy()

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["ALLOW_FILE_URLS"] = False
    try:
        webview.start(gui="edgechromium" if os.name == "nt" else None, private_mode=False,
                      storage_path=str(args.data.parent / "webview"),
                      menu=[Menu("服务", [MenuAction("连接本机或共享服务…", choose_server)])])
    finally:
        if app:
            app.stop()


if __name__ == "__main__":
    main()
