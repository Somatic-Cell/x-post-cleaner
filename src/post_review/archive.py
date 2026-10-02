"""Read a narrow, documented X archive subset. Never evaluate JavaScript or extract ZIPs."""
from __future__ import annotations

import html
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from .domain import Post, canonical_text, parse_timestamp, utc_string, valid_id
from .errors import AppError

POST_NAME = re.compile(r"tweets?(?:-part\d+)?\.(?:js|json)$", re.I)
NOTE_NAME = re.compile(r"note[-_]tweets?(?:-part\d+)?\.(?:js|json)$", re.I)
PREFIX = re.compile(r"\A\s*(?:window\.)?YTD\.(?:tweets?|account)\.part\d+\s*=\s*")
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024


def decode_array(data: bytes) -> list[dict[str, Any]]:
    try:
        value = data.decode("utf-8-sig").strip()
        if not value.startswith("["):
            match = PREFIX.match(value)
            if not match:
                raise ValueError
            value = value[match.end():]
        if value.endswith(";"):
            value = value[:-1].rstrip()
        result = json.loads(value)
        if not isinstance(result, list) or any(not isinstance(r, dict) for r in result):
            raise ValueError
        return result
    except (ValueError, UnicodeError):
        raise AppError("unsupported_archive_json") from None


def parse_post(row: dict[str, Any], owner: str) -> Post:
    tweet = row.get("tweet", row)
    if not isinstance(tweet, dict):
        raise AppError("invalid_tweet_record")
    post_id = valid_id(tweet.get("id_str", tweet.get("id")))
    if tweet.get("author_id") is not None and valid_id(tweet["author_id"]) != owner:
        raise AppError("archive_author_mismatch")
    text = tweet.get("full_text", tweet.get("text"))
    if not isinstance(text, str):
        raise AppError("missing_post_text")
    flags: set[str] = set()
    # Archive legacy text is HTML-escaped. Do not apply this to live v2 text.
    text = canonical_text(html.unescape(text))
    if not text:
        flags.add("empty_text")
    if tweet.get("truncated") is True or "full_text" not in tweet:
        flags.add("possibly_incomplete_text")
    if tweet.get("in_reply_to_status_id_str") or tweet.get("in_reply_to_status_id"):
        flags.add("reply_context_missing")
    if tweet.get("is_quote_status") or tweet.get("quoted_status_id") or tweet.get("quoted_status_id_str"):
        flags.add("quote_context_missing")
    entities = tweet.get("entities") or {}
    extended = tweet.get("extended_entities") or {}
    if not isinstance(entities, dict) or not isinstance(extended, dict):
        raise AppError("invalid_post_entities")
    if entities.get("media") or extended.get("media"):
        flags.add("media_not_analyzed")
    if entities.get("urls") or re.search(r"https?://", text):
        flags.add("linked_content_not_analyzed")
    if text.startswith("RT @") or tweet.get("retweeted_status"):
        flags.add("repost_or_rt_prefix")
    if tweet.get("note_tweet") or tweet.get("note_tweet_id"):
        flags.add("long_post_needs_verification")
    history = tweet.get("edit_history_tweet_ids", [])
    if history and (not isinstance(history, list) or len(history) > 1):
        flags.add("edited_post")
    created = tweet.get("created_at")
    if not isinstance(created, str):
        raise AppError("missing_post_timestamp")
    return Post(post_id, owner, utc_string(parse_timestamp(created)), text, tuple(sorted(flags)))


@dataclass
class Archive:
    owner_id: str
    entries: list[tuple[str, Callable[[], bytes]]]
    close: Callable[[], None]

    def posts(self) -> Iterator[Post]:
        seen: dict[str, str] = {}
        for _, load in self.entries:
            for row in decode_array(load()):
                post = parse_post(row, self.owner_id)
                if post.id in seen:
                    if seen[post.id] != post.revision:
                        raise AppError("conflicting_duplicate_post")
                    continue
                seen[post.id] = post.revision
                yield post


def open_archive(path: Path, owner_override: str | None = None) -> Archive:
    resources: list[tuple[str, int, Callable[[], bytes]]] = []
    close: Callable[[], None] = lambda: None
    if path.is_dir():
        for child in path.rglob("*"):
            if not child.is_file():
                continue
            name = child.name
            if not (POST_NAME.fullmatch(name) or name == "account.js" or NOTE_NAME.fullmatch(name)):
                continue
            if child.is_symlink() or not child.resolve().is_relative_to(path.resolve()):
                raise AppError("archive_symlink_rejected")
            resources.append((str(child.relative_to(path).as_posix()), child.stat().st_size, child.read_bytes))
    elif path.is_file() and zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        close = zf.close
        if len(zf.infolist()) > 100000:
            close()
            raise AppError("archive_too_many_members")
        for info in zf.infolist():
            name = PurePosixPath(info.filename).name
            if POST_NAME.fullmatch(name) or name == "account.js" or NOTE_NAME.fullmatch(name):
                resources.append((info.filename, info.file_size, lambda i=info: zf.read(i)))
    else:
        raise AppError("archive_must_be_directory_or_zip")
    try:
        if any(NOTE_NAME.fullmatch(PurePosixPath(n).name) for n, _, _ in resources):
            # Failing here is intentional: do not claim full coverage while omitting long-post data.
            raise AppError("separate_note_tweet_files_not_supported_see_docs")
        if any(s > MAX_FILE for _, s, _ in resources) or sum(s for _, s, _ in resources) > MAX_TOTAL:
            raise AppError("archive_size_limit_see_docs")
        post_files = [(n, load) for n, _, load in resources if POST_NAME.fullmatch(PurePosixPath(n).name)]
        if not post_files:
            raise AppError("no_supported_tweet_files")
        parents = {str(PurePosixPath(n).parent) for n, _ in post_files}
        if len(parents) != 1:
            raise AppError("multiple_archive_roots")
        account_files = [(n, load) for n, _, load in resources if PurePosixPath(n).name == "account.js"]
        owner: str | None = None
        if account_files:
            if len(account_files) != 1 or str(PurePosixPath(account_files[0][0]).parent) not in parents:
                raise AppError("ambiguous_archive_account")
            accounts = decode_array(account_files[0][1]())
            if len(accounts) != 1 or not isinstance(accounts[0].get("account"), dict):
                raise AppError("invalid_archive_account")
            owner = valid_id(accounts[0]["account"].get("accountId"))
        supplied = valid_id(owner_override) if owner_override else None
        if owner and supplied and owner != supplied:
            raise AppError("archive_owner_override_mismatch")
        owner = owner or supplied
        if owner is None:
            raise AppError("account_js_missing_supply_owner_id")
        return Archive(owner, sorted(post_files, key=lambda p: p[0]), close)
    except BaseException:
        close()
        raise
