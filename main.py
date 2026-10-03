"""Local RSS → Ollama → Pocket TTS → Telegram morning briefing."""

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import wave
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from article import article_text
from prompt import build_news_prompt, build_selection_prompt


ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "state.sqlite"
HEADERS = {"User-Agent": "MorningBirdNSB/0.1 (personal RSS reader)"}


def read_env():
    path = ROOT / ".env"
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def config():
    return json.loads((ROOT / "config.json").read_text())


def database():
    STATE.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(STATE)
    db.execute("CREATE TABLE IF NOT EXISTS sent (path TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
    return db


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def child_text(node, names):
    for child in node:
        if child.tag.split("}")[-1].lower() in names:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def feed_items(feed, cutoff):
    url = feed["url"]
    response = requests.get(url, headers=HEADERS, timeout=25)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items = []
    for node in root.iter():
        if node.tag.split("}")[-1].lower() not in {"item", "entry"}:
            continue
        title = clean(child_text(node, {"title"}))
        link = child_text(node, {"link"})
        if not link:
            link = next((c.attrib.get("href", "") for c in node if c.tag.split("}")[-1] == "link"), "")
        published = child_text(node, {"pubdate", "published", "updated"})
        try:
            when = parsedate_to_datetime(published) if published else None
            if when and when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when and when < cutoff:
                continue
        except (ValueError, TypeError):
            when = None
        summary = clean(child_text(node, {"encoded"}) or child_text(node, {"description", "summary"}))
        if title and link and urlparse(link).scheme in {"http", "https"}:
            categories = [clean("".join(c.itertext())) for c in node if c.tag.split("}")[-1].lower() == "category"]
            if any(category in feed.get("exclude_categories", []) for category in categories):
                continue
            if feed.get("path_contains") and feed["path_contains"] not in urlparse(link).path:
                continue
            if feed.get("keywords"):
                if not any(re.search(r"\b" + re.escape(word) + r"\b", title, re.I) for word in feed["keywords"]):
                    continue
            source = feed["source"]
            items.append({"title": title, "url": link, "summary": summary[:5000], "published": published, "when": when, "source": source})
    return items


def ask_ollama(cfg, prompt, *, timeout, num_predict, json_schema=None):
    payload = {
        "model": cfg["ollama_model"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "keep_alive": "5m",
        "options": {"temperature": 0.1 if json_schema else 0.2, "num_predict": num_predict},
    }
    if json_schema:
        payload["format"] = json_schema
    try:
        response = requests.post(cfg["ollama_url"], json=payload, timeout=timeout)
    except requests.ConnectionError:
        raise RuntimeError(
            f"Ollama is not reachable at {cfg['ollama_url']}. Start it with `ollama serve`."
        ) from None
    except requests.ReadTimeout:
        raise RuntimeError(
            f"Ollama exceeded the {timeout // 60}-minute timeout. Run the command again to resume."
        ) from None
    response.raise_for_status()
    try:
        content = response.json()["message"]["content"]
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("Ollama returned an unexpected response") from None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Ollama returned no text")
    return content.strip()


def ask_ollama_news(cfg, topic, stories):
    prompt = build_news_prompt(topic, stories)
    cache_dir = ROOT / "data" / "analysis-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    signature = hashlib.sha256(
        (cfg["ollama_model"] + "\n" + prompt).encode("utf-8")
    ).hexdigest()
    cache_path = cache_dir / f"{topic}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("signature") == signature and cached.get("script"):
                print(f"Using saved Ollama analysis for {topic}")
                return cached["script"]
        except (json.JSONDecodeError, OSError):
            pass
    script = ask_ollama(cfg, prompt, timeout=1200, num_predict=1100)
    temporary = cache_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"signature": signature, "script": script}, ensure_ascii=False))
    temporary.replace(cache_path)
    return script


