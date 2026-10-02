"""Single-process SQLite state. The file lock covers an entire command/TUI session."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock, Timeout

from .domain import Assessment, Item, Period, Post, digest, utc_string
from .errors import AppError

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS posts (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, created_at TEXT NOT NULL,
 text TEXT NOT NULL, flags TEXT NOT NULL, revision TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS posts_date ON posts(created_at, id);
CREATE TABLE IF NOT EXISTS profiles (
 id TEXT PRIMARY KEY, config TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS assessments (
 post_id TEXT NOT NULL, revision TEXT NOT NULL, profile_id TEXT NOT NULL,
 payload TEXT, error_code TEXT, updated REAL NOT NULL,
 PRIMARY KEY(post_id, revision, profile_id));
CREATE TABLE IF NOT EXISTS decisions (
 post_id TEXT NOT NULL, revision TEXT NOT NULL, profile_id TEXT NOT NULL,
 value TEXT NOT NULL CHECK(value IN ('keep','defer','approve')), updated REAL NOT NULL,
 PRIMARY KEY(post_id, revision));
CREATE TABLE IF NOT EXISTS translations (
 cache_key TEXT PRIMARY KEY, text TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS deletion_state (
 post_id TEXT PRIMARY KEY, revision TEXT NOT NULL, profile_id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('in_flight','deleted','failed','unknown')),
 code TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY, post_id TEXT, action TEXT NOT NULL, code TEXT NOT NULL,
 created REAL NOT NULL);
"""


