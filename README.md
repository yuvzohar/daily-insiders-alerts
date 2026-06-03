# daily-insiders-alerts

Monitors [Finviz insider trading](https://finviz.com/insidertrading?tc=1) every 5 minutes via GitHub Actions and sends real-time ClickUp alerts for new executive BUY transactions.

## How it works

1. GitHub Actions cron triggers `finviz_monitor.py` every 5 minutes (Mon–Sun, 24/7)
2. Script fetches the latest insider trading page from Finviz
3. Filters to `Buy` transactions only
4. Compares against `.state/seen.json` (committed in the repo) to find new rows
5. For each new buy: creates a **ClickUp task** + posts a **ClickUp chat message**
6. Commits the updated state file back to the repo

## Repository structure

```
daily-insiders-alerts/
├── .github/
│   └── workflows/
│       └── finviz-insider-monitor.yml   # GitHub Actions cron workflow
├── .state/
│   └── seen.json                        # Dedupe state (auto-updated by Actions)
├── finviz_monitor.py                    # Main monitor script
├── requirements.txt
└── README.md
```

## Setup

### 1. Add GitHub Secret

In your repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|---|---|
| `CLICKUP_API_TOKEN` | Your ClickUp personal API token |

### 2. Optional repo variables (filters)

In **Settings → Secrets and variables → Actions → Variables**:

| Variable | Example | Description |
|---|---|---|
| `MIN_VALUE_USD` | `50000` | Skip buys with total value below this (USD) |
| `MIN_SHARES` | `1000` | Skip buys with share count below this |

Leave blank to receive all buys regardless of size.

### 3. Push and enable Actions

```bash
git add .
git commit -m "feat: initial Finviz insider buy monitor"
git push
```

Then go to **Actions** tab → enable workflows if prompted.

### 4. First run behaviour

The first run **does not send any alerts** — it only seeds the seen-set with whatever is currently on the page. Alerts begin from the second run onwards, so you won't get a flood of historical notifications.

## ClickUp destinations

| Destination | ID | Purpose |
|---|---|---|
| Task list | `901818545610` (`trading-insiders-realtime-alerts`) | One task per new buy |
| Chat channel | `2kzmtvyf-698` | Instant chat message per new buy |

## Alert format

**Task name:** `🟢 Insider Buy: AAPL — Tim Cook (CEO)`

**Chat message:**
```
🟢 Insider BUY Alert — AAPL
👤 Tim Cook (CEO)
📅 Jun 03 '26  |  💰 $1,234,567  |  📈 10,000 shares @ $123.45
🔗 SEC Form 4: https://...
```
