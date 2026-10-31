"""Build a signed Android APK using an existing JDK/SDK and project-local Gradle."""
import argparse
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
GRADLE = "8.13"


def distribution(cache):
    executable = cache / ("gradle-%s/bin/gradle%s" % (GRADLE, ".bat" if os.name == "nt" else ""))
    if executable.is_file():
        return executable
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / ("gradle-%s-bin.zip" % GRADLE)
    url = "https://downloads.gradle.org/distributions/" + archive.name
    checksum = urllib.request.urlopen(url + ".sha256", timeout=30).read().decode().strip()
    if not archive.is_file():
        print("Downloading project build dependency: Gradle " + GRADLE, flush=True)
        urllib.request.urlretrieve(url, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != checksum:
        raise SystemExit("Gradle checksum mismatch; remove " + str(archive))
    with zipfile.ZipFile(archive) as contents:
        contents.extractall(cache)
    if os.name != "nt": executable.chmod(0o755)
    return executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jdk", type=Path, default=os.environ.get("JAVA_HOME"))
    parser.add_argument("--sdk", type=Path, default=os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT"))
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--connected-test", action="store_true")
    args = parser.parse_args()
    if not args.jdk or not args.sdk:
        parser.error("Set JAVA_HOME and ANDROID_HOME, or supply --jdk and --sdk; this tool does not install them")
    suffix = ".exe" if os.name == "nt" else ""
    for required in (args.jdk / ("bin/java" + suffix), args.sdk / "platforms/android-36/android.jar", args.sdk / ("build-tools/36.0.0/aapt2" + suffix)):
        if not required.is_file(): parser.error("Existing build tool missing: " + str(required))
    environment = os.environ.copy()
    environment.update(JAVA_HOME=str(args.jdk.resolve()), ANDROID_HOME=str(args.sdk.resolve()),
                       GRADLE_USER_HOME=str(ROOT / ".build/gradle-home"), SUPERMAIL_BUILD_PYTHON=sys.executable)
    environment["PATH"] = str(args.jdk / "bin") + os.pathsep + environment.get("PATH", "")
    if not args.debug:
        secret_dir = ROOT / ".local-secrets/android"
        secret_dir.mkdir(parents=True, exist_ok=True)
        key, password = secret_dir / "release.p12", secret_dir / "password.txt"
        if key.exists() and not password.exists(): raise SystemExit("Signing password missing; existing key was preserved")
        if not password.exists(): password.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        environment["SUPERMAIL_SIGN_KEY"] = str(key)
        environment["SUPERMAIL_SIGN_PASSWORD"] = password.read_text(encoding="utf-8").strip()
        if not key.exists():
            subprocess.run([str(args.jdk / ("bin/keytool" + suffix)), "-genkeypair", "-keystore", str(key), "-storetype", "PKCS12",
                            "-alias", "superaimail", "-keyalg", "RSA", "-keysize", "3072", "-validity", "10000", "-dname", "CN=SuperAIMail",
                            "-storepass:env", "SUPERMAIL_SIGN_PASSWORD", "-keypass:env", "SUPERMAIL_SIGN_PASSWORD"], env=environment, check=True)
    gradle = distribution(ROOT / ".build/tooling")
    # Invoke Gradle's Java launcher directly: avoids .bat quoting and shell interpolation on Windows.
    launcher = next((gradle.parent.parent / "lib").glob("gradle-launcher-*.jar"))
    task = "connectedDebugAndroidTest" if args.connected_test else "assembleDebug" if args.debug else "assembleRelease"
    command = [str(args.jdk / ("bin/java" + suffix)), "-Xmx64m", "-classpath", str(launcher), "org.gradle.launcher.GradleMain", "--no-daemon", "--console=plain", task]
    subprocess.run(command, cwd=ROOT / "android", env=environment, check=True)
    if args.connected_test: return
    flavor = "debug" if args.debug else "release"
    apk = ROOT / ("android/app/build/outputs/apk/%s/app-%s.apk" % (flavor, flavor))
    output = ROOT / ("dist/SuperAIMail-0.1.0-Android-%s.apk" % ("debug" if args.debug else "universal"))
    output.parent.mkdir(exist_ok=True)
    shutil.copy2(apk, output)
    print(output.name, output.stat().st_size, hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
