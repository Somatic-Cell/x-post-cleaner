# Post Review — v0.1.0

自分の X アーカイブを読み込み，Jev による**公開方針に沿った文面の分類**，
Textual による一投稿ずつの確認，承認後の X API による削除を行う Python プロジェクトです．
人物の健康状態を推定・診断するものではありません．

**共同レビュー用の初期実装です．実 API との接続，手元の実アーカイブへの適合，
日本語の分類精度，Windows 実端末での表示は未検証です．本番利用の完成品ではありません．**
Jev・Azure・X のアダプターは実際の HTTP リクエストを行う実装であり，
ダミーの成功応答に置き換えてはいません．デモだけが合成データ・仮の判定結果です．
検証結果は [TEST_REPORT.md](docs/TEST_REPORT.md) を参照してください．

## まず何を試すか

最初は **デモ → コードレビュー → 実アーカイブのオフライン集計 → 少数の分類 → 削除なしの確認**
という順序で進めてください．削除を有効にするのは，その後です．

前の設計案の `--mode dry-run / review / auto` は維持しつつ，
取込・分類・確認/削除を別コマンドに分けています．確認を再開するだけで
翻訳・分類 API を呼び直してしまうことを避けるためです．

## セットアップ

Python 3.11 以上．以下はリポジトリのルートで実行します．

### Windows / PowerShell

インストール済みの Python 3.11 以上を `py -3` で起動できる環境の例です．
仮想環境を activate しないので，PowerShell のスクリプト実行ポリシー変更は不要です．

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[tui,dev]"
.\.venv\Scripts\python.exe -m post_review demo
```

以後の例の `post-review` は，Windows では
`.\.venv\Scripts\python.exe -m post_review` に置き換えられます．

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[tui,dev]"
post-review demo
```

TUI を入れず，まず取込・API アダプター・テストを調べる場合は
`python -m pip install -e ".[dev]"` とし，デモには `--no-ui` を付けます．

## 1. 外部通信のないデモ

```bash
post-review demo
```

`.local/demo.sqlite3` に5件の合成投稿と仮の判定結果を保存します．
Jev・翻訳 API・X API への通信はなく，API キーは不要です．
デモ DB は実データ DB と区別され，実分類・実削除を拒否します．
繰り返し起動しても確認結果を引き継ぎます．最初からやり直す場合は，
別の `--db .local/demo2.sqlite3` を指定してください．

画面操作は `y`＋Enter＝削除を承認，`n` または空の Enter＝残す，
`s`＋Enter＝保留，`q`＋Enter＝中断です．マウスホイールや PageUp/PageDown で
本文をスクロールできます．通常の端末画面に本文を積み上げません．

**デモと，削除を有効化していない review モードの `y` は，承認の記録だけです．
実際には削除しません．** 次の未確認投稿を同じ領域に表示します．

```bash
post-review demo --no-ui
post-review status --db .local/demo.sqlite3
post-review run --db .local/demo.sqlite3 --mode dry-run
```

## 2. 実アーカイブの取込とオフライン集計

ダウンロードした X アーカイブのディレクトリ，または ZIP を指定します．
実アーカイブはリポジトリ外に置いてください．

```bash
post-review ingest --archive /private/x-archive --db .local/review.sqlite3
post-review inspect --db .local/review.sqlite3 --since 2025-01-01 --until 2026-01-01 --timezone Asia/Tokyo
```

この期間は日本時間の **2025年1月1日00:00以上，2026年1月1日00:00未満**です．
期間を省略すれば，取り込んだ投稿の全期間が対象です．日時にオフセットを明示することもできます．
DST の重複/欠落にあたるローカル時刻では，明示オフセットを要求します．

取込は全体を一つのトランザクションにし，不正なレコードや競合を検出した場合は
その回の変更をロールバックします．`account.js` から数値ユーザーIDを取得します．
同ファイルがない場合だけ，確認した ID を `--owner-id` で指定できます．
別のアカウントを同じ DB に混ぜることはできません．

**「アーカイブ内の対応形式をすべて読む」と「現在の X の全投稿に完全対応する」は別です．**
初版が扱うファイル名・形式と，停止するケースは [ARCHIVE_FORMAT.md](docs/ARCHIVE_FORMAT.md)
に記載しています．再投稿の取消し・編集履歴・別ファイル形式の長文投稿は未対応です．
`inspect` の文字数は Unicode 符号点の合計であり，サービスの正確な請求文字数ではありません．