def parse_json_object(text):
    """Extract the single JSON object returned by the editorial selection prompt."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Ollama selection response did not contain JSON")
    return json.loads(text[start : end + 1])


def select_stories(cfg, topic, candidates, previously_selected_titles):
    """Use the local Ollama model to choose important, distinct candidate stories."""
    limit = cfg["max_stories_per_topic"]
    prompt = build_selection_prompt(topic, candidates, limit, previously_selected_titles)
    cache_dir = ROOT / "data" / "selection-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    signature = hashlib.sha256(
        (cfg["ollama_model"] + "\n" + prompt).encode("utf-8")
    ).hexdigest()
    cache_path = cache_dir / f"{topic}.json"
    selection = None
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("signature") == signature:
                selection = cached.get("selected")
                if selection:
                    print(f"Using saved Ollama selection for {topic}")
        except (json.JSONDecodeError, OSError):
            pass
    if selection is None:
        schema = {
            "type": "object",
            "properties": {
                "selected": {
                    "type": "array",
                    "maxItems": limit,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "reason": {"type": "string"},
                        },
                        "required": ["id", "reason"],
                    },
                }
            },
            "required": ["selected"],
        }
        try:
            content = ask_ollama(cfg, prompt, timeout=600, num_predict=700, json_schema=schema)
            selection = parse_json_object(content).get("selected")
        except (json.JSONDecodeError, ValueError, AttributeError) as error:
            raise RuntimeError(f"Ollama returned invalid selection JSON for {topic}: {error}") from None
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"signature": signature, "selected": selection}, ensure_ascii=False, indent=2)
        )
        temporary.replace(cache_path)
    if not isinstance(selection, list):
        raise RuntimeError(f"Ollama selection for {topic} is not a list")
    chosen = []
    audit = []
    seen_ids = set()
    for item in selection:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        candidate_id = item["id"]
        if candidate_id in seen_ids or not 1 <= candidate_id <= len(candidates):
            continue
        seen_ids.add(candidate_id)
        story = candidates[candidate_id - 1]
        chosen.append(story)
        audit.append({
            "title": story["title"],
            "publisher": story["source"],
            "url": story["url"],
            "reason": str(item.get("reason", "")),
        })
        if len(chosen) >= limit:
            break
    if not chosen:
        raise RuntimeError(f"Ollama did not select any valid {topic} stories")
    return chosen, audit


def spoken_text(section):
    """Turn numbered written points into clean speech without Markdown tokens."""
    section = re.sub(r"\*\*|__|`", "", section)
    section = re.sub(r"(?m)^\s*#{1,6}\s*", "", section)
    transitions = ("First, ", "Second, ", "Third, ", "Fourth, ", "Next, ")
    section = re.sub(
        r"(?m)^\s*(\d+)[.)]\s*",
        lambda match: transitions[min(int(match.group(1)) - 1, len(transitions) - 1)],
        section,
    )
    section = re.sub(r"(?m)^\s*[-*]\s*", "", section)
    return re.sub(r"\s+", " ", section).strip()


def save_audio(model, voice_state, script, path):
    audio = model.generate_audio(voice_state, script)
    samples = audio.detach().cpu().numpy()
    import numpy as np

    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(model.sample_rate)
        wav.writeframes(pcm.tobytes())


def prepare(refresh=False):
    from zoneinfo import ZoneInfo
    from pocket_tts import TTSModel

    cfg = config()
    health_url = cfg["ollama_url"].split("/api/", 1)[0] + "/api/tags"
    try:
        health = requests.get(health_url, timeout=5)
        health.raise_for_status()
    except requests.RequestException:
        raise RuntimeError(
            f"Ollama is not ready at {health_url}. Start it with `ollama serve`."
        ) from None
    today = datetime.now(ZoneInfo(cfg["timezone"])).date().isoformat()
    folder = ROOT / "output" / today
    folder.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["hours_back"])
    target = folder / "daily_briefing.wav"
    if target.exists() and not refresh:
        print(f"Already prepared: {target.name}")
        return folder
    report = [f"# Morning briefing — {today}\n"]
    sections = []
    used_urls = set()
    used_titles = []
    selection_audit = {}
    for topic, feeds in cfg["feeds"].items():
        candidates = []
        for feed in feeds:
            try:
                candidates.extend(
                    sorted(
                        feed_items(feed, cutoff),
                        key=lambda story: story["when"] or cutoff,
                        reverse=True,
                    )[: cfg["candidates_per_feed"]]
                )
            except (requests.RequestException, ET.ParseError) as error:
                print(f"Feed failed ({feed['url']}): {error}", file=sys.stderr)
        unique = {s["url"].split("?")[0]: s for s in candidates}
        available = sorted(
            (s for key, s in unique.items() if key not in used_urls),
            key=lambda s: s["when"] or cutoff,
            reverse=True,
        )
        if not available:
            print(f"No recent {topic} stories; skipping.")
            continue
        print(f"Selecting {topic} from {len(available)} candidates")
        stories, audit = select_stories(cfg, topic, available, used_titles)
        selection_audit[topic] = audit
        used_urls.update(s["url"].split("?")[0] for s in stories)
        used_titles.extend(s["title"] for s in stories)
        for story in stories:
            try:
                article = article_text(story["url"])
            except requests.RequestException as error:
                print(f"Article unavailable ({story['url']}): {type(error).__name__}", file=sys.stderr)
                article = ""
            story["evidence"] = article or story["summary"]
            story["evidence_type"] = "article text" if article else "feed excerpt only"
        print(f"Analyzing {topic}: {len(stories)} stories")
        script = ask_ollama_news(cfg, topic, stories)
        sections.append((topic, spoken_text(script)))
        report.append(f"## {topic.replace('_', ' ').title()}\n\n{script}\n")
        report.extend(f"- [{s['title']}]({s['url']}) — {s['source']}; {s['published'] or 'date unavailable'}" for s in stories)
        report.append("")
    if not sections:
        raise RuntimeError("No recent stories found in the configured feeds")
    intro = f"Good morning. Here is your news briefing for {datetime.now(ZoneInfo(cfg['timezone'])):%A, %B %d}."
    narration = "\n\n".join([intro] + [f"{topic.replace('_', ' ').title()}. {text}" for topic, text in sections])
    (folder / "briefing.md").write_text("\n".join(report))
    (folder / "narration.txt").write_text(narration)
    (folder / "selection.json").write_text(
        json.dumps(selection_audit, ensure_ascii=False, indent=2)
    )
    print(f"Narration: {len(narration.split())} words")
    model = TTSModel.load_model()
    voice_state = model.get_state_for_audio_prompt(cfg["voice"])
    temp = target.with_suffix(".partial.wav")
    save_audio(model, voice_state, narration, temp)
    temp.replace(target)
    with wave.open(str(target)) as wav:
        duration = wav.getnframes() / wav.getframerate()
    print(f"Created {target} ({duration / 60:.1f} minutes)")
    if duration < 300:
        print("Briefing is under 5 minutes; feed evidence or generated text was shorter than requested.", file=sys.stderr)
    return folder


def telegram_call(method, token, *, data=None, files=None):
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=120)
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError(f"Telegram {method} request failed (HTTP {error.response.status_code if error.response is not None else 'network error'})") from None
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {result.get('description')}")
    return result["result"]


def find_chat():
    read_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN in .env")
    bot = telegram_call("getMe", token)
    print(f"Token belongs to @{bot['username']}")
    webhook = telegram_call("getWebhookInfo", token)
    if webhook.get("url"):
        raise ValueError("This bot has an active webhook; getUpdates cannot receive messages until it is removed.")
    updates = telegram_call("getUpdates", token)
    ids = [u["message"]["chat"]["id"] for u in updates if "message" in u]
    if not ids:
        raise ValueError("No pending messages. Send a NEW message to this exact bot now, then run chat-id again.")
    print(f"Most recent chat ID: {ids[-1]}")


def send():
    from zoneinfo import ZoneInfo

    read_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
    cfg = config()
    folder = ROOT / "output" / datetime.now(ZoneInfo(cfg["timezone"])).date().isoformat()
    files = [folder / "daily_briefing.wav"] if (folder / "daily_briefing.wav").exists() else []
    if not files:
        raise FileNotFoundError(f"No prepared WAV files in {folder}; run prepare first")
    db = database()
    for path in files:
        audio_key = f"{path}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        if db.execute("SELECT 1 FROM sent WHERE path=?", (audio_key,)).fetchone():
            print(f"Already sent: {path.name}")
            continue
        with path.open("rb") as stream:
            telegram_call("sendDocument", token, data={"chat_id": chat_id, "caption": f"MorningBird: {path.stem.replace('_', ' ').title()} — {folder.name}"}, files={"document": (path.name, stream, "audio/wav")})
        db.execute("INSERT INTO sent VALUES (?, ?)", (audio_key, datetime.now(timezone.utc).isoformat()))
        db.commit()
        print(f"Sent {path.name}")
    report = folder / "briefing.md"
    report_key = f"{report}:{hashlib.sha256(report.read_bytes()).hexdigest()}" if report.exists() else ""
    if report.exists() and not db.execute("SELECT 1 FROM sent WHERE path=?", (report_key,)).fetchone():
        with report.open("rb") as stream:
            telegram_call("sendDocument", token, data={"chat_id": chat_id, "caption": "Sources and written briefing"}, files={"document": (report.name, stream, "text/markdown")})
        db.execute("INSERT INTO sent VALUES (?, ?)", (report_key, datetime.now(timezone.utc).isoformat()))
        db.commit()
        print("Sent briefing.md")
    db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "send", "run", "chat-id", "sources", "voice-preview"])
    parser.add_argument("--refresh", action="store_true", help="Regenerate today's briefing even if it exists")
    args = parser.parse_args()
    if args.command == "chat-id":
        find_chat()
    elif args.command == "sources":
        for topic, feeds in config()["feeds"].items():
            print(f"{topic}:")
            for feed in feeds:
                print(f"  {feed['source']}: {feed['url']}")
    elif args.command == "voice-preview":
        from pocket_tts import TTSModel

        model = TTSModel.load_model()
        folder = ROOT / "output" / "voice-previews"
        folder.mkdir(parents=True, exist_ok=True)
        sample = "Good morning. Here are today's most important developments, and why they may matter to you."
        for voice_name in ("alba", "marius", "caro_davy"):
            voice_state = model.get_state_for_audio_prompt(voice_name)
            path = folder / f"{voice_name}.wav"
            save_audio(model, voice_state, sample, path)
            print(path)
    elif args.command == "prepare":
        prepare(refresh=args.refresh)
    elif args.command == "send":
        send()
    else:
        prepare(refresh=args.refresh)
        send()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, FileNotFoundError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
