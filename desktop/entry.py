if __name__ == "__main__":
    import os
    from pathlib import Path
    import traceback
    try:
        from superaimail.desktop import main
        main()
    except Exception:
        path = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SuperAIMail"
        path.mkdir(parents=True, exist_ok=True)
        (path / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
