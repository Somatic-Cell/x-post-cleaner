from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .archive import open_archive
from .deletion import DeletePolicy, DeletionService, verify_live
from .demo import seed_demo
from .domain import Period, auto_eligible, valid_id
from .errors import AppError
from .pipeline import analyze
from .providers.azure import AzureTranslator
from .providers.http import http_client
from .providers.jev import DEFAULT_MODEL, JevClassifier, load_rules, profile_config
from .providers.x import OAuth1UserAuth, XClient
from .store import Store


def probability(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("0 以上 1 以下の有限数が必要です") from None
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise argparse.ArgumentTypeError("0 以上 1 以下の有限数が必要です")
    return result


def positive(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("正の整数が必要です") from None
    if result <= 0:
        raise argparse.ArgumentTypeError("正の整数が必要です")
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="自分のX投稿を公開方針に照らして確認する初期実装")
    root.add_argument("--env-file", type=Path, help="明示した環境変数ファイルのみ読み込む")
    sub = root.add_subparsers(dest="command", required=True)

    def common(name: str, help_text: str, *, period: bool = False) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--db", type=Path, default=Path(".local/review.sqlite3"))
        if period:
            p.add_argument("--since", help="開始日時を含む。省略時は下限なし")
            p.add_argument("--until", help="終了日時を含まない。省略時は上限なし")
            p.add_argument("--timezone", default="Asia/Tokyo")
        return p

    ingest = common("ingest", "アーカイブを読み込む。外部通信なし")
    ingest.add_argument("--archive", type=Path, required=True)
    ingest.add_argument("--owner-id", help="account.js がない場合の明示指定。認証との照合は削除前に行う")
    common("inspect", "期間内の件数・Unicode文字数を集計。外部通信なし", period=True)
    status = common("status", "作業状態の件数。外部通信なし")
    status.add_argument("--details", action="store_true", help="未解決の削除IDと状態を表示。本文は表示しない")
    common("profiles", "保存済みの分類設定ID一覧。外部通信なし")

    scan = common("analyze", "Jevで分類。外部送信への明示同意が必要", period=True)
    scan.add_argument("--rules", type=Path, default=Path("rules.toml"))
    scan.add_argument("--model", default=DEFAULT_MODEL)
    scan.add_argument("--input", choices=["original", "english", "both"], default="original")
    scan.add_argument("--translation-revision", default="azure-v3-auto-en-cache-v1")
    scan.add_argument("--max-input-chars", type=positive, default=16000,
                      help="原文/英訳合計の保守的な文字数ガード。超過は切り捨てず保留")
    scan.add_argument("--limit", type=positive, help="今回新たに分類する件数の上限")
    scan.add_argument("--allow-upload", action="store_true", help="必要な本文をJev/Azureへ送信することに同意")

    run = common("run", "dry-run / review / auto を実行", period=True)
    run.add_argument("--profile", help="分類設定ID。一つだけ保存済みなら省略可")
    run.add_argument("--mode", choices=["dry-run", "review", "auto"], default="dry-run")
    run.add_argument("--review-threshold", type=probability, default=0.70)
    run.add_argument("--auto-threshold", type=probability,
                     help="autoでは明示必須。個人データで検証した値を指定する")
    run.add_argument("--all", action="store_true", help="KEEP分類も確認候補に含める（既に残すと選択済みの投稿は除く）")
    run.add_argument("--include-deferred", action="store_true")
    run.add_argument("--include-approved", action="store_true")
    run.add_argument("--enable-delete", action="store_true")
    run.add_argument("--ack-irreversible", action="store_true")
    run.add_argument("--expected-user-id", help="自身の数値ユーザーID。実削除では必須")
    run.add_argument("--max-deletions", type=positive, default=20, help="今回のDELETE要求数の上限。失敗も数える")

    reconcile = common("reconcile", "結果不明/失敗の投稿を現在のXと再照合。DELETEは送信しない")
    reconcile.add_argument("--post-id", required=True)
    reconcile.add_argument("--expected-user-id", required=True)
    reconcile.add_argument("--reset-if-present", action="store_true",
                           help="同一投稿の存在を確認できた場合だけ、手動再確認に戻す")

    demo = common("demo", "合成投稿と仮スコアによるデモ。外部通信・削除なし")
    demo.set_defaults(db=Path(".local/demo.sqlite3"))
    demo.add_argument("--no-ui", action="store_true", help="デモデータ作成と集計のみ")
    return root


def make_x(client: Any) -> XClient:
    env = os.environ
    auth = OAuth1UserAuth(env.get("X_API_KEY", ""), env.get("X_API_SECRET", ""),
                          env.get("X_ACCESS_TOKEN", ""), env.get("X_ACCESS_TOKEN_SECRET", ""))
    return XClient(client, auth)


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def progress(counts: dict[str, int]) -> None:
    # No post IDs or text. Do not create a line for every post in non-interactive logs.
    if sys.stderr.isatty():
        sys.stderr.write(f"\r分類済み（今回）{counts['new']} / キャッシュ {counts['cached']} / エラー {counts['errors']}   ")
        sys.stderr.flush()


def live_notice(message: str) -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\r" + message.ljust(90))
        sys.stderr.flush()


async def launch_tui(store: Store, items: list[Any], zone: str,
                     deletion: DeletionService | None = None) -> None:
    try:
        from .tui import ReviewApp
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("textual"):
            raise AppError("tui_missing_install_project_with_tui_extra") from None
        raise
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise AppError("interactive_terminal_required")
    await ReviewApp(store, items, zone, deletion).run_async()


async def dispatch(args: argparse.Namespace, store: Store) -> None:
    command = args.command
    period = Period.parse(getattr(args, "since", None), getattr(args, "until", None),
                          getattr(args, "timezone", "Asia/Tokyo"))
    if command == "ingest":
        if store.get_meta("demo") == "true":
            raise AppError("cannot_import_real_archive_into_demo_database")
        archive = open_archive(args.archive, args.owner_id)
        try:
            count = store.ingest(archive.owner_id, archive.posts())
        finally:
            archive.close()
        emit({"imported_unique_posts": count, "owner_id": store.owner_id,
              "note": "archive snapshot only; reposts are retained for counting but not classified/deleted"})
    elif command == "inspect":
        counts = {"posts_in_period": 0, "unicode_characters": 0, "reposts_excluded": 0,
                  "context_flagged": 0, "over_16000_characters": 0}
        for post in store.posts(period):
            counts["posts_in_period"] += 1
            counts["unicode_characters"] += len(post.text)
            counts["reposts_excluded"] += int(post.is_repost)
            counts["context_flagged"] += int(bool(post.flags))
            counts["over_16000_characters"] += int(len(post.text) > 16000)
        emit(counts)
    elif command == "status":
        if args.details:
            rows = store.conn.execute("SELECT post_id,state,code FROM deletion_state WHERE state != 'deleted' ORDER BY updated")
            emit({"counts": store.counts(), "unresolved_deletions": [dict(row) for row in rows]})
        else:
            emit(store.counts())
    elif command == "profiles":
        emit([{ "id": row["id"], "provider": row["config"]["provider"],
                "model": row["config"].get("model"), "input": row["config"].get("input_mode")}
              for row in store.profiles()])
    elif command == "demo":
        profile = seed_demo(store)
        if not args.no_ui:
            items = store.items(period, profile, 0.7, all_posts=True)
            await launch_tui(store, items, period.timezone_name)
        emit({"demo": True, "profile": profile, "counts": store.counts()})
    elif command == "analyze":
        if not args.allow_upload:
            raise AppError("external_upload_requires_allow_upload_flag")
        if store.get_meta("demo") == "true":
            raise AppError("demo_database_must_stay_offline")
        config = profile_config(load_rules(args.rules), args.model, args.input, args.max_input_chars,
                                args.translation_revision)
        # Profile is stored before any API calls; inspect it with `profiles` even after failure.
        store.add_profile(config)
        async with http_client() as client:
            classifier = JevClassifier(client, os.environ.get("TYPESAFE_API_KEY", ""), config)
            translator = None
            if args.input != "original":
                translator = AzureTranslator(client, os.environ.get("AZURE_TRANSLATOR_KEY", ""),
                                             os.environ.get("AZURE_TRANSLATOR_REGION", ""),
                                             revision=args.translation_revision)
            try:
                profile, counts = await analyze(store, period, classifier, translator,
                                                 limit=args.limit, progress=progress)
            finally:
                if sys.stderr.isatty():
                    sys.stderr.write("\n")
            emit({"profile": profile, "counts": counts})
    elif command == "reconcile":
        if store.get_meta("demo") == "true":
            raise AppError("demo_database_must_stay_offline")
        expected = valid_id(args.expected_user_id)
        post_id = valid_id(args.post_id)
        if expected != store.owner_id:
            raise AppError("expected_owner_does_not_match_archive")
        state = store.deletion(post_id)
        if state is None or state["state"] == "deleted":
            raise AppError("no_unresolved_deletion_to_reconcile")
        async with http_client() as client:
            x = make_x(client)
            if await x.me() != expected:
                raise AppError("authenticated_x_account_mismatch")
            verify_live(store.post(post_id), await x.lookup(post_id))
        if args.reset_if_present:
            store.reset_present_deletion(post_id)
        emit({"post_id": post_id, "present_and_matches": True, "reset": args.reset_if_present})
    elif command == "run":
        if args.mode == "dry-run" and (args.enable_delete or args.ack_irreversible):
            raise AppError("dry_run_rejects_deletion_flags")
        if args.mode == "auto" and (not args.enable_delete or args.auto_threshold is None):
            raise AppError("auto_requires_enable_delete_and_explicit_threshold")
        if not args.enable_delete and args.ack_irreversible:
            raise AppError("acknowledgement_without_enable_delete")
        profile = store.resolve_profile(args.profile)
        if args.mode == "auto":
            # Automatic eligibility is independent of the manual review threshold.
            items = store.items(period, profile, 0.0, all_posts=True)
            items = [item for item in items if auto_eligible(item, args.auto_threshold)]
        else:
            items = store.items(period, profile, args.review_threshold, all_posts=args.all,
                                include_deferred=args.include_deferred,
                                include_approved=args.include_approved or args.enable_delete)
        if args.mode == "dry-run":
            emit({"mode": "dry-run", "review_queue": len(items), "profile": profile,
                  "database_counts": store.counts(), "external_requests": 0})
        elif not args.enable_delete:
            await launch_tui(store, items, period.timezone_name)
            emit(store.counts())
        else:
            policy = DeletePolicy(True, args.ack_irreversible, args.expected_user_id or "",
                                  max_attempts=args.max_deletions, auto_threshold=args.auto_threshold)
            policy.validate()
            async with http_client() as client:
                service = DeletionService(store, make_x(client), policy, notice=live_notice)
                # Verify identity even for an empty queue; this is an explicitly live command.
                await service.verify_account()
                if args.mode == "review":
                    await launch_tui(store, items, period.timezone_name, service)
                else:
                    for item in items:
                        if service.attempts >= policy.max_attempts:
                            break
                        await service.delete(item, automatic=True)
                if sys.stderr.isatty():
                    sys.stderr.write("\n")
                emit({"delete_requests_this_run": service.attempts, "counts": store.counts()})


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.env_file is not None:
            if not args.env_file.is_file():
                raise AppError("env_file_not_found")
            load_dotenv(args.env_file, override=False)
        with Store(args.db, create=args.command in {"ingest", "demo"}) as store:
            asyncio.run(dispatch(args, store))
        return 0
    except KeyboardInterrupt:
        print("中断しました。保存済みの状態から再開できます。", file=sys.stderr)
        return 130
    except AppError as exc:
        print("処理を停止しました：" + exc.code, file=sys.stderr)
        return 2
    except Exception as exc:
        # No str(exc), traceback, URL, request body or private local variables.
        print(f"処理を停止しました：unexpected_{type(exc).__name__}（機密情報を含む詳細は表示しません）",
              file=sys.stderr)
        return 3
