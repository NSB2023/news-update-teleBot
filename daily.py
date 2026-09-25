"""Prepare the briefing, deliver at 08:00, and manage only our own Gemma server."""

import platform
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from main import config, prepare, send


GEMMA = Path("/Users/nazmussakib/Downloads/Work/Gemma_AI_Turbo/turbo-fieldfare")
SERVER = GEMMA / ".build/release/TurboFieldfareServer"
MODEL = GEMMA / "scratch/gemma4.gturbo"
PROCESS_PATTERN = (
    "TurboFieldfareServer|TurboFieldfareMac|TurboFieldfareDecodeService|"
    "TurboFieldfareCLI|TurboFieldfarePackageTests|swiftpm-testing-helper|mlx_lm|mlx-lm"
)


def server_ready(url):
    try:
        return requests.get(url, timeout=3).ok
    except requests.RequestException:
        return False


def start_if_needed(health_url):
    if server_ready(health_url):
        print("Using existing Gemma server", flush=True)
        return None
    processes = subprocess.run(["pgrep", "-fl", PROCESS_PATTERN], capture_output=True, text=True)
    if processes.stdout.strip():
        raise RuntimeError("Another model process is running; stop it yourself before the next scheduled run:\n" + processes.stdout)
    if int(platform.mac_ver()[0].split(".")[0]) < 26:
        raise RuntimeError("TurboFieldfare requires macOS 26 or newer")
    swift = subprocess.run(["swift", "--version"], capture_output=True, text=True, check=True)
    version = re.search(r"Swift version (\d+)\.(\d+)", swift.stdout)
    if not version or tuple(map(int, version.groups())) < (6, 2):
        raise RuntimeError("TurboFieldfare requires Swift 6.2 or newer")
    if not (SERVER.is_file() and (MODEL / "manifest.json").is_file()):
        raise RuntimeError("Gemma server binary or completed model is missing")
    if shutil.disk_usage(GEMMA).free < 2 * 1024**3:
        raise RuntimeError("Less than 2 GB free disk space")
    pressure = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, check=True)
    print(pressure.stdout.strip(), flush=True)
    if "System-wide free percentage:" in pressure.stdout:
        match = re.search(r"System-wide free percentage:\s*(\d+)%", pressure.stdout)
        if match and int(match.group(1)) < 10:
            raise RuntimeError("Memory pressure is high; skipping model startup")
    print("Starting Gemma server", flush=True)
    process = subprocess.Popen(
        [str(SERVER), "--model", str(MODEL), "--port", "8080", "--max-context", "16384"],
        cwd=GEMMA,
    )
    for _ in range(120):
        if server_ready(health_url):
            print("Gemma server ready", flush=True)
            return process
        if process.poll() is not None:
            raise RuntimeError(f"Gemma server exited with code {process.returncode}")
        time.sleep(1)
    process.terminate()
    raise RuntimeError("Gemma server did not become ready within 2 minutes")


def main():
    cfg = config()
    health_url = cfg["gemma_url"].split("/v1/", 1)[0] + "/health"
    owned_server = start_if_needed(health_url)
    try:
        prepare()
        local_now = datetime.now(ZoneInfo(cfg["timezone"]))
        target = local_now.replace(hour=8, minute=0, second=0, microsecond=0)
        if local_now < target:
            seconds = (target - local_now).total_seconds()
            print(f"Briefing ready; waiting {seconds:.0f}s until 08:00", flush=True)
            time.sleep(seconds)
        send()
    finally:
        if owned_server is not None and owned_server.poll() is None:
            owned_server.terminate()
            try:
                owned_server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                owned_server.kill()
                owned_server.wait()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(f"MorningBird failed: {error}") from None
