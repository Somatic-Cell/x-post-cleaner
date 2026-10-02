"""One-post-at-a-time Textual UI; no raw post text is printed to the normal terminal."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Input, Static

from .deletion import DeletionService
from .domain import Item, display_text, parse_timestamp
from .errors import AppError
from .store import Store


class ReviewApp(App[None]):
    TITLE = "Post Review"
    BINDINGS = [("ctrl+q", "request_quit", "中断"), ("pageup", "scroll_up", "上へ"),
                ("pagedown", "scroll_down", "下へ")]
    CSS = """
    Screen { layout: vertical; }
    #status { height: auto; padding: 0 1; border-bottom: solid $primary; }
    #body-scroll { height: 1fr; }
    #post-body { height: auto; padding: 1 2; }
    #notice { height: auto; min-height: 2; padding: 0 1; }
    #answer { margin: 0 1; }
    """

    def __init__(self, store: Store, items: list[Item], timezone_name: str = "Asia/Tokyo",
                 deletion: DeletionService | None = None):
        super().__init__()
        self.store, self.items, self.zone, self.deletion = store, items, ZoneInfo(timezone_name), deletion
        self.position = 0
        self.busy = False
        self.session_completed = 0
        if deletion is not None:
            deletion.notice = self.set_notice

    def compose(self) -> ComposeResult:
        yield Static("", id="status", markup=False)
        with VerticalScroll(id="body-scroll"):
            yield Static("", id="post-body", markup=False)
        yield Static("", id="notice", markup=False)
        yield Input(placeholder="y＋Enter：削除を承認 / n または空 Enter：残す / s：保留 / q：中断", id="answer")
        yield Footer()

    def on_mount(self) -> None:
        self.show_current()
        self.query_one("#answer", Input).focus()

    def set_notice(self, message: str) -> None:
        self.query_one("#notice", Static).update(display_text(message))

    def show_current(self) -> None:
        mode = "実削除が有効：y＋Enterで不可逆な削除" if self.deletion else "記録のみ：Xへの削除要求なし"
        if self.store.get_meta("demo") == "true":
            mode = "DEMO：合成データ・仮のスコア・削除なし"
        counts = self.store.counts()
        status = (f"{mode}\n確認済み {self.session_completed} / 今回の候補 {len(self.items)}　"
                  f"削除成功 {counts.get('deleted', 0)}　残す {counts.get('keep', 0)}　"
                  f"保留 {counts.get('defer', 0)}　結果不明 {counts.get('unknown', 0)}")
        self.query_one("#status", Static).update(status)
        if self.position >= len(self.items):
            self.query_one("#post-body", Static).update("今回の確認は終了しました。q＋Enter または Ctrl+Q で終了します。")
            self.set_notice("選択は保存済みです。保留は --include-deferred、承認済みは --include-approved で再表示できます。")
            return
        item = self.items[self.position]
        post, a = item.post, item.assessment
        date = parse_timestamp(post.created_at).astimezone(self.zone).strftime("%Y年%m月%d日 %H:%M:%S %Z")
        probability = f"削除候補のモデル推定確率：{a.delete_probability:.1%}"
        if a.origin == "local_guard":
            probability = "モデル推定確率：未計算（ローカルの入力制限による保留）"
        elif a.origin == "demo":
            probability = f"デモ用の仮スコア：{a.delete_probability:.1%}（推論結果ではありません）"
        body = (f"{self.position+1} / {len(self.items)}\n投稿日時：{date}\n投稿 ID：{post.id}\n"
                f"分類：{a.choice}\n{probability}\n"
                f"注意：{', '.join(post.flags) or 'なし（文脈が完全である保証ではありません）'}\n\n"
                f"原文：\n{display_text(post.text)}")
        if a.translation is not None:
            body += "\n\n英訳（補助表示）：\n" + display_text(a.translation)
        self.query_one("#post-body", Static).update(body)
        self.query_one("#body-scroll", VerticalScroll).scroll_home(animate=False)

    def action_scroll_up(self) -> None:
        self.query_one("#body-scroll", VerticalScroll).scroll_page_up()

    def action_scroll_down(self) -> None:
        self.query_one("#body-scroll", VerticalScroll).scroll_page_down()

    def action_request_quit(self) -> None:
        if self.busy:
            self.set_notice("通信処理中です。強制終了した場合は、次回起動時に結果不明として保護されます。")
        else:
            self.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        answer = event.value.strip().lower()
        event.input.value = ""  # Never retain typed commands or post text in an input history.
        if self.busy:
            return
        if answer == "q":
            self.action_request_quit()
            return
        if self.position >= len(self.items):
            return
        if answer not in {"", "y", "n", "s"}:
            self.set_notice("y / n / s / q のいずれかを入力してください。")
            return
        item = self.items[self.position]
        value = {"": "keep", "n": "keep", "s": "defer", "y": "approve"}[answer]
        try:
            self.store.decide(item, value)
        except AppError as exc:
            self.set_notice("保存を中止しました：" + exc.code)
            return
        except Exception:
            self.set_notice("状態の保存に失敗しました。次の投稿へは進めません。DBの空き容量等を確認してください。")
            return
        if value == "approve" and self.deletion is not None:
            self.busy = True
            self.query_one("#answer", Input).disabled = True
            self.perform_delete(item)
            return
        self.position += 1
        self.session_completed += 1
        self.show_current()
        self.set_notice({"keep": "残す、と記録しました。", "defer": "保留しました。",
                         "approve": "削除の承認を記録しました。まだ削除していません。"}[value])

    @work(exclusive=True)
    async def perform_delete(self, item: Item) -> None:
        try:
            assert self.deletion is not None
            await self.deletion.delete(item)
        except AppError as exc:
            self.show_current()
            self.set_notice("削除未完了：" + exc.code + "。詳細は docs/SAFETY.md を参照してください。")
        except Exception:
            # Do not print a traceback containing sensitive local variables in normal operation.
            self.show_current()
            self.set_notice("予期しないエラーで停止しました。結果を確認するまで削除を再送しないでください。")
        else:
            self.position += 1
            self.session_completed += 1
            self.show_current()
            self.set_notice("削除成功。次の投稿を表示しています。")
        finally:
            self.busy = False
            field = self.query_one("#answer", Input)
            field.disabled = False
            field.focus()
