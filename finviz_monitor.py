"""
finviz_monitor.py
-----------------
Fetches the Finviz insider trading table, detects new BUY transactions,
and fires ClickUp notifications (task + chat message) for each new buy.

State is persisted in .state/seen.json and committed back to the repo
by the GitHub Actions workflow — no external storage needed.

Environment variables (set as GitHub secrets/vars):
  CLICKUP_API_TOKEN     – ClickUp personal API token          (secret)
  CLICKUP_WORKSPACE_ID  – "90182578127"                       (hardcoded in workflow)
  CLICKUP_LIST_ID       – "901818545610"                      (hardcoded in workflow)
  CLICKUP_CHANNEL_ID    – "2kzmtvyf-698"  chat channel        (hardcoded in workflow)
  FINVIZ_STATE_FILE     – path to seen.json, e.g. .state/seen.json
  MIN_VALUE_USD         – (optional) minimum transaction value to alert on
  MIN_SHARES            – (optional) minimum share count to alert on
"""

import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

FINVIZ_URL = "https://finviz.com/insidertrading?tc=1"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; insider-monitor/1.0)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

CLICKUP_API_TOKEN  = os.environ["CLICKUP_API_TOKEN"]
CLICKUP_LIST_ID    = os.environ.get("CLICKUP_LIST_ID", "901818545610")
CLICKUP_CHANNEL_ID = os.environ.get("CLICKUP_CHANNEL_ID", "2kzmtvyf-698")
STATE_FILE         = pathlib.Path(os.environ.get("FINVIZ_STATE_FILE", ".state/seen.json"))
MIN_VALUE_USD      = int(os.environ.get("MIN_VALUE_USD", "0") or "0")
MIN_SHARES         = int(os.environ.get("MIN_SHARES", "0") or "0")

CLICKUP_HEADERS = {
    "Authorization": CLICKUP_API_TOKEN,
    "Content-Type": "application/json",
}

MAX_STATE_ENTRIES = 500   # trim seen-set to this many fingerprints


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load the seen-set from disk. Returns {} on first run."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        print("⚠️  State file unreadable — resetting.")
        return {}


def save_state(state: dict) -> None:
    """Persist the seen-set, trimmed to MAX_STATE_ENTRIES."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if len(state) > MAX_STATE_ENTRIES:
        # Keep the most recent entries (dict preserves insertion order in 3.7+)
        trimmed = dict(list(state.items())[-MAX_STATE_ENTRIES:])
        state.clear()
        state.update(trimmed)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fingerprint(row: dict) -> str:
    """Stable unique key for a transaction row."""
    return f"{row['ticker']}|{row['owner']}|{row['date']}|{row['value']}"


# ── Finviz scraper ────────────────────────────────────────────────────────────

def fetch_page(url: str) -> BeautifulSoup | None:
    """Fetch a URL and return a BeautifulSoup object, or None on failure."""
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "html.parser")
            print(f"  HTTP {resp.status_code} on attempt {attempt + 1}")
        except requests.RequestException as exc:
            print(f"  Request error attempt {attempt + 1}: {exc}")
        time.sleep(3 * (attempt + 1))
    return None


def parse_table(soup: BeautifulSoup) -> list[dict]:
    """Extract insider trading rows from the page."""
    rows = []

    # Finviz renders the insider table with class 'insider-trading-table'
    # or as a table containing the 'Insider Trading' header text.
    table = soup.find("table", class_="insider-trading-table")
    if table is None:
        # Fallback: find by header content
        for t in soup.find_all("table"):
            header_text = t.get_text(" ")
            if "Ticker" in header_text and "Transaction" in header_text:
                table = t
                break

    if table is None:
        return rows

    trs = table.find_all("tr")
    for tr in trs:
        tds = tr.find_all("td")
        if len(tds) < 10:
            continue  # header or separator row

        ticker       = tds[0].get_text(strip=True)
        owner        = tds[1].get_text(strip=True)
        relationship = tds[2].get_text(strip=True)
        date         = tds[3].get_text(strip=True)
        transaction  = tds[4].get_text(strip=True)
        cost         = tds[5].get_text(strip=True)
        shares       = tds[6].get_text(strip=True)
        value        = tds[7].get_text(strip=True)
        shares_total = tds[8].get_text(strip=True)

        # SEC Form 4 link
        sec_link_tag = tds[9].find("a")
        sec_url  = sec_link_tag["href"] if sec_link_tag and sec_link_tag.get("href") else ""
        sec_text = sec_link_tag.get_text(strip=True) if sec_link_tag else tds[9].get_text(strip=True)

        if not ticker:
            continue

        rows.append({
            "ticker":       ticker,
            "owner":        owner,
            "relationship": relationship,
            "date":         date,
            "transaction":  transaction,
            "cost":         cost,
            "shares":       shares,
            "value":        value,
            "shares_total": shares_total,
            "sec_url":      sec_url,
            "sec_text":     sec_text,
        })

    return rows


def scrape_buys() -> list[dict]:
    """Fetch the first page of Finviz and return BUY rows only."""
    print(f"🔍 Fetching {FINVIZ_URL}")
    soup = fetch_page(FINVIZ_URL)
    if soup is None:
        print("❌ Failed to fetch Finviz page.")
        return []

    all_rows = parse_table(soup)
    print(f"   Parsed {len(all_rows)} total rows")

    buys = [r for r in all_rows if r["transaction"].strip().lower() == "buy"]
    print(f"   {len(buys)} BUY rows found")
    return buys


# ── Value/share filters ───────────────────────────────────────────────────────

def parse_numeric(val: str) -> float:
    """Parse a value like '$1,234,567' or '10,000' to float."""
    cleaned = val.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def passes_filters(row: dict) -> bool:
    if MIN_VALUE_USD > 0 and parse_numeric(row["value"]) < MIN_VALUE_USD:
        return False
    if MIN_SHARES > 0 and parse_numeric(row["shares"]) < MIN_SHARES:
        return False
    return True


# ── ClickUp notifications ─────────────────────────────────────────────────────

def post_clickup_task(row: dict) -> bool:
    """Create a ClickUp task in the insider alerts list."""
    sec_link = f"[{row['sec_text']}]({row['sec_url']})" if row["sec_url"] else row["sec_text"]
    now_utc  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    markdown_desc = f"""## 🟢 New Insider BUY Detected

