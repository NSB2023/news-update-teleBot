"""SQLite state for articles, pipeline runs, and Telegram deliveries."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS sent (path TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            url TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            publisher TEXT NOT NULL,
            published_at TEXT,
            topic TEXT NOT NULL,
            selection_status TEXT NOT NULL,
            briefing_date TEXT NOT NULL,
            edition TEXT NOT NULL,
            evidence_type TEXT,
            summary TEXT,
            delivery_status TEXT NOT NULL DEFAULT 'pending',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (url, briefing_date, edition, topic)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            briefing_date TEXT NOT NULL,
            edition TEXT NOT NULL,
            status TEXT NOT NULL,
            error_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (briefing_date, edition)
        )
        """
    )
    db.commit()
    return db


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def save_article(db, story, *, topic, briefing_date, edition, status, summary=""):
    db.execute(
        """
        INSERT INTO articles (
            url, normalized_title, publisher, published_at, topic,
            selection_status, briefing_date, edition, evidence_type,
            summary, delivery_status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(url, briefing_date, edition, topic) DO UPDATE SET
            normalized_title=excluded.normalized_title,
            publisher=excluded.publisher,
            published_at=excluded.published_at,
            selection_status=excluded.selection_status,
            evidence_type=excluded.evidence_type,
            summary=excluded.summary,
            delivery_status='pending',
            updated_at=excluded.updated_at
        """,
        (
            story["url"], story.get("normalized_title", ""), story["source"],
            story.get("published"), topic, status, briefing_date, edition,
            story.get("evidence_type"), summary, now_utc(),
        ),
    )
    db.commit()


def clear_edition_articles(db, briefing_date, edition):
    db.execute(
        "DELETE FROM articles WHERE briefing_date=? AND edition=?",
        (briefing_date, edition),
    )
    db.commit()


def mark_topic_delivered(db, briefing_date, edition, topic):
    db.execute(
        """
        UPDATE articles SET delivery_status='delivered', updated_at=?
        WHERE briefing_date=? AND edition=? AND topic=? AND selection_status='selected'
        """,
        (now_utc(), briefing_date, edition, topic),
    )
    db.commit()


def save_run(db, briefing_date, edition, status, error_count):
    db.execute(
        """
        INSERT INTO runs VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(briefing_date, edition) DO UPDATE SET
            status=excluded.status,
            error_count=excluded.error_count,
            updated_at=excluded.updated_at
        """,
        (briefing_date, edition, status, error_count, now_utc()),
    )
    db.commit()