## 3. Jev による分類

`rules.toml` は公開方針の**たたき台**です．まず内容を読み，消したい投稿の基準を調整してください．
単に否定的な表現があるだけでは削除候補にしない，という例を含めています．

`.env.example` を `.env` にコピーし，Jev の `TYPESAFE_API_KEY` を設定します．
実キーは GitHub やチャットへ貼り付けないでください．環境変数を直接設定しても構いません．
`.env` は明示的に `--env-file` を指定したときだけ読み込みます．

```bash
post-review --env-file .env analyze --db .local/review.sqlite3 --rules rules.toml --since 2025-01-01 --until 2026-01-01 --input original --limit 50 --allow-upload
```

`--allow-upload` がなければ外部送信を拒否します．`original` では翻訳を行わず，原文だけを
Jev に送信します．一投稿につき一リクエストです．投稿本文には人物名・私的情報が含まれる
可能性があります．内容を自動マスキングする機能はありません．アーカイブ中の DM は読みません．

分類は `DELETE_CANDIDATE / KEEP / NEEDS_CONTEXT` の三分類です．画面で百分率にするのは
`probabilities["DELETE_CANDIDATE"]` であり，Jev の `confidence` ではありません．
この値と実際の誤削除率との対応は，まだ検証していません．

分類基準・モデル・入力方式・文字数ガード・翻訳キャッシュ世代をハッシュ化し，
分類設定 ID（profile）を付けます．同じ投稿内容・設定なら分類結果を再利用します．
`jev-latest` などの移動する別名ではなく，既定では `jev-1.13.0` に固定しています．

```bash
post-review profiles --db .local/review.sqlite3
```

分類設定が一つだけなら，後続の `--profile` は省略できます．複数ある場合は，
表示された ID または一意な先頭部分を指定してください．

### 英訳を選択式で加える

`.env` に `AZURE_TRANSLATOR_KEY` と，リソースに応じて `AZURE_TRANSLATOR_REGION` を設定します．

```bash
post-review --env-file .env analyze --db .local/review.sqlite3 --input both --limit 50 --allow-upload
```

`original`＝原文のみ，`english`＝英訳のみ，`both`＝原文と英訳です．
原文は確認画面に常に表示します．訳文を再利用し，翻訳失敗時に原文へ黙って切り替えません．
翻訳 API のバックエンドの版は固定できないため，訳文とキャッシュ世代を保存します．
再翻訳したい場合は `--translation-revision` を変更します．
初版の翻訳プロバイダーは Azure のみです．無料枠の残量確認・課金上限の設定は実装していません．

`--max-input-chars` の既定値は16000です．これはアプリ独自の保守的な**文字数**上限で，翻訳前の原文と，
選択した入力方式でJevへ送る原文/訳文の合計の両方に適用します．
Jev のトークン上限を正確に計算したものではありません．超過時は本文を切り捨てず，
ローカル処理による `NEEDS_CONTEXT` として保存し，モデル未評価と表示します．
API が文脈長などの理由で拒否した場合も，分類済みとして扱いません．

## 4. 削除をしない確認

```bash
post-review run --db .local/review.sqlite3 --mode dry-run
post-review run --db .local/review.sqlite3 --mode review
```

`run` の既定は `dry-run` で，外部通信も削除も行いません．ただし，DB の作成済み状態を開き，
中断された削除意図を `unknown` に回復させる処理は行います．読み取り専用ファイル操作ではありません．
`review` も `--enable-delete` がなければ削除せず，選択だけを記録します．

`DELETE_CANDIDATE`，`NEEDS_CONTEXT`，メディア等の注意フラグ付き投稿は常に確認候補にします．
`--review-threshold` はそれ以外の分類でも候補に加えるための確率閾値です．
`--all` は未確認の KEEP 分類も含めます．既に「残す」と人間が決めた投稿は対象外です．

```bash
post-review run --db .local/review.sqlite3 --mode review --all
post-review run --db .local/review.sqlite3 --mode review --include-deferred
post-review run --db .local/review.sqlite3 --mode review --include-approved
```

