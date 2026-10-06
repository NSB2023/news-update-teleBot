"""Local configuration desk for MorningBird. Run: python ui_server.py"""

import json
import secrets
import webbrowser
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Timer
from urllib.parse import urlparse

import requests

from main import ROOT, config


HOST = "127.0.0.1"
PORT = 8765
UI = ROOT / "ui"
TOKEN = secrets.token_urlsafe(24)
TOPICS = ("world", "economy", "ai_technology", "education", "politics")
VOICES = ("caro_davy", "alba", "marius")
COUNTRIES = ("Bangladesh", "India", "Pakistan", "Nepal", "Sri Lanka", "United Kingdom", "United States", "Other")
REGIONS = ("South Asia", "Middle East", "Europe", "Africa", "Americas", "East Asia", "Global")


def frontpage_stories():
    """Show only publisher-supplied RSS photos, with their real article links."""
    stories = []
    for feed in config()["feeds"].get("world", []):
        if feed.get("enabled", True) is False:
            continue
        try:
            response = requests.get(feed["url"], timeout=5, headers={"User-Agent": "MorningBird/1.0 (personal RSS reader)"})
            response.raise_for_status()
            root = ET.fromstring(response.content)
            for item in root.findall(".//item")[:12]:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                photo = None
                for child in item:
                    tag = child.tag.rsplit("}", 1)[-1]
                    if tag in ("thumbnail", "content", "enclosure"):
                        candidate = child.attrib.get("url", "")
                        if candidate.startswith("https://") and (
                            tag == "thumbnail" or child.attrib.get("medium") == "image"
                            or child.attrib.get("type", "").startswith("image/")
                        ):
                            photo = candidate
                            break
                if title and link.startswith("https://") and photo:
                    stories.append({"title": title, "url": link, "image": photo, "source": feed["source"]})
                    break
        except (requests.RequestException, ET.ParseError):
            continue
        if len(stories) == 3:
            break
    return stories


def safe_settings():
    cfg = config()
    try:
        response = requests.get(cfg["ollama_url"].split("/api/", 1)[0] + "/api/tags", timeout=2)
        response.raise_for_status()
        models = [item["name"] for item in response.json().get("models", [])]
    except (requests.RequestException, ValueError, KeyError):
        models = [cfg["ollama_model"]]
    return {
        "csrf": TOKEN,
        "timezone": cfg.get("timezone", "Asia/Dhaka"),
        "start_time": cfg.get("start_time", "07:40"),
        "edition": cfg.get("edition", "morning"),
        "voice": cfg.get("voice", "caro_davy"),
        "ollama_model": cfg.get("ollama_model", "qwen3.5:4b"),
        "models": sorted(set(models + [cfg.get("ollama_model", "qwen3.5:4b")])),
        "max_stories_per_topic": cfg.get("max_stories_per_topic", 6),
        "home_country": cfg.get("home_country", "Bangladesh"),
        "regions": cfg.get("regions", ["South Asia", "Global"]),
        "enabled_topics": cfg.get("enabled_topics", list(TOPICS)),
        "feeds": cfg["feeds"],
        "countries": COUNTRIES,
        "region_options": REGIONS,
        "voice_options": VOICES,
    }


def validate_and_save(body):
    cfg = config()
    enabled_topics = body.get("enabled_topics")
    if not isinstance(enabled_topics, list) or not enabled_topics or any(item not in TOPICS for item in enabled_topics):
        raise ValueError("Choose at least one valid topic.")
    regions = body.get("regions")
    if not isinstance(regions, list) or not regions or any(item not in REGIONS for item in regions):
        raise ValueError("Choose at least one region.")
    country = body.get("home_country")
    if country not in COUNTRIES:
        raise ValueError("Choose a valid home country.")
    time_value = body.get("start_time")
    if not isinstance(time_value, str) or len(time_value) != 5 or time_value[2] != ":":
        raise ValueError("Enter a time such as 07:40.")
    try:
        hour, minute = map(int, time_value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        raise ValueError("Enter a valid 24-hour time.") from None
    voice = body.get("voice")
    if voice not in VOICES:
        raise ValueError("Choose a listed voice.")
    model = body.get("ollama_model")
    if not isinstance(model, str) or not model or len(model) > 100 or any(c.isspace() for c in model):
        raise ValueError("Choose a valid installed model.")
    count = body.get("max_stories_per_topic")
    if type(count) is not int or not 1 <= count <= 10:
        raise ValueError("Stories per topic must be between 1 and 10.")
    enabled_feeds = body.get("enabled_feeds")
    if not isinstance(enabled_feeds, list) or any(not isinstance(url, str) for url in enabled_feeds):
        raise ValueError("Source selection is invalid.")
    available_urls = {feed["url"] for feeds in cfg["feeds"].values() for feed in feeds}
    if any(url not in available_urls for url in enabled_feeds):
        raise ValueError("Source selection contains an unknown feed.")
    if not any(feed["url"] in enabled_feeds for topic in enabled_topics for feed in cfg["feeds"][topic]):
        raise ValueError("Enable a source for at least one selected topic.")

    cfg.update({
        "home_country": country,
        "regions": regions,
        "enabled_topics": enabled_topics,
        "start_time": time_value,
        "voice": voice,
        "ollama_model": model,
        "max_stories_per_topic": count,
    })
    for feeds in cfg["feeds"].values():
        for feed in feeds:
            feed["enabled"] = feed["url"] in enabled_feeds
    path = ROOT / "config.json"
    temporary = ROOT / "config.json.tmp"
    temporary.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, content, mime="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/settings":
            return self.respond(200, json.dumps(safe_settings()).encode())
        if route == "/api/frontpage":
            return self.respond(200, json.dumps(frontpage_stories()).encode())
        files = {
            "/": (UI / "index.html", "text/html; charset=utf-8"),
            "/style.css": (UI / "style.css", "text/css; charset=utf-8"),
            "/app.js": (UI / "app.js", "text/javascript; charset=utf-8"),
            "/assets/commuter-editorial.png": (UI / "assets" / "commuter-editorial.png", "image/png"),
        }
        item = files.get(route)
        if item and item[0].is_file():
            return self.respond(200, item[0].read_bytes(), item[1])
        self.respond(404, b'{"error":"Not found"}')

    def do_POST(self):
        if self.path != "/api/settings":
            return self.respond(404, b'{"error":"Not found"}')
        if self.headers.get("X-MorningBird-Token") != TOKEN:
            return self.respond(403, b'{"error":"Invalid session"}')
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 32_000:
            return self.respond(413, b'{"error":"Invalid request size"}')
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Invalid settings.")
            validate_and_save(body)
        except (ValueError, OSError) as error:
            return self.respond(400, json.dumps({"error": str(error)}).encode())
        self.respond(200, b'{"saved":true}')


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    address = f"http://{HOST}:{PORT}"
    print(f"MorningBird settings: {address}", flush=True)
    Timer(0.5, webbrowser.open, args=[address]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
