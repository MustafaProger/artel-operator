"""Install/remove this app as a per-user launchd service (no admin access)."""
from pathlib import Path
import argparse
import os
import plistlib
import subprocess

ROOT = Path(__file__).resolve().parent
LABEL = "ru.artel.operator"
DESTINATION = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()
    domain = f"gui/{os.getuid()}"
    if args.remove:
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False, capture_output=True)
        DESTINATION.unlink(missing_ok=True)
        print("Автозапуск приложения отключён. Отчёты сохранены.")
        return
    interpreter = ROOT / ".venv/bin/python"
    if not interpreter.is_file():
        raise SystemExit("Сначала установите зависимости в .venv")
    logs = ROOT / "data/logs"
    logs.mkdir(parents=True, exist_ok=True)
    os.chmod(ROOT / "data", 0o700)
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    settings = {
        "Label": LABEL,
        "ProgramArguments": [str(interpreter), "-m", "uvicorn", "operator_app.main:app", "--host", "127.0.0.1", "--port", "8790", "--workers", "1"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        # Chromium opens more files/sockets than launchd's default of 256.
        "SoftResourceLimits": {"NumberOfFiles": 8192},
        "StandardOutPath": str(logs / "service.log"),
        "StandardErrorPath": str(logs / "service-error.log"),
        "EnvironmentVariables": {"TZ": "Europe/Moscow", "PYTHONUNBUFFERED": "1"},
    }
    DESTINATION.write_bytes(plistlib.dumps(settings))
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(DESTINATION)], check=True)
    print("Артель Оператор запускается при входе в macOS и восстанавливается при сбое.")


if __name__ == "__main__":
    main()
