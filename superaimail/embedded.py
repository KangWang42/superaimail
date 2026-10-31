"""Small entry point for Android's embedded Python; no Android imports required."""
import threading

from .app import Application

_lock = threading.Lock()
_app = None


def start(data):
    global _app
    with _lock:
        if _app is None:
            _app = Application(data)
            _app.start()
        return _app.url


def stop():
    global _app
    with _lock:
        if _app is not None:
            _app.stop()
            _app = None
