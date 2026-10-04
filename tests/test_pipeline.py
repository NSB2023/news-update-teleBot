import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from speech import normalize_for_speech, speech_chunks
from storage import connect, save_article


class PipelineTests(unittest.TestCase):
    def test_speech_is_normalized_and_chunked_safely(self):
        text = normalize_for_speech("On 2026-10-04, US GDP rose 3.5% and cost $1,250.")
        self.assertIn("October fourth", text)
        self.assertIn("three point five percent", text)
        self.assertIn("one thousand two hundred fifty dollars", text)
        chunks = speech_chunks("A sentence with several words, " * 30, max_words=32)
        self.assertTrue(chunks)
        self.assertLessEqual(max(len(chunk.split()) for chunk in chunks), 32)

    def test_opinion_and_duplicate_detection(self):
        self.assertTrue(main.opinion_item("Opinion: A view", "https://example.com/opinion/a", []))
        self.assertTrue(main.same_event("Central bank cuts rates as inflation falls", "Inflation falls as central bank cuts rates"))

    def test_selection_retries_invalid_json_and_caps_publishers(self):
        candidates = [
            {"title": f"Distinct event {i}", "url": f"https://example.com/{i}", "source": "One" if i < 4 else "Two", "published": "today", "summary": "Substantial reported event."}
            for i in range(1, 6)
        ]
        replies = iter([
            "not json",
            '{"selected":[{"id":1,"reason":"important"},{"id":2,"reason":"important"},{"id":3,"reason":"important"},{"id":4,"reason":"important"}]}'
        ])
        cfg = {"max_stories_per_topic": 3, "selection_backup_count": 2, "max_stories_per_publisher": 2, "ollama_model": "test"}
        with tempfile.TemporaryDirectory() as temp, patch.object(main, "ROOT", Path(temp)), patch.object(main, "ask_ollama", side_effect=lambda *a, **k: next(replies)) as ask:
            selected, _ = main.select_stories(cfg, "economy", candidates, [])
        self.assertEqual(ask.call_count, 2)
        self.assertLessEqual(sum(story["source"] == "One" for story in selected), 2)

    def test_selection_falls_back_after_two_invalid_responses(self):
        candidates = [
            {"title": f"Event {i}", "url": f"https://example.com/{i}", "source": f"Publisher {i}", "published": "today", "summary": "Reported development", "when": None}
            for i in range(1, 4)
        ]
        cfg = {"max_stories_per_topic": 2, "selection_backup_count": 0, "max_stories_per_publisher": 2, "ollama_model": "test"}
        with tempfile.TemporaryDirectory() as temp, patch.object(main, "ROOT", Path(temp)), patch.object(main, "ask_ollama", return_value="broken") as ask:
            selected, audit = main.select_stories(cfg, "world", candidates, [])
        self.assertEqual(ask.call_count, 2)
        self.assertEqual(len(selected), 2)
        self.assertTrue(all("fallback" in item["reason"].lower() for item in audit))

    def test_sqlite_stores_article_history(self):
        with tempfile.TemporaryDirectory() as temp:
            db = connect(Path(temp) / "state.sqlite")
            story = {"url": "https://example.com/a", "normalized_title": "example", "source": "Publisher", "published": "today", "evidence_type": "article text"}
            save_article(db, story, topic="world", briefing_date="2026-10-04", edition="morning", status="selected", summary="Summary")
            row = db.execute("SELECT topic, edition, summary, delivery_status FROM articles").fetchone()
            self.assertEqual(tuple(row), ("world", "morning", "Summary", "pending"))
            db.close()


if __name__ == "__main__":
    unittest.main()
