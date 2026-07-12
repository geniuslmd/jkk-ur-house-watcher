# JKK / UR House Watcher

[中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)

東京の JKK・UR 公開募集情報を確認する軽量な監視ツールです。GitHub Actions で定期実行し、物件の出現・再出現などを記録して、条件に合う情報だけを Slack に送信します。

JKK/UR へのログインや申込みの自動実行は行いません。

## 動作概要

- 毎日、日本時間 09:00 から 18:50 まで 10 分ごとに JKK と UR の公開ページを確認します。
- 日本時間 09:00 の確認後、Slack に短い稼働確認メッセージを 1 回送ります。
- それ以外の確認では、優先条件に合う物件が見つかった時だけ Slack 通知を送ります。
- 実行間で `new`、`stable`、`reappeared`、短時間で消えた物件を記録します。

GitHub Actions の定期実行は数分遅れる場合があるため、厳密なリアルタイム実行ではありません。

## 非公開の検索条件

公開リポジトリの設定ファイルには個人の希望物件名を含めません。自分の条件は GitHub Actions の Repository Secret `WATCH_RULES_JSON` に設定します。

```json
{
  "high_priority_ur_names": ["Your UR target"],
  "high_priority_jkk_names": ["Your JKK target A", "Your JKK target B"],
  "jkk_exclude_names": ["Optional excluded JKK building"],
  "slack_notifications": {
    "priority_min_layout_bedrooms": 2
  }
}
```

`priority_min_layout_bedrooms` が `2` の場合、`2LDK`、`2SLDK`、`3LDK`、`4LDK` などを通知し、`1LDK` は通知しません。

## GitHub Actions の設定

1. このコードを含むリポジトリを作成します。
2. `Settings -> Secrets and variables -> Actions -> New repository secret` を開きます。
3. `SLACK_WEBHOOK_URL` に Slack Incoming Webhook URL を設定します。
4. `WATCH_RULES_JSON` に自分専用の条件 JSON を設定します。
5. `Settings -> Actions -> General -> Workflow permissions` で `Read and write permissions` を選択します。
6. `Actions -> House Watcher -> Run workflow` を開きます。
   - まず `Only send a Slack test message` を選び、Slack 通知を確認します。
   - 次にチェックを外して 1 回実行し、実際の検索を確認します。

ワークフローは [`.github/workflows/house_watcher.yml`](.github/workflows/house_watcher.yml) にあります。

## ローカルで 1 回だけ実行する

このプロジェクトは Python 標準ライブラリのみを使用します。

```powershell
$env:SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/..."
$env:WATCH_RULES_JSON = '{"high_priority_ur_names":["Your UR target"],"high_priority_jkk_names":["Your JKK target"],"slack_notifications":{"priority_min_layout_bedrooms":2}}'
python scripts/run_watcher_once.py --dry-run-slack
```

`--test-slack` を指定すると、物件ページを取得せずに Slack のテストメッセージだけを送れます。

## プライバシー

`SLACK_WEBHOOK_URL` と `WATCH_RULES_JSON` はコミットしないでください。GitHub Actions Secret またはローカル環境変数にのみ保存します。

新規・再出現の判定を行うため、GitHub Actions は必要最小限の状態 JSON をコミットします。公開リポジトリの場合、この物件履歴も公開されます。スクリーンショット、元データ、ローカルログ、Secret は `.gitignore` により除外されます。

## 注意事項

- ページ構成やアクセス制限が変わった場合、取得処理の保守が必要になることがあります。
- UR の表示は参考情報です。行動する前に、公式ページ・電話・現地で最新情報を確認してください。
- 適切な更新間隔を守り、対象サイトの利用条件に従ってください。
