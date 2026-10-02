# 公式資料と実装への対応

参照日：2026-10-02．以下は実装時に確認した公開資料です．
通信の実 API 検証を済ませた，という意味ではありません．
X アーカイブは固定された公開APIスキーマとみなしていません．対応範囲は別資料です．

## Jev / TypeSafe

- HTTP API： https://docs.typesafe.ai/api
- Choice の要求・応答： https://docs.typesafe.ai/primitives/choice
- モデルID・別名・入力制限： https://docs.typesafe.ai/models
- SDK の案内： https://docs.typesafe.ai/sdk

`providers/jev.py` は SDK の代わりに公開HTTP APIを直接呼びます．
`POST https://api.typesafe.ai/v1/systemone` に Bearer 認証を用い，
`model / state / questions` を送り，`answers.publication` を読みます．
Choice の `probabilities` と `confidence` は別のフィールドとして保存します．
`confidence` を削除候補の確率に置き換えません．固定モデルIDと応答モデルIDの一致も検査します．

## Azure Translator

- Translate v3： https://learn.microsoft.com/en-us/azure/ai-services/translator/text-translation/reference/v3/translate

`providers/azure.py` は Translator v3 の text/plain 翻訳を使います．
`to=en`，言語自動判定，`profanityAction=NoAction` とし，文面を勝手に削除するフィルタは指定しません．
本文は `[{"Text": "..."}]` の配列で送ります．鍵と必要なリージョンはヘッダーで渡します．
料金や無料枠の残量はこのアダプターで管理しません．

## X

- 認証方式の対応表： https://docs.x.com/fundamentals/authentication/guides/v2-authentication-mapping
- 自分のアカウント： https://docs.x.com/x-api/users/get-my-user
- 投稿の取得： https://docs.x.com/x-api/posts/get-post-by-id
- 投稿の削除： https://docs.x.com/x-api/posts/delete-post
- レート制限： https://docs.x.com/x-api/fundamentals/rate-limits

初版は OAuth 1.0a User Context を使い，OAuthLib で署名した GET と DELETE だけを送ります．
POSTでの新規投稿はしません．`GET /2/users/me` と `GET /2/tweets/{id}` が削除前の照合，
`DELETE /2/tweets/{id}` が実削除です．`data.deleted` がブール値trueの場合に成功とします．
取得した本文がアーカイブと違う場合，未確認の別の本文を削除しないよう拒否します．

公開レート表のDELETE制限は1ユーザー50件/15分です．それを基に既定18.1秒間隔とし，
`x-rate-limit-remaining / x-rate-limit-reset / Retry-After` が利用できる場合はそちらも考慮します．
他アプリからの利用やサーバー側条件により拒否される可能性は残り，429のDELETEは自動再送しません．

## OAuthLib / Textual

- OAuth 1 client： https://oauthlib.readthedocs.io/en/latest/oauth1/client.html
- Textual workers： https://textual.textualize.io/guide/workers/
- Textual tests： https://textual.textualize.io/guide/testing/
- Textual 配布情報： https://pypi.org/project/textual/

TUI はネットワーク操作を async worker に分離し，通常の表示更新と一投稿ずつのキー入力を扱います．
headless の `run_test` を使うテストも用意していますが，作成環境では未実行です．

## 実サービスを使う前の条件確認

- X 開発者規約： https://docs.x.com/developer-terms/policy
- X の利用制限： https://docs.x.com/developer-terms/restricted-use-cases
- TypeSafe のデータ処理条件： https://docs.typesafe.ai/legal

最後の3項目は利用者が適用条件を確認するための参照先です．
本プロジェクトは具体的な用途の規約適合を認定するものではありません．
第三者サービスへの本文送信を，ソースコードが実装できることだけで許可済みと判断しないでください．