同じ本文に対する「残す」は分類設定を変更しても尊重します．本文・日時・注意フラグが変わった場合は
別の revision とし，再評価・再確認を要求します．中断時にまとめてではなく，一操作ずつ保存します．

## 5. 実削除を有効にする場合

**実アカウントの削除テストは，この配布物の作成時には一切行っていません．**
先に [SAFETY.md](docs/SAFETY.md) と [REVIEW_GUIDE.md](docs/REVIEW_GUIDE.md) を確認してください．
X の自身の Read-and-Write アプリから発行された OAuth 1.0a ユーザー資格情報4点を使います．
ブラウザ OAuth 2.0 PKCE フローの実装は含みません．

```bash
# 数値IDは自分のものに置き換える。まずは上限1件。
post-review --env-file .env run --db .local/review.sqlite3 --mode review --enable-delete --ack-irreversible --expected-user-id YOUR_NUMERIC_USER_ID --max-deletions 1
```

このコマンドはアカウント照合のため X に通信します．画面に「実削除が有効」と表示された状態で
`y`＋Enter を入力すると，その一件を削除します．記録済みの承認を一括実行するのではなく，
承認済みの投稿も再提示して，改めて確認します．

実行直前に現在の投稿者と本文を取得し，アーカイブと一致するか検査します．
本文やIDが異なる投稿，別人の投稿，再投稿，編集履歴付き投稿では停止します．
この事前照会も X API の利用にあたります．

`auto` も実装していますが，個人データで分類基準と閾値を検証した後の段階です．
`--enable-delete --ack-irreversible --expected-user-id` に加え，
`--auto-threshold` の明示が必須で，その既定値は設けていません．
人間が「残す／保留／承認済み」と判断した投稿は自動削除しません．
返信・メディア・リンク・引用などの注意フラグがある投稿も自動削除しません．

## データの保存と除外

初版は，取り込んだ**原文・訳文・判定・操作状態を SQLite に保存**します．
アーカイブを移動しても確認を再開できる一方，本文のローカル複製が増える設計です．
DB は暗号化していません．操作履歴の events 表には本文を複製せず，通常のコンソール出力にも出しません．

`.gitignore` に `.local/`，`.env`，SQLite，アーカイブ ZIP 等の除外がありますが，
ファイル名を変えたデータや既に追跡しているファイルまでは保護できません．
コミット前に `git status` と `git diff --cached` を確認してください．
画面の消去は端末ログ・録画・バックアップからの消去を保証しません．

## テストと GitHub

```bash
python -m pytest
python -m pytest --cov=post_review --cov-report=term-missing
python -m compileall -q src tests
```

`.github/workflows/ci.yml` は Ubuntu / Windows × Python 3.11 / 3.13 で
TUI を含む依存関係をインストールしてオフラインテストを行う設定です．
API キーを GitHub Secrets に登録する必要はありません．
実際に GitHub 上で CI を実行した結果はまだありません．

新しい空のリポジトリを作成してから，このディレクトリで次を実行できます．
初回は非公開リポジトリで取り扱いを確認する運用が無難です．

```bash
git init -b main
git add .
git status
git diff --cached --stat
# ステージされたファイルに私的データ・秘密鍵がないことを確認する。
git commit -m "Initial reviewable post curation implementation"
git remote add origin YOUR_REPOSITORY_URL
git push -u origin main
```

ライセンスはまだ選択していません．公開配布する場合は，意図する利用条件に合わせて
リポジトリのライセンスを決めてください．

## レビュー資料

- [REVIEW_GUIDE.md](docs/REVIEW_GUIDE.md)：最初に読む順序とチェック項目
- [ARCHITECTURE.md](docs/ARCHITECTURE.md)：責務分割・データフロー・状態遷移
- [SAFETY.md](docs/SAFETY.md)：削除の保護条件・中断/障害からの復帰
- [ARCHIVE_FORMAT.md](docs/ARCHIVE_FORMAT.md)：対応形式と対象外
- [API_REFERENCES.md](docs/API_REFERENCES.md)：公式 API 資料と実装への対応
- [TEST_REPORT.md](docs/TEST_REPORT.md)：実行した検証と未検証事項
