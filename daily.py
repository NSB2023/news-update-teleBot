"""Run a configured MorningBird edition when it becomes due."""

import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from main import config, prepare, send


def installed_models(url):
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return {item["name"] for item in response.json().get("models", [])}
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return None


def ollama_binary():
    found = shutil.which("ollama")
    if found:
        return found
    for candidate in (Path("/opt/homebrew/bin/ollama"), Path("/usr/local/bin/ollama")):
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("Ollama command not found. Install Ollama or add its binary path to daily.py")


def start_if_needed(cfg):
    tags_url = cfg["ollama_url"].split("/api/", 1)[0] + "/api/tags"
    models = installed_models(tags_url)
    process = None
    if models is None:
        print("Starting Ollama", flush=True)
        process = subprocess.Popen([ollama_binary(), "serve"])
        for _ in range(60):
            models = installed_models(tags_url)
            if models is not None:
                break
            if process.poll() is not None:
                raise RuntimeError(f"Ollama exited with code {process.returncode}")
            time.sleep(1)
        else:
            process.terminate()
            raise RuntimeError("Ollama did not become ready within one minute")
    else:
        print("Using existing Ollama server", flush=True)
    wanted = cfg["ollama_model"]
    if wanted not in models:
        if process is not None:
            process.terminate()
        raise RuntimeError(f"Ollama model {wanted!r} is not installed. Run: ollama pull {wanted}")
    return process


def main():
    cfg = config()
    edition = cfg.get("edition", "morning")
    local_now = datetime.now(ZoneInfo(cfg["timezone"]))
    start_time = datetime.strptime(cfg.get("start_time", "07:40"), "%H:%M").time()
    folder = Path(__file__).resolve().parent / "output" / local_now.date().isoformat() / edition
    if local_now.time() < start_time:
        print(f"{edition.title()} briefing is not due until {start_time:%H:%M}", flush=True)
        return
    if (folder / "delivery.complete").exists():
        print(f"{edition.title()} briefing already delivered for {local_now.date()}", flush=True)
        return
    owned_server = start_if_needed(cfg)
    try:
        prepare(edition=edition)
        send(edition=edition)
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
