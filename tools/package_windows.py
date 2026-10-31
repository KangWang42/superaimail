"""Build with an existing Python and .NET Framework compiler on Windows.

Run using a project venv containing desktop/requirements-build.txt.
"""
import hashlib
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from superaimail import __version__


def main():
    if os.name != "nt":
        raise SystemExit("Windows build requires Windows")
    build, dist = ROOT / ".build/windows", ROOT / "dist"
    build.mkdir(parents=True, exist_ok=True)
    dist.mkdir(exist_ok=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
                    "--name", "SuperAIMail", "--icon", str(ROOT / "desktop/icon.ico"), "--paths", str(ROOT), "--collect-all", "webview",
                    "--add-data", str(ROOT / "superaimail/web") + ";superaimail/web", "--hidden-import", "tkinter",
                    "--specpath", str(build), "--workpath", str(build / "work"), "--distpath", str(build / "app"),
                    str(ROOT / "desktop/entry.py")], check=True, cwd=ROOT)
    stage = build / "app/SuperAIMail"
    for name in ("LICENSE", "THIRD_PARTY.md", "README.md"):
        shutil.copy2(ROOT / name, stage / name)
    shutil.copytree(ROOT / "docs", stage / "docs", dirs_exist_ok=True)
    notices = stage / "licenses"
    notices.mkdir(exist_ok=True)
    shutil.copy2(Path(sys.base_prefix) / "LICENSE.txt", notices / "Python-LICENSE.txt")
    for package in ("pywebview", "pythonnet", "clr-loader", "cffi", "pycparser", "proxy_tools", "bottle", "typing_extensions", "pyinstaller"):
        distribution = importlib.metadata.distribution(package)
        target = notices / package
        target.mkdir(exist_ok=True)
        for item in distribution.files or []:
            if any(word in str(item).lower() for word in ("license", "copying", "notice")) and distribution.locate_file(item).is_file():
                shutil.copy2(distribution.locate_file(item), target / Path(item).name)
    payload = build / "payload.zip"
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(stage).as_posix())
    shutil.copy2(payload, dist / ("SuperAIMail-%s-Windows-x64.zip" % __version__))
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/codepage:65001",
                    "/out:" + str(dist / ("SuperAIMail-%s-Windows-Setup.exe" % __version__)), "/win32icon:" + str(ROOT / "desktop/icon.ico"),
                    "/resource:%s,payload.zip" % payload, "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
                    "/reference:System.IO.Compression.dll", "/reference:System.IO.Compression.FileSystem.dll",
                    str(ROOT / "desktop/Setup.cs")], check=True)
    for path in sorted(dist.glob("*Windows*")):
        print(path.name, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
