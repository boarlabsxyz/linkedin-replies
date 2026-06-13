"""Daily pipeline.

1. Open Sales Navigator lead-alerts feed with a saved LinkedIn session.
2. Find new "lead posted on LinkedIn" alerts (skip job-change / news /
   list-save alerts) we haven't seen before.
3. For each, ask `claude` to run the linkedin-comment-ideas skill and
   produce exactly 3 variants in JSON.
4. Post the lead's post + 3 variants into Slack as a thread.

Designed to be invoked by launchd on a Mac Studio with a pre-authenticated
LinkedIn session in storage_state.json.

Usage:
    python daily_run.py            # full run, posts to Slack
    python daily_run.py --dry-run  # skip Slack, print to stdout
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"
SEEN_FILE = STATE_DIR / "seen_posts.json"
STORAGE_STATE = ROOT / "storage_state.json"

SKILL_PROMPT = """\
Use the linkedin-comment-ideas skill (~/.claude/skills/linkedin-comment-ideas).

The LinkedIn post is pre-loaded below — SKIP the skill's Step 1 (Playwright
load) and use the pasted text as-is.

Generate EXACTLY 3 comment variants (override the skill's 2-3 default).
Apply one strategy per variant. Do all the pre-work the skill requires
(check Posted folder for prior public stance, mine Transcripts when a
personal example is needed, etc.).

Return ONLY a single JSON object on the final line, nothing else after it:
{{"variants": [{{"strategy": "...", "comment": "...", "rationale": "..."}}, ...]}}

Lead: {lead}
Headline: {headline}
Post URL: {url}

Post text:
---
{text}
---
"""


@dataclass
class Alert:
    lead_name: str
    lead_headline: str
    post_url: str
    post_text: str
    alert_id: str = ""

    @property
    def post_id(self) -> str:
        key = self.alert_id or self.post_url
        return hashlib.sha256(key.encode()).hexdigest()[:16]


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except json.JSONDecodeError:
        return set()


def save_seen(seen: set[str]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))


def log(msg: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    line = f"{stamp}  {msg}"
    print(line, flush=True)
    with (LOG_DIR / "daily.log").open("a") as f:
        f.write(line + "\n")


# ---------- Sales Navigator scrape ----------------------------------------

SALES_NAV_URL = os.environ.get(
    "SALES_NAV_URL",
    "https://www.linkedin.com/sales/home?alertGroup=LEAD&listId=7451883417308237824",
)

# Walk the Sales Nav alerts feed. Cards are <article class="alert-card-new">
# with a data-alert-id URN; only LEAD_SHARED_UPDATE ids are post-share
# alerts (vs job-change, news, etc). The post URL is not in the DOM (Sales
# Nav routes "View" through a JS click handler), so we use data-alert-id
# as the stable dedup key and fall back to the lead's Sales Nav profile
# URL for the Slack "view" link — the post text itself is captured in
# full from the card.
EXTRACT_JS = r"""
() => {
  const out = [];
  const cards = Array.from(document.querySelectorAll('article.alert-card-new'));
  for (const card of cards) {
    const alertId = card.getAttribute('data-alert-id') || '';
    if (!alertId.includes('LEAD_SHARED_UPDATE')) continue;

    const profileLink = card.querySelector('a[aria-label^="View profile for"]');
    let leadName = '';
    let profileUrl = '';
    if (profileLink) {
      leadName = (profileLink.getAttribute('aria-label') || '')
        .replace(/^View profile for\s+/, '').trim();
      profileUrl = profileLink.href;
    }

    const rawLines = (card.innerText || '')
      .split('\n').map(l => l.trim()).filter(Boolean);

    let i = 0;
    if (rawLines[i] && /is online$/i.test(rawLines[i])) i++;
    if (rawLines[i] && /\bshared a post$/i.test(rawLines[i])) i++;

    const leadHeadline = rawLines[i] || '';
    if (leadHeadline) i++;

    // Time-ago line: "15 hours", "2 days", "1 week", etc.
    if (rawLines[i] && /^\d+\s+\w+$/.test(rawLines[i])) i++;

    const bodyLines = [];
    for (; i < rawLines.length; i++) {
      const line = rawLines[i];
      if (line === 'View') break;
      if (line.startsWith('Clear this alert')) break;
      bodyLines.push(line);
    }

    // Sales Nav renders the post body once as a truncated button preview
    // and once in full — dedup identical lines.
    const seen = new Set();
    const uniqueBody = [];
    for (const line of bodyLines) {
      if (seen.has(line)) continue;
      seen.add(line);
      uniqueBody.push(line);
    }
    const postText = uniqueBody.join('\n').trim();

    if (!leadName || !postText) continue;
    out.push({ leadName, leadHeadline, profileUrl, alertId, postText });
  }
  return out;
}
"""


def expand_all_see_more(page: Page) -> None:
    """Click every visible "…see more" toggle so longer posts render fully."""
    selectors = [
        "button:has-text('see more')",
        "button:has-text('See more')",
        "button[aria-label*='see more' i]",
    ]
    for sel in selectors:
        try:
            buttons = page.locator(sel)
            for i in range(min(buttons.count(), 40)):
                try:
                    buttons.nth(i).click(timeout=500)
                except Exception:
                    pass
        except Exception:
            pass


def detect_login_wall(page: Page) -> bool:
    if "/login" in page.url or "/checkpoint" in page.url:
        return True
    title = (page.title() or "").lower()
    return "sign in" in title and "linkedin" in title


def scrape_alerts() -> list[Alert]:
    if not STORAGE_STATE.exists():
        raise SystemExit(
            f"{STORAGE_STATE} missing. Run `python linkedin_login.py` first."
        )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STORAGE_STATE))
        page = context.new_page()
        page.goto(SALES_NAV_URL, wait_until="domcontentloaded", timeout=60_000)

        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass

        if detect_login_wall(page):
            browser.close()
            raise SystemExit(
                "LOGIN_WALL: LinkedIn redirected to the sign-in page. "
                "Re-run `python linkedin_login.py` to refresh storage_state.json."
            )

        # Scroll a few times to trigger lazy-loading of more alerts.
        for _ in range(3):
            page.mouse.wheel(0, 2000)
            time.sleep(0.8)

        expand_all_see_more(page)
        time.sleep(0.5)

        raw = page.evaluate(EXTRACT_JS)
        browser.close()

    alerts = [
        Alert(
            lead_name=a["leadName"],
            lead_headline=a["leadHeadline"],
            post_url=(a.get("profileUrl") or "").split("?")[0],
            post_text=a["postText"],
            alert_id=a.get("alertId", ""),
        )
        for a in raw
    ]
    return alerts


# ---------- Claude skill invocation ---------------------------------------

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

JSON_OBJECT_RE = re.compile(r"\{[^{}]*\"variants\"[\s\S]*?\}\s*$", re.MULTILINE)


def call_skill(alert: Alert) -> list[dict] | None:
    prompt = SKILL_PROMPT.format(
        lead=alert.lead_name,
        headline=alert.lead_headline,
        url=alert.post_url,
        text=alert.post_text,
    )
    try:
        proc = subprocess.run(
            [
                CLAUDE_BIN,
                "-p",
                prompt,
                "--output-format",
                "json",
                # Headless cron — auto-approve tool calls the skill needs
                # (Read its own reference files, Drive/Docs MCP, etc.).
                "--permission-mode",
                "bypassPermissions",
            ],
            capture_output=True,
            text=True,
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        log(f"  skill timeout for {alert.post_url}")
        return None
    if proc.returncode != 0:
        log(f"  skill nonzero exit {proc.returncode}: {proc.stderr.strip()[:300]}")
        return None

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log(f"  skill output not JSON envelope: {proc.stdout[:200]}")
        return None
    body = envelope.get("result") if isinstance(envelope, dict) else None
    if not isinstance(body, str):
        log("  skill envelope missing 'result'")
        return None

    # Find the JSON object in the body. Try strict-from-end first (object
    # ending the body), then any object containing "variants".
    candidates = []
    m = JSON_OBJECT_RE.search(body)
    if m:
        candidates.append(m.group(0))
    # Fallback: scan for balanced braces around "variants".
    if "variants" in body and not candidates:
        idx = body.find("{")
        while idx != -1:
            depth = 0
            for j in range(idx, len(body)):
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(body[idx : j + 1])
                        break
            idx = body.find("{", idx + 1)

    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        variants = obj.get("variants")
        if isinstance(variants, list) and variants:
            return variants[:3]
    log(f"  could not extract variants from skill output: {body[:300]}")
    return None


# ---------- Slack posting -------------------------------------------------


def trunc(text: str, n: int = 1500) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def post_to_slack(client: WebClient, channel: str, alert: Alert, variants: list[dict]) -> str | None:
    top_text = (
        f"*New post from {alert.lead_name}*"
        + (f" — _{alert.lead_headline}_" if alert.lead_headline else "")
        + (f"\n<{alert.post_url}|View lead in Sales Navigator>" if alert.post_url else "")
        + "\n\n"
        + "> " + trunc(alert.post_text).replace("\n", "\n> ")
        + "\n\nReact 1️⃣ / 2️⃣ / 3️⃣ to pick. Reply in thread with edit notes (optional)."
    )
    try:
        top = client.chat_postMessage(channel=channel, text=top_text, unfurl_links=False)
    except SlackApiError as e:
        log(f"  slack top post failed: {e.response.get('error')}")
        return None
    ts = top["ts"]

    for i, v in enumerate(variants, start=1):
        strategy = v.get("strategy", "?")
        comment = v.get("comment", "").strip()
        rationale = v.get("rationale", "").strip()
        body = f"*Option {i} — {strategy}*\n{comment}"
        if rationale:
            body += f"\n_Why: {rationale}_"
        try:
            client.chat_postMessage(
                channel=channel, thread_ts=ts, text=body, unfurl_links=False
            )
        except SlackApiError as e:
            log(f"  slack option {i} post failed: {e.response.get('error')}")
    return ts


# ---------- Main ----------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Slack posting and seen-state updates; print results to stdout.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N new posts (for testing).",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")

    channel = os.environ.get("SLACK_CHANNEL_ID", "").strip()
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not args.dry_run and (not channel or not token):
        raise SystemExit("SLACK_BOT_TOKEN and SLACK_CHANNEL_ID must be set in .env")

    log("run: scrape start")
    try:
        alerts = scrape_alerts()
    except SystemExit as e:
        # Login-wall etc. — let it bubble after logging.
        log(f"run: aborted — {e}")
        raise
    log(f"run: scraped {len(alerts)} alerts")

    seen = load_seen()
    new_alerts = [a for a in alerts if a.post_id not in seen]
    log(f"run: {len(new_alerts)} new (vs {len(seen)} previously seen)")

    if args.limit:
        new_alerts = new_alerts[: args.limit]

    client = WebClient(token=token) if not args.dry_run else None

    posted = 0
    for alert in new_alerts:
        log(f"  -> {alert.lead_name}: {alert.post_url}")
        variants = call_skill(alert)
        if not variants:
            log("    skipped (no variants)")
            continue
        if len(variants) < 3:
            log(f"    only {len(variants)} variants returned; posting anyway")
        if args.dry_run:
            print(json.dumps({"alert": asdict(alert), "variants": variants}, indent=2))
        else:
            ts = post_to_slack(client, channel, alert, variants)
            if ts:
                seen.add(alert.post_id)
                posted += 1

    if not args.dry_run:
        save_seen(seen)
    log(f"run: done — posted {posted}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        log(f"run: crashed — {type(e).__name__}: {e}")
        sys.exit(1)