class Store:
    def __init__(self, path: Path, *, create: bool = False):
        path = path.expanduser().resolve()
        if not create and not path.is_file():
            raise AppError("database_not_found")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = FileLock(str(path) + ".lock", timeout=0)
        try:
            self.lock.acquire()
        except Timeout:
            raise AppError("database_in_use") from None
        try:
            self.conn = sqlite3.connect(path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.executescript(SCHEMA)
            version = self.get_meta("schema_version")
            if version not in {None, "1"}:
                raise AppError("unsupported_database_schema")
            self.set_meta("schema_version", "1")
            # No other process can own the DB lock; an in-flight row is from an interrupted run.
            with self.conn:
                self.conn.execute("UPDATE deletion_state SET state='unknown',code='interrupted' "
                                  "WHERE state='in_flight'")
        except BaseException:
            if hasattr(self, "conn"):
                self.conn.close()
            self.lock.release()
            raise

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()
        self.lock.release()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, value))

    @property
    def owner_id(self) -> str:
        value = self.get_meta("owner_id")
        if value is None:
            raise AppError("database_has_no_owner")
        return value

    def ingest(self, owner: str, posts: Iterable[Post]) -> int:
        existing = self.get_meta("owner_id")
        if existing is not None and existing != owner:
            raise AppError("database_owner_mismatch")
        count = 0
        # All-or-nothing import, including generator parse failures.
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('owner_id',?)", (owner,))
            for post in posts:
                if post.owner_id != owner:
                    raise AppError("post_owner_mismatch")
                self.conn.execute(
                    "INSERT INTO posts VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "owner_id=excluded.owner_id,created_at=excluded.created_at,text=excluded.text,"
                    "flags=excluded.flags,revision=excluded.revision",
                    (post.id, post.owner_id, post.created_at, post.text, json.dumps(post.flags), post.revision))
                count += 1
        return count

    @staticmethod
    def _post(row: sqlite3.Row) -> Post:
        return Post(row["id"], row["owner_id"], row["created_at"], row["text"], tuple(json.loads(row["flags"])))

    def post(self, post_id: str) -> Post:
        row = self.conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if row is None:
            raise AppError("post_not_in_database")
        return self._post(row)

    def posts(self, period: Period) -> Iterable[Post]:
        conditions, args = [], []
        if period.since is not None:
            conditions.append("created_at>=?")
            args.append(utc_string(period.since))
        if period.until is not None:
            conditions.append("created_at<?")
            args.append(utc_string(period.until))
        sql = "SELECT * FROM posts" + (" WHERE " + " AND ".join(conditions) if conditions else "")
        for row in self.conn.execute(sql + " ORDER BY created_at,id", args):
            yield self._post(row)

    def add_profile(self, config: dict[str, Any]) -> str:
        profile_id = digest(config)
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO profiles VALUES (?,?,?)",
                              (profile_id, json.dumps(config, ensure_ascii=False, sort_keys=True), time.time()))
        return profile_id

    def profiles(self) -> list[dict[str, Any]]:
        return [{"id": r["id"], "config": json.loads(r["config"])}
                for r in self.conn.execute("SELECT * FROM profiles ORDER BY created")]

    def resolve_profile(self, profile_id: str | None) -> str:
        profiles = self.profiles()
        if profile_id is None:
            if len(profiles) != 1:
                raise AppError("choose_explicit_profile_with_profiles_command")
            return profiles[0]["id"]
        matches = [r["id"] for r in profiles if r["id"].startswith(profile_id)]
        if len(matches) != 1:
            raise AppError("unknown_or_ambiguous_profile")
        return matches[0]

    def profile(self, profile_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT config FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if row is None:
            raise AppError("profile_not_found")
        return json.loads(row[0])

    def assessment(self, post: Post, profile: str) -> Assessment | None:
        row = self.conn.execute("SELECT payload FROM assessments WHERE post_id=? AND revision=? AND profile_id=?",
                                (post.id, post.revision, profile)).fetchone()
        if not row or row[0] is None:
            return None
        obj = json.loads(row[0])
        base = {k: obj.pop(k) for k in ("choice", "probabilities", "confidence")}
        return Assessment.validate(base, **obj)

    def save_assessment(self, post: Post, profile: str, assessment: Assessment | None,
                        error_code: str | None = None) -> None:
        payload = json.dumps(asdict(assessment), ensure_ascii=False, allow_nan=False) if assessment else None
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO assessments VALUES (?,?,?,?,?,?)",
                              (post.id, post.revision, profile, payload, error_code, time.time()))

    def decision(self, post: Post) -> str | None:
        row = self.conn.execute("SELECT value FROM decisions WHERE post_id=? AND revision=?",
                                (post.id, post.revision)).fetchone()
        return row[0] if row else None

    def decide(self, item: Item, value: str) -> None:
        if value not in {"keep", "defer", "approve"}:
            raise AppError("invalid_decision")
        if self.post(item.post.id).revision != item.post.revision:
            raise AppError("stale_review_item")
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?)",
                              (item.post.id, item.post.revision, item.profile_id, value, time.time()))
            self.conn.execute("INSERT INTO events(post_id,action,code,created) VALUES (?,?,?,?)",
                              (item.post.id, "decision", value, time.time()))

    def translation(self, key: str) -> str | None:
        row = self.conn.execute("SELECT text FROM translations WHERE cache_key=?", (key,)).fetchone()
        return row[0] if row else None

    def save_translation(self, key: str, text: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO translations VALUES (?,?,?)", (key, text, time.time()))

    def deletion(self, post_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM deletion_state WHERE post_id=?", (post_id,)).fetchone()
        return dict(row) if row else None

    def set_deletion(self, item: Item, state: str, code: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO deletion_state VALUES (?,?,?,?,?,?)",
                              (item.post.id, item.post.revision, item.profile_id, state, code, time.time()))
            self.conn.execute("INSERT INTO events(post_id,action,code,created) VALUES (?,?,?,?)",
                              (item.post.id, state, code, time.time()))

    def reset_present_deletion(self, post_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM deletion_state WHERE post_id=? AND state != 'deleted'", (post_id,))
            self.conn.execute("INSERT INTO events(post_id,action,code,created) VALUES (?,?,?,?)",
                              (post_id, "reconcile", "present_reset_for_manual_review", time.time()))

    def counts(self) -> dict[str, int]:
        result = {"posts": self.conn.execute("SELECT count(*) FROM posts").fetchone()[0]}
        for row in self.conn.execute("SELECT state,count(*) AS n FROM deletion_state GROUP BY state"):
            result[row["state"]] = row["n"]
        for row in self.conn.execute("SELECT d.value,count(*) AS n FROM decisions d JOIN posts p "
                                     "ON p.id=d.post_id AND p.revision=d.revision GROUP BY d.value"):
            result[row["value"]] = row["n"]
        return result

    def items(self, period: Period, profile: str, threshold: float, *, all_posts: bool = False,
              include_deferred: bool = False, include_approved: bool = False) -> list[Item]:
        result: list[Item] = []
        for post in self.posts(period):
            if post.is_repost or self.deletion(post.id) is not None:
                continue
            assessment = self.assessment(post, profile)
            if assessment is None:
                continue
            decision = self.decision(post)
            if decision == "keep" or decision == "defer" and not include_deferred:
                continue
            if decision == "approve" and not include_approved:
                continue
            if (all_posts or assessment.choice in {"DELETE_CANDIDATE", "NEEDS_CONTEXT"}
                    or assessment.delete_probability >= threshold or post.flags):
                result.append(Item(post, assessment, profile, decision))
        return result
