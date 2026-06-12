# LinkedIn Lead-Alerts → 3 Comment Drafts → Slack

Daily pipeline that drafts 3 comment variants (in Petro's voice via the
`linkedin-comment-ideas` skill) for each new post by a Sales Navigator lead,
and posts them to Slack for review/redaction before you paste back to
LinkedIn.

## What it does

1. `daily_run.py` (launchd, 09:00 Mac Studio) scrapes the Sales Navigator
   lead-alerts feed with Playwright using a saved logged-in session.
2. For each new post, shells out to the `claude` CLI with the
   `linkedin-comment-ideas` skill to generate 3 variants.
3. Posts each post + 3 variants into `#linkedin-replies`
   (top-level = post; thread = variants).
4. You react `1️⃣` / `2️⃣` / `3️⃣` to pick. Reply in thread with edit notes.
5. A scheduled remote agent (every 15 min) applies edits and posts
   `*FINAL —*` in the thread. You copy/paste into LinkedIn yourself.

## One-time setup (Mac Studio)

```bash
# 1. Python deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 2. Skill (linkedin-comment-ideas) — symlink so `git pull` refreshes it
git clone git@github.com:boarlabsxyz/linkedin-ai.git ~/repos/linkedin-ai
ln -s ~/repos/linkedin-ai/.claude/skills/linkedin-comment-ideas \
      ~/.claude/skills/linkedin-comment-ideas

# 3. Configure MCP servers Claude Code needs for the skill
#    (Google Drive + Google Docs — for the Posted/Transcripts folders the
#    skill references). Use `claude mcp add` per your auth flow.

# 4. Environment
cp .env.example .env
# Fill in SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, SLACK_HUMAN_USER_ID.

# 5. LinkedIn login — opens a browser; log in (including 2FA); press Enter.
python linkedin_login.py
chmod 600 storage_state.json

# 6. Smoke test (no Slack writes)
python daily_run.py --dry-run

# 7. Install the launchd job
cp com.user.linkedin-replies.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.user.linkedin-replies.plist

# 8. Register the remote Slack-poll routine — paste the prompt from
#    remote_routine.md into `/schedule` (cron: */15 * * * *).
```

## Slack bot setup

Create a Slack app (https://api.slack.com/apps), add bot token scopes:

- `chat:write`
- `channels:history` (or `groups:history` if `#linkedin-replies` is private)
- `channels:read`
- `reactions:read`

Install to workspace, copy the Bot User OAuth Token (`xoxb-…`) into `.env`,
invite the bot to `#linkedin-replies`.

## Files

| File | Purpose |
|---|---|
| `linkedin_login.py` | One-time headed browser to capture `storage_state.json`. |
| `daily_run.py` | Daily scrape + generate + post. Supports `--dry-run`. |
| `com.user.linkedin-replies.plist` | launchd schedule. |
| `remote_routine.md` | Prompt for the scheduled remote agent that finalizes replies. |
| `state/seen_posts.json` | Dedup ledger (auto-created). |
| `storage_state.json` | Playwright session cookies. Sensitive — `chmod 600`. |
| `logs/` | Daily logs. |

## Re-auth

LinkedIn cookies expire (~30 days). If `daily_run.py` detects a login-wall
it posts a heads-up in Slack — re-run `python linkedin_login.py` to refresh.
