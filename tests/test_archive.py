import json
import zipfile

import pytest

from post_review.archive import decode_array, open_archive, parse_post
from post_review.errors import AppError


def tweet(post_id="123", text="本文"):
    return {"tweet": {"id_str": post_id, "full_text": text, "created_at": "Sat Jun 14 14:18:00 +0000 2025"}}


def make_archive(path, entries=None):
    path.mkdir()
    (path / "account.js").write_text('window.YTD.account.part0 = [{"account":{"accountId":"12345"}}];', encoding="utf-8")
    for name, rows in (entries or {"tweets.js": [tweet()]}).items():
        (path / name).write_text("window.YTD.tweets.part0 = " + json.dumps(rows, ensure_ascii=False) + ";", encoding="utf-8")
    return path


def test_js_wrapper_is_json_not_executable():
    assert decode_array(b'window.YTD.tweets.part0 = [{"tweet":{}}];') == [{"tweet": {}}]
    with pytest.raises(AppError):
        decode_array(b'window.YTD.tweets.part0 = []; alert("execute me");')
    with pytest.raises(AppError):
        decode_array(b'__import__("os").system("something")')


def test_all_parts_read_and_deduplicated(tmp_path):
    root = make_archive(tmp_path / "a", {"tweets.js": [tweet("1")],
                                         "tweets-part1.js": [tweet("2"), tweet("1")]})
    archive = open_archive(root)
    try:
        assert {p.id for p in archive.posts()} == {"1", "2"}
    finally:
        archive.close()


def test_conflicting_duplicate_rejected(tmp_path):
    root = make_archive(tmp_path / "a", {"tweets.js": [tweet("1"), tweet("1", "changed")]})
    archive = open_archive(root)
    with pytest.raises(AppError, match="conflicting_duplicate"):
        list(archive.posts())
    archive.close()


def test_zip_is_read_without_extracting(tmp_path):
    source = make_archive(tmp_path / "a")
    target = tmp_path / "archive.zip"
    with zipfile.ZipFile(target, "w") as zf:
        for f in source.iterdir():
            zf.write(f, "some-root/data/" + f.name)
        zf.writestr("some-root/data/direct-messages.js", "NOT JSON; DO NOT READ")
        zf.writestr("../../unrelated.txt", "DO NOT EXTRACT")
    archive = open_archive(target)
    assert len(list(archive.posts())) == 1
    archive.close()
    assert not (tmp_path / "unrelated.txt").exists()


def test_missing_account_requires_explicit_owner(tmp_path):
    root = make_archive(tmp_path / "a")
    (root / "account.js").unlink()
    with pytest.raises(AppError, match="account_js_missing"):
        open_archive(root)
    archive = open_archive(root, "12345")
    assert archive.owner_id == "12345"
    archive.close()


def test_owner_override_cannot_replace_archive_account(tmp_path):
    root = make_archive(tmp_path / "a")
    with pytest.raises(AppError, match="override_mismatch"):
        open_archive(root, "99999")


def test_separate_long_post_file_fails_loudly(tmp_path):
    root = make_archive(tmp_path / "a")
    (root / "note-tweet.js").write_text("not silently ignored")
    with pytest.raises(AppError, match="separate_note_tweet"):
        open_archive(root)


def test_archive_decodes_entities_and_marks_replies():
    row = tweet(text="a &amp; b\r\nc")
    row["tweet"]["in_reply_to_status_id_str"] = "999"
    post = parse_post(row, "12345")
    assert post.text == "a & b\nc"
    assert "reply_context_missing" in post.flags


def test_reposts_and_media_flagged():
    row = tweet(text="RT @example: 原投稿")
    row["tweet"]["entities"] = {"media": [{"id_str": "1"}]}
    post = parse_post(row, "12345")
    assert post.is_repost and "media_not_analyzed" in post.flags


def test_full_text_not_truncated():
    body = "長" * 25000
    assert parse_post(tweet(text=body), "12345").text == body
