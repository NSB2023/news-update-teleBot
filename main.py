"""Local RSS → Ollama → Pocket TTS → Telegram morning briefing."""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from article import article_text
from prompt import build_news_prompt, build_selection_prompt
from speech import encode_telegram_voice, save_chunked_wav
from storage import connect as storage_connect
from storage import clear_edition_articles, mark_topic_delivered, save_article, save_run


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
    return storage_connect(STATE)


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def normalized_title(value):
    words = re.findall(r"[a-z0-9]+", value.lower())
    ignored = {"a", "an", "and", "at", "for", "from", "in", "of", "on", "the", "to", "with"}
    return " ".join(word for word in words if word not in ignored)


def same_event(first, second):
    left, right = set(normalized_title(first).split()), set(normalized_title(second).split())
    return bool(left and right) and len(left & right) / len(left | right) >= 0.58


def south_asia_story(story):
    text = f"{story.get('title', '')} {story.get('summary', '')}".lower()
    places = (
        "bangladesh", "bangladeshi", "dhaka", "south asia", "india", "indian",
        "pakistan", "pakistani", "nepal", "nepali", "sri lanka", "sri lankan",
        "bhutan", "maldives", "afghanistan",
    )
    return any(place in text for place in places)


def fallback_selection(candidates, limit, publisher_limit):
    """Choose a diverse recent set when structured model output remains invalid."""
    ordered = sorted(
        enumerate(candidates, 1),
        key=lambda pair: (not south_asia_story(pair[1]), -(pair[1]["when"].timestamp() if pair[1].get("when") else 0)),
    )
    selected = []
    publishers = {}
    titles = []
    for candidate_id, story in ordered:
        if publishers.get(story["source"], 0) >= publisher_limit:
            continue
        if any(same_event(story["title"], title) for title in titles):
            continue
        selected.append({"id": candidate_id, "reason": "Deterministic fallback after invalid model selection"})
        publishers[story["source"]] = publishers.get(story["source"], 0) + 1
        titles.append(story["title"])
        if len(selected) >= limit:
            break
    return selected


def opinion_item(title, link, categories):
    labels = " ".join(categories).lower()
    path = urlparse(link).path.lower()
    return (
        "opinion" in labels
        or "/opinion/" in path
        or bool(re.match(r"^(opinion|comment|editorial)\s*[:|—-]", title, re.I))
    )


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
            excluded = {category.lower() for category in feed.get("exclude_categories", [])}
            if any(category.lower() in excluded for category in categories):
                continue
            if opinion_item(title, link, categories):
                continue
            if feed.get("path_contains") and feed["path_contains"] not in urlparse(link).path:
                continue
            if feed.get("keywords"):
                if not any(re.search(r"\b" + re.escape(word) + r"\b", title, re.I) for word in feed["keywords"]):
                    continue
            source = feed["source"]
            items.append({
                "title": title, "normalized_title": normalized_title(title), "url": link,
                "summary": summary[:5000], "published": published, "when": when,
                "source": source, "content_type": "reporting",
            })
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
    last_error = None
    for attempt in range(2):
        try:
            response = requests.post(cfg["ollama_url"], json=payload, timeout=timeout)
            response.raise_for_status()
            content = response.json()["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Ollama returned no text")
            return content.strip()
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            last_error = error
            if attempt == 0:
                print("Ollama request failed; retrying once", file=sys.stderr, flush=True)
                time.sleep(2)
    if isinstance(last_error, requests.ConnectionError):
        raise RuntimeError(
            f"Ollama is not reachable at {cfg['ollama_url']}. Start it with `ollama serve`."
        ) from None
    if isinstance(last_error, requests.ReadTimeout):
        raise RuntimeError(f"Ollama exceeded the {timeout // 60}-minute timeout after one retry") from None
    raise RuntimeError(f"Ollama request failed after one retry: {last_error}") from None


