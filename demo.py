"""Start the local Bug Investigator demo with python3.14 demo.py."""

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parent


def prepare():
    target = tomllib.loads((ROOT / "targets.toml").read_text())["targets"]["lantern"]
    checkout = ROOT / target["local_source_root"]
    if not checkout.exists():
        subprocess.run(["git", "clone", "https://github.com/aikastor/faulty-app.git", checkout], check=True)

    python = ROOT / ".venv/bin/python"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", ROOT / ".venv"], check=True)
    subprocess.run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "-r", ROOT / "requirements.txt"], check=True)

    # Compose builds a missing app image on first use. A running container may
    # still hold the faulty source after a human has approved a local fix.
    subprocess.run(["docker", "compose", "up", "-d", "--no-recreate", "buggy-react"], cwd=checkout, check=True)
    runner = target["baseline"]["runner_image"]
    images = subprocess.check_output(["docker", "image", "ls", "--quiet", runner], text=True)
    if not images.strip():
        subprocess.run(["docker", "compose", "--profile", "tests", "build", "browser-tests"], cwd=checkout, check=True)
    return python


def start():
    api_port = int(os.getenv("INVESTIGATOR_API_PORT", "8174"))
    ui_port = int(os.getenv("INVESTIGATOR_UI_PORT", "8514"))
    host = "127.0.0.1"
    for port in (api_port, ui_port):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError as exc:
                raise SystemExit(f"Could not start on {host}:{port} ({exc.strerror}). "
                                 f"If the demo is running, open http://{host}:{ui_port}.") from exc

    python = prepare()
    api = f"http://{host}:{api_port}"
    ui = f"http://{host}:{ui_port}"
    env = {**os.environ, "INVESTIGATOR_API": api, "INVESTIGATOR_REVIEW_UI": ui}
    services = []
    try:
        for args in (["uvicorn", "investigator.api:app", "--host", host, "--port", str(api_port)],
                     ["streamlit", "run", "streamlit_app.py", "--server.address", host,
                      "--server.port", str(ui_port), "--server.headless", "true", "--browser.gatherUsageStats", "false"]):
            services.append(subprocess.Popen([python, "-m", *args], cwd=ROOT, env=env))
        print(f"Open {ui} for Bug Investigator. Press Ctrl+C to stop.", flush=True)
        while all(service.poll() is None for service in services):
            time.sleep(0.25)
        return next(service.returncode for service in services if service.returncode is not None)
    finally:
        for service in services:
            if service.poll() is None:
                service.terminate()
        for service in services:
            service.wait()


if __name__ == "__main__":
    def stop(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    try:
        sys.exit(start())
    except KeyboardInterrupt:
        sys.exit(130)
    except (subprocess.CalledProcessError, OSError) as exc:
        sys.exit(str(exc))