| Field | Value |
|---|---|
| **Ticker** | {row['ticker']} |
| **Owner** | {row['owner']} |
| **Role** | {row['relationship']} |
| **Date** | {row['date']} |
| **Cost/Share** | {row['cost']} |
| **Shares** | {row['shares']} |
| **Total Value** | {row['value']} |
| **Shares Held After** | {row['shares_total']} |
| **SEC Form 4** | {sec_link} |

_Detected at {now_utc} by Finviz insider monitor_
"""

    payload = {
        "name":                 f"🟢 Insider Buy: {row['ticker']} — {row['owner']} ({row['relationship']})",
        "markdown_description": markdown_desc,
        "priority": 2 if parse_numeric(row["value"]) >= 800_000 else 3,  # high ≥$800k, normal otherwise -- # "priority": 2,
        "tags":                 ["insider-buy", "trading-signal"],
    }

    url  = f"https://api.clickup.com/api/v2/list/{CLICKUP_LIST_ID}/task"
    resp = requests.post(url, headers=CLICKUP_HEADERS, json=payload, timeout=15)

    if resp.status_code in (200, 201):
        task_id  = resp.json().get("id", "")
        task_url = resp.json().get("url", "")
        print(f"   ✅ Task created: {task_url or task_id}")
        return True
    else:
        print(f"   ❌ Task creation failed [{resp.status_code}]: {resp.text[:200]}")
        return False


def post_clickup_chat(row: dict) -> bool:
    """Post a chat message to the ClickUp channel."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    value_str  = row['value'] if row['value'] else "N/A"
    cost_str   = row['cost']  if row['cost']  else "N/A"
    sec_part   = f"\n🔗 SEC Form 4: {row['sec_url']}" if row['sec_url'] else ""

    message = (
        f"🟢 *Insider BUY Alert* — `{row['ticker']}`\n"
        f"👤 {row['owner']} ({row['relationship']})\n"
        f"📅 {row['date']}  |  💰 {value_str}  |  📈 {row['shares']} shares @ {cost_str}"
        f"{sec_part}\n"
        f"_Detected at {now_utc}_"
    )

    url     = f"https://api.clickup.com/api/v2/chat/channel/{CLICKUP_CHANNEL_ID}/message"
    payload = {"content": message, "content_type": "text/markdown"}
    resp    = requests.post(url, headers=CLICKUP_HEADERS, json=payload, timeout=15)

    if resp.status_code in (200, 201):
        print(f"   ✅ Chat message sent to channel {CLICKUP_CHANNEL_ID}")
        return True
    else:
        print(f"   ⚠️  Chat message failed [{resp.status_code}]: {resp.text[:200]}")
        return False   # non-fatal — task was already created


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n{'='*60}")
    print(f"  Finviz Insider Buy Monitor  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    # 1. Load state
    state = load_state()
    is_first_run = len(state) == 0
    if is_first_run:
        print("🆕 First run — initialising seen-set (no alerts sent).")

    # 2. Scrape
    buys = scrape_buys()
    if not buys:
        print("⚠️  No BUY rows found (possible scrape failure). Exiting.")
        sys.exit(0)

    # 3. Detect new rows
    new_rows = []
    for row in buys:
        fp = fingerprint(row)
        if fp not in state:
            new_rows.append((fp, row))

    print(f"\n📊 {len(buys)} buys on page  |  {len(new_rows)} new  |  {len(state)} already seen")

    # 4. On first run: just seed the state, no notifications
    if is_first_run:
        for fp, _ in new_rows:
            state[fp] = True
        save_state(state)
        print(f"✅ Seen-set initialised with {len(new_rows)} entries. Monitoring starts next run.")
        sys.exit(0)

    # 5. Send notifications for new rows
    alerted = 0
    for fp, row in new_rows:
        if not passes_filters(row):
            print(f"   ⏭  Skipped {row['ticker']} (below filter threshold)")
            state[fp] = True   # mark seen so we don't keep evaluating it
            continue

        print(f"\n🚨 New buy: {row['ticker']} by {row['owner']} — {row['value']}")
        task_ok = post_clickup_task(row)
        chat_ok = post_clickup_chat(row)

        if task_ok or chat_ok:
            alerted += 1

        state[fp] = True
        time.sleep(0.5)   # gentle rate limit between ClickUp calls

    # 6. Save updated state
    save_state(state)

    print(f"\n{'='*60}")
    print(f"  Done — {alerted} alert(s) sent  |  state size: {len(state)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