def valid_news_script(script, story_count):
    numbered_points = re.findall(r"(?m)^\s*\d+[.)]\s+", script)
    return len(numbered_points) >= story_count and len(script.split()) >= story_count * 35


def ask_ollama_news(cfg, topic, stories):
    prompts = [
        build_news_prompt(topic, [story]) + (
            "\n\nReturn JSON only with this structure: "
            '{"points":[{"text":"complete factual narration for this article"}]}. '
            "Return exactly one detailed point."
        )
        for story in stories
    ]
    cache_dir = ROOT / "data" / "analysis-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    signature = hashlib.sha256(
        (cfg["ollama_model"] + "\nsingle-article-json-v1\n" + "\n".join(prompts)).encode("utf-8")
    ).hexdigest()
    cache_path = cache_dir / f"{topic}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if (
                cached.get("signature") == signature
                and cached.get("script")
                and valid_news_script(cached["script"], len(stories))
            ):
                print(f"Using saved Ollama analysis for {topic}")
                return cached["script"]
        except (json.JSONDecodeError, OSError):
            pass
    schema = {
        "type": "object",
        "properties": {
            "points": {
                "type": "array", "minItems": 1, "maxItems": 1,
                "items": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        },
        "required": ["points"],
    }
    summaries = []
    for article_index, prompt in enumerate(prompts, 1):
        summary = ""
        for attempt in range(2):
            request_prompt = prompt
            if attempt:
                request_prompt += "\n\nThe previous response was incomplete. Return one detailed factual point."
            content = ask_ollama(
                cfg, request_prompt, timeout=180, num_predict=300, json_schema=schema
            )
            try:
                points = parse_json_object(content).get("points", [])
                if len(points) == 1 and isinstance(points[0], dict):
                    candidate = points[0].get("text", "").strip()
                    if len(candidate.split()) >= 35:
                        summary = candidate
                        break
            except (json.JSONDecodeError, ValueError, AttributeError):
                pass
            if attempt == 0:
                print(f"Incomplete {topic} article {article_index}; retrying once", file=sys.stderr)
        if not summary:
            raise RuntimeError(
                f"Ollama returned an incomplete {topic} summary for article {article_index} after one retry"
            )
        summaries.append(summary)
    script = "\n\n".join(f"{index}. {text}" for index, text in enumerate(summaries, 1))
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
    selection_limit = min(len(candidates), limit + cfg.get("selection_backup_count", 4))
    prompt = build_selection_prompt(topic, candidates, selection_limit, previously_selected_titles)
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
                    "maxItems": selection_limit,
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
        parse_error = None
        for attempt in range(2):
            content = ask_ollama(cfg, prompt, timeout=600, num_predict=700, json_schema=schema)
            try:
                selection = parse_json_object(content).get("selected")
                if not isinstance(selection, list):
                    raise ValueError("selected is not a list")
                break
            except (json.JSONDecodeError, ValueError, AttributeError) as error:
                parse_error = error
                if attempt == 0:
                    print(f"Invalid {topic} selection; retrying once", file=sys.stderr)
        if selection is None:
            print(
                f"Ollama returned invalid selection JSON for {topic} after one retry; "
                "using deterministic fallback",
                file=sys.stderr,
            )
            selection = fallback_selection(
                candidates, selection_limit, cfg.get("max_stories_per_publisher", 2)
            )
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
    publisher_counts = {}
    publisher_limit = cfg.get("max_stories_per_publisher", 2)
    for item in selection:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        candidate_id = item["id"]
        if candidate_id in seen_ids or not 1 <= candidate_id <= len(candidates):
            continue
        seen_ids.add(candidate_id)
        story = candidates[candidate_id - 1]
        if publisher_counts.get(story["source"], 0) >= publisher_limit:
            continue
        if any(same_event(story["title"], existing["title"]) for existing in chosen):
            continue
        chosen.append(story)
        publisher_counts[story["source"]] = publisher_counts.get(story["source"], 0) + 1
        audit.append({
            "title": story["title"],
            "publisher": story["source"],
            "url": story["url"],
            "reason": str(item.get("reason", "")),
        })
        if len(chosen) >= selection_limit:
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


def save_audio(model, voice_state, script, path, cfg=None):
    """Compatibility wrapper used by voice-preview."""
    settings = (cfg or {}).get("tts", {})
    return save_chunked_wav(
        model, voice_state, script, path,
        max_words=settings.get("max_words_per_chunk", 16),
        pause_ms=settings.get("pause_ms", 350),
    )


def split_summary_points(script, count):
    parts = re.split(r"(?m)^\s*\d+[.)]\s*", script.strip())
    points = [part.strip() for part in parts if part.strip()]
    if len(points) == count:
        return points
    return [script.strip()] * count


def write_errors(folder, errors):
    path = folder / "errors.md"
    if not errors:
        if path.exists():
            path.unlink()
        return
    lines = ["# MorningBird errors", "", "Completed topics were preserved. The following problems occurred:", ""]
    lines.extend(f"- **{item['stage']}** — {item['message']}" for item in errors)
    path.write_text("\n".join(lines) + "\n")


def prepare(refresh=False, edition=None):
    from zoneinfo import ZoneInfo
    from pocket_tts import TTSModel

    cfg = config()
    edition = edition or cfg.get("edition", "morning")
    health_url = cfg["ollama_url"].split("/api/", 1)[0] + "/api/tags"
    try:
        health = requests.get(health_url, timeout=5)
        health.raise_for_status()
    except requests.RequestException:
        raise RuntimeError(
            f"Ollama is not ready at {health_url}. Start it with `ollama serve`."
        ) from None
    today = datetime.now(ZoneInfo(cfg["timezone"])).date().isoformat()
    folder = ROOT / "output" / today / edition
    folder.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["hours_back"])
    complete_marker = folder / "prepare.complete"
    if complete_marker.exists() and not refresh:
        try:
            previous_status = json.loads((folder / "manifest.json").read_text()).get("status")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            previous_status = None
        if previous_status == "success":
            print(f"Already prepared: {today} {edition}")
            return folder
        complete_marker.unlink(missing_ok=True)
    if refresh:
        complete_marker.unlink(missing_ok=True)
        for old_file in folder.glob(f"{today}_{edition}_*"):
            if old_file.is_file():
                old_file.unlink()
    report = [f"# {edition.title()} briefing — {today}\n"]
    errors = []
    completed_topics = []
    used_urls = set()
    used_titles = []
    selection_path = folder / "selection.json"
    try:
        selection_audit = json.loads(selection_path.read_text()) if selection_path.exists() and not refresh else {}
    except (json.JSONDecodeError, OSError):
        selection_audit = {}
    db = database()
    if refresh:
        clear_edition_articles(db, today, edition)
    model = None
    voice_state = None
    for topic, feeds in cfg["feeds"].items():
        stem = f"{today}_{edition}_{topic}"
        existing_voice = folder / f"{stem}.ogg"
        existing_report = folder / f"{stem}.md"
        if not refresh and existing_voice.exists() and existing_report.exists():
            completed_topics.append(topic)
            report.extend(existing_report.read_text().splitlines() + [""])
            for item in selection_audit.get(topic, {}).get("selected", []):
                used_urls.add(item.get("url", "").split("?")[0])
                used_titles.append(item.get("title", ""))
            print(f"Reusing completed topic: {topic}")
            continue
        try:
            candidates = []
            for feed in feeds:
                try:
                    candidates.extend(
                        sorted(feed_items(feed, cutoff), key=lambda story: story["when"] or cutoff, reverse=True)
                        [: cfg["candidates_per_feed"]]
                    )
                except (requests.RequestException, ET.ParseError) as error:
                    message = f"{feed['source']} feed failed for {topic}: {type(error).__name__}"
                    print(message, file=sys.stderr)
                    errors.append({"stage": f"feed/{topic}", "message": message})
            unique = {story["url"].split("?")[0]: story for story in candidates}
            available = sorted(
                (
                    story for key, story in unique.items()
                    if key not in used_urls
                    and not any(same_event(story["title"], title) for title in used_titles)
                ),
                key=lambda story: story["when"] or cutoff,
                reverse=True,
            )
            for story in available:
                save_article(db, story, topic=topic, briefing_date=today, edition=edition, status="candidate")
            if not available:
                raise RuntimeError("No recent, distinct reporting was available")
            print(f"Selecting {topic} from {len(available)} candidates")
            selected_pool, audit_pool = select_stories(cfg, topic, available, used_titles)
            reason_by_url = {item["url"]: item["reason"] for item in audit_pool}
            stories = []
            rejected = []
            for story in selected_pool:
                try:
                    article = article_text(story["url"])
                except requests.RequestException as error:
                    print(f"Article unavailable ({story['url']}): {type(error).__name__}", file=sys.stderr)
                    article = ""
                story["evidence"] = article or story["summary"]
                story["evidence_type"] = "article text" if article else "feed excerpt only"
                if len(story["evidence"].split()) < cfg.get("min_evidence_words", 60):
                    rejected.append({"title": story["title"], "reason": "insufficient evidence"})
                    save_article(db, story, topic=topic, briefing_date=today, edition=edition, status="rejected_insufficient_evidence")
                    continue
                stories.append(story)
                if len(stories) >= cfg["max_stories_per_topic"]:
                    break
            if not stories:
                raise RuntimeError("Selected stories did not contain enough extractable evidence")
            audit = [
                {
                    "title": story["title"], "publisher": story["source"], "url": story["url"],
                    "reason": reason_by_url.get(story["url"], "Selected as a qualified replacement"),
                    "evidence_type": story["evidence_type"],
                }
                for story in stories
            ]
            selection_audit[topic] = {"selected": audit, "rejected": rejected}
            used_urls.update(story["url"].split("?")[0] for story in stories)
            used_titles.extend(story["title"] for story in stories)
            print(f"Analyzing {topic}: {len(stories)} stories")
            script = ask_ollama_news(cfg, topic, stories)
            summaries = split_summary_points(script, len(stories))
            for story, summary in zip(stories, summaries):
                save_article(
                    db, story, topic=topic, briefing_date=today, edition=edition,
                    status="selected", summary=summary,
                )
            narration = f"{topic.replace('_', ' ').title()}. {spoken_text(script)}"
            (folder / f"{stem}.txt").write_text(narration)
            topic_report = [f"# {topic.replace('_', ' ').title()} — {today} ({edition})", "", script, "", "## Sources", ""]
            topic_report.extend(
                f"- [{story['title']}]({story['url']}) — {story['source']}; "
                f"{story['published'] or 'date unavailable'}; {story['evidence_type']}"
                for story in stories
            )
            (folder / f"{stem}.md").write_text("\n".join(topic_report) + "\n")
            if model is None:
                model = TTSModel.load_model()
                voice_state = model.get_state_for_audio_prompt(cfg["voice"])
            wav_path = folder / f"{stem}.wav"
            duration, chunks = save_audio(model, voice_state, narration, wav_path, cfg)
            ogg_path = folder / f"{stem}.ogg"
            encode_telegram_voice(wav_path, ogg_path, cfg.get("tts", {}).get("ffmpeg_path", ""))
            completed_topics.append(topic)
            report.extend(topic_report + [""])
            print(f"Created {ogg_path.name} ({duration / 60:.1f} minutes, {chunks} speech chunks)")
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            print(f"{topic} failed: {message}", file=sys.stderr)
            errors.append({"stage": f"topic/{topic}", "message": message})
        finally:
            write_errors(folder, errors)
            (folder / "selection.json").write_text(json.dumps(selection_audit, ensure_ascii=False, indent=2))
            (folder / "briefing.md").write_text("\n".join(report))
    status = "success" if completed_topics and not errors else "partial" if completed_topics else "failed"
    save_run(db, today, edition, status, len(errors))
    db.close()
    manifest = {
        "date": today, "edition": edition, "status": status,
        "completed_topics": completed_topics, "error_count": len(errors),
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if not completed_topics:
        raise RuntimeError(f"No topics completed; see {folder / 'errors.md'}")
    if status == "success":
        complete_marker.write_text(datetime.now(timezone.utc).isoformat())
    else:
        complete_marker.unlink(missing_ok=True)
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


def send(edition=None):
    from zoneinfo import ZoneInfo

    read_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
    cfg = config()
    edition = edition or cfg.get("edition", "morning")
    briefing_date = datetime.now(ZoneInfo(cfg["timezone"])).date().isoformat()
    folder = ROOT / "output" / briefing_date / edition
    files = sorted(folder.glob(f"{briefing_date}_{edition}_*.ogg"))
    if not files:
        raise FileNotFoundError(f"No prepared OGG/Opus topic files in {folder}; run prepare first")
    db = database()
    for path in files:
        topic = path.stem.removeprefix(f"{briefing_date}_{edition}_")
        audio_key = f"{path}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        if db.execute("SELECT 1 FROM sent WHERE path=?", (audio_key,)).fetchone():
            print(f"Already sent: {path.name}")
            continue
        with path.open("rb") as stream:
            telegram_call(
                "sendVoice", token,
                data={
                    "chat_id": chat_id,
                    "caption": f"MorningBird • {briefing_date} • {edition.title()} • {topic.replace('_', ' ').title()}",
                },
                files={"voice": (path.name, stream, "audio/ogg")},
            )
        db.execute("INSERT INTO sent VALUES (?, ?)", (audio_key, datetime.now(timezone.utc).isoformat()))
        db.commit()
        mark_topic_delivered(db, briefing_date, edition, topic)
        print(f"Sent {path.name}")
    for report, caption in (
        (folder / "briefing.md", f"MorningBird sources — {briefing_date} {edition}"),
        (folder / "errors.md", f"MorningBird processing notes — {briefing_date} {edition}"),
    ):
        report_key = f"{report}:{hashlib.sha256(report.read_bytes()).hexdigest()}" if report.exists() else ""
        if report.exists() and not db.execute("SELECT 1 FROM sent WHERE path=?", (report_key,)).fetchone():
            with report.open("rb") as stream:
                telegram_call(
                    "sendDocument", token, data={"chat_id": chat_id, "caption": caption},
                    files={"document": (report.name, stream, "text/markdown")},
                )
            db.execute("INSERT INTO sent VALUES (?, ?)", (report_key, datetime.now(timezone.utc).isoformat()))
            db.commit()
            print(f"Sent {report.name}")
    manifest = json.loads((folder / "manifest.json").read_text())
    save_run(db, briefing_date, edition, "delivered", manifest.get("error_count", 0))
    db.close()
    (folder / "delivery.complete").write_text(datetime.now(timezone.utc).isoformat())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "send", "run", "chat-id", "sources", "voice-preview"])
    parser.add_argument("--refresh", action="store_true", help="Regenerate today's briefing even if it exists")
    parser.add_argument("--edition", choices=["morning", "afternoon"], help="Override the configured edition")
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
            save_audio(model, voice_state, sample, path, config())
            print(path)
    elif args.command == "prepare":
        prepare(refresh=args.refresh, edition=args.edition)
    elif args.command == "send":
        send(edition=args.edition)
    else:
        prepare(refresh=args.refresh, edition=args.edition)
        send(edition=args.edition)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, FileNotFoundError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
