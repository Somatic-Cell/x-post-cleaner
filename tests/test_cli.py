import json

import pytest

from post_review.cli import main, parser
from post_review.demo import seed_demo
from post_review.store import Store


def test_cli_defaults_to_dry_run():
    assert parser().parse_args(["run"]).mode == "dry-run"


def demo_db(tmp_path):
    db = tmp_path / "demo.sqlite3"
    with Store(db, create=True) as store:
        seed_demo(store)
    return db


def test_offline_demo_and_dry_run(tmp_path, capsys):
    db = tmp_path / "demo.sqlite3"
    assert main(["demo", "--db", str(db), "--no-ui"]) == 0
    assert "合成" not in capsys.readouterr().out  # Body text is never printed.
    assert main(["run", "--db", str(db)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry-run" and out["external_requests"] == 0


def test_inspect_does_not_print_post_text(tmp_path, capsys):
    db = demo_db(tmp_path)
    assert main(["inspect", "--db", str(db), "--since", "2025-01-01", "--until", "2026-01-01"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["posts_in_period"] == 5
    assert "手元の日記" not in out


def test_upload_requires_explicit_permission(tmp_path, capsys):
    db = demo_db(tmp_path)
    assert main(["analyze", "--db", str(db)]) == 2
    assert "allow_upload" in capsys.readouterr().err


def test_demo_is_blocked_from_real_analysis(tmp_path, capsys):
    db = demo_db(tmp_path)
    assert main(["analyze", "--db", str(db), "--allow-upload"]) == 2
    assert "demo_database_must_stay_offline" in capsys.readouterr().err


def test_auto_without_enable_is_blocked(tmp_path, capsys):
    db = demo_db(tmp_path)
    assert main(["run", "--db", str(db), "--mode", "auto", "--auto-threshold", "0.98"]) == 2
    assert "auto_requires" in capsys.readouterr().err


def test_deletion_flags_are_rejected_in_dry_run(tmp_path, capsys):
    db = demo_db(tmp_path)
    assert main(["run", "--db", str(db), "--enable-delete"]) == 2
    assert "dry_run_rejects" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["nan", "inf", "-0.1", "1.1"])
def test_nonfinite_or_out_of_range_threshold_rejected(value):
    with pytest.raises(SystemExit):
        parser().parse_args(["run", "--auto-threshold", value])


def test_missing_db_is_not_silently_created(tmp_path, capsys):
    db = tmp_path / "missing.sqlite3"
    assert main(["status", "--db", str(db)]) == 2
    assert not db.exists()
