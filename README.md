# JKK / UR House Watcher

一个用于监测东京 JKK 与 UR 房源的轻量工具。它会定时查询公开网页，记录房源的出现与消失，并只把值得人工确认的结果发送到 Slack。

这个仓库的云端模式是“定时查询 + 状态保存 + Slack 通知”，不会登录 JKK/UR，也不会自动申请房源。

## Notification rules

公开的 [`config/watch_rules.json`](config/watch_rules.json) 不包含个人目标。实际团地、排除项与通知户型通过运行环境变量 `WATCH_RULES_JSON` 传入；GitHub Actions 请将它保存为同名 Actions Secret。

例如将最小户型设为 `2` 时，`2LDK`、`2SLDK`、`3LDK`、`4LDK` 会通知，`1LDK` 不会通知。其他抓到的房源仍会记录到状态 JSON，只是不推送 Slack。

通知不会反复轰炸：同一房源默认 30 分钟内最多一次；状态变化（例如 `new -> stable` 或再次出现）可再次通知。

## GitHub Actions setup

1. 在 GitHub 新建一个空仓库，然后把本目录的代码推送到你的个人账号仓库。
2. 在仓库页面打开 `Settings -> Secrets and variables -> Actions -> New repository secret`。
3. 新建名称为 `SLACK_WEBHOOK_URL` 的 secret，值填 Slack Incoming Webhook URL。
4. 新建名称为 `WATCH_RULES_JSON` 的 secret，值为你的私人监控规则，例如：

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

5. `WATCH_RULES_JSON` 会覆盖公开默认值，但不会出现在仓库、Actions 日志或 Slack 中。
6. 在 `Settings -> Actions -> General -> Workflow permissions` 选择 `Read and write permissions`，让 Actions 能把状态 JSON 提交回仓库。
7. 打开 `Actions -> House Watcher -> Run workflow` 手动运行一次。先勾选 `test_slack` 验证 Slack；再不勾选运行一次真实查询。

工作流文件在 [`.github/workflows/house_watcher.yml`](.github/workflows/house_watcher.yml)。默认计划为工作日日本时间 09:00--18:50 每 10 分钟运行一次。GitHub 的定时任务可能延迟几分钟，不能当作精确计时器。

要改成每 5 分钟，把 workflow 内的：

```yaml
- cron: "*/10 0-9 * * 1-5"
```

改为：

```yaml
- cron: "*/5 0-9 * * 1-5"
```

## Configuration

公开默认规则在 [`config/watch_rules.json`](config/watch_rules.json)。私人目标请通过 `WATCH_RULES_JSON` 传入，不要提交进仓库。重点字段：

```json
{
  "high_priority_ur_names": ["Your UR target"],
  "high_priority_jkk_names": ["Your JKK target"],
  "slack_notifications": {
    "priority_min_layout_bedrooms": 2,
    "cooldown_minutes": 30
  }
}
```

把 `priority_min_layout_bedrooms` 改成 `1` 可通知 1LDK 及以上；改成 `3` 则只通知 3LDK 及以上。`allowed_layouts` 可选地用于进一步按文字过滤，例如 `["2LDK", "3LDK"]`。

## Local one-time run

项目只依赖 Python 标准库。PowerShell 中：

```powershell
$env:SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/..."
$env:WATCH_RULES_JSON = '{"high_priority_ur_names":["Your UR target"],"high_priority_jkk_names":["Your JKK target"],"slack_notifications":{"priority_min_layout_bedrooms":2}}'
python scripts/run_watcher_once.py --dry-run-slack
```

`--dry-run-slack` 只打印将发送的内容，不会发送。确认后去掉该参数即可实际发送。

只测试 Slack 连通性：

```powershell
python scripts/run_watcher_once.py --test-slack
```

## Data and privacy

本机运行产生的 `data/*.json`、截图/证据、日志以及 dashboard 状态都被 `.gitignore` 排除，正常 `git add .` 不会上传。请勿把 Slack Webhook URL 或 `WATCH_RULES_JSON` 写进代码或提交到仓库。

不过，为了让 GitHub Actions 在不同运行之间识别 `new` / `reappeared`，工作流会**有意提交**少量状态文件：`data/listings.json`、`data/listing_events.json`、`data/lifecycle_meta.json`、`data/last_notifications.json`、`data/last_run_summary.json` 和 `watch_stats.json`。如果仓库是 public，这些房源历史也会公开；希望保留历史但不公开时，请使用 private repo。截图、原始证据和 Slack 密钥不会被提交。

## Notes

- JKK/UR 页面结构或访问限制变化时，抓取可能需要维护。
- UR 结果是参考信息；需要行动时请以电话、线下或官方页面的最新信息为准。
- 请以合理频率使用，遵守目标网站的使用规则。
