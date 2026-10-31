"""Command-line entry point."""
import argparse
import getpass
import os
from pathlib import Path
import signal
import threading
import webbrowser

from . import __version__
from .app import Application
from .auth import Auth


def default_data():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SuperAIMail" / "data"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "superaimail"


def main():
    parser = argparse.ArgumentParser(description="SuperAIMail — 邮件与 AI 工作台")
    parser.add_argument("command", nargs="?", choices=("serve", "init"), default="serve")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data", type=Path, default=Path(os.environ.get("SUPERMAIL_DATA", default_data())))
    parser.add_argument("--host", default=os.environ.get("SUPERMAIL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SUPERMAIL_PORT", "8787")))
    parser.add_argument("--public-url", default=os.environ.get("SUPERMAIL_PUBLIC_URL", ""))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        password = getpass.getpass("设置访问密码（至少 10 个字符）: ")
        if password != getpass.getpass("再次输入: "):
            parser.error("两次输入不同")
        args.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        Auth(args.data).set_password(password)
        print("访问密码已保存。")
        return
    password_file = os.environ.get("SUPERMAIL_PASSWORD_FILE")
    password = Path(password_file).read_text(encoding="utf-8").strip() if password_file else None
    try:
        app = Application(args.data, args.host, args.port, args.public_url, password)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    url = app.start()
    print("SuperAIMail %s — %s" % (__version__, url), flush=True)
    if not args.no_browser and args.host in ("127.0.0.1", "localhost"):
        webbrowser.open(url)
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        stopped.wait()
    finally:
        app.stop()


if __name__ == "__main__":
    main()
