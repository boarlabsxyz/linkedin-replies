# Scheduled remote routine — `linkedin-replies-finalizer`

Register this prompt via the `/schedule` skill with cron `*/15 * * * *`.
Replace `$SLACK_CHANNEL_ID` and `$SLACK_HUMAN_USER_ID` with the actual
values from your local `.env` before saving.

---

## Routine prompt (paste below into /schedule)

```
You are finalizing LinkedIn comment drafts in Slack.

Channel: $SLACK_CHANNEL_ID
Human user ID (Petro): $SLACK_HUMAN_USER_ID

STEPS

1. Call mcp__claude_ai_slack-test__readChannelHistory with channel=$SLACK_CHANNEL_ID,
   limit=50. Filter to top-level messages (no thread_ts on them) posted in
   the last 26 hours whose text starts with "*New post from ".

2. For each such message in oldest-first order:

   a. Look at its `reactions` field. Find one reaction whose name is one
      of "one", "two", "three" AND whose `users` array includes
      $SLACK_HUMAN_USER_ID. If none, skip this message.

      (Slack stores the digit emojis as names "one"/"two"/"three"; if the
      payload uses different names like "1️⃣", match those too.)

      Let selected_n = 1, 2, or 3 accordingly.

   b. Call mcp__claude_ai_slack-test__readThreadReplies with
      channel=$SLACK_CHANNEL_ID and thread_ts=<this message's ts>.

   c. If ANY reply in the thread has text starting with "*FINAL —*", skip
      (already finalized).

   d. Find the bot-posted thread reply whose text starts with
      "*Option {selected_n} —". Parse its content: everything between the
      first newline after that header and (if present) the "_Why:" line.
      That parsed text is `base_comment`.

   e. Gather edit notes: thread replies authored by $SLACK_HUMAN_USER_ID
      that are NOT quoted blocks (don't start with ">"). Concatenate them
      in order as `edit_notes`.

   f. If `edit_notes` is empty:
        final = base_comment
      Else:
        Rewrite base_comment per edit_notes, preserving Petro
        Ovchynnykov's voice (see VOICE RULES below). Output only the
        rewritten comment, nothing else.

   g. Post the result back in the thread via
      mcp__claude_ai_slack-test__replyInThread with text:

        *FINAL — copy/paste this into LinkedIn:*

        <final>

3. After processing all messages, stop. Do not loop, do not retry skipped
   messages — they'll be picked up on the next 15-min run.

VOICE RULES (apply when rewriting in step 2f)

- Technical honesty over marketing. Concrete numbers, named tradeoffs,
  named failure modes. No "revolutionary", "game-changing",
  "transforming".
- Anti-hype. Comfortable saying "we hope more than we know".
- Practitioner credibility: 7+ years in production GenAI; Co-CEO of
  Speed & Function; builder of True BDD.
- No AI-stylistic tells: no em-dash crutch, no "It's not X, it's Y", no
  listy buzzword stacks, no "let's dive in".
- Light humor and occasional neologisms are OK when natural.
- Match the language of the original post (English by default; some
  Ukrainian posts may appear).

DO NOT

- Do not link to Petro's own posts.
- Do not flatter ("great post", "love this", "spot on").
- Do not use engagement bait ("Agree?", "Thoughts?").
- Do not invent personal experience.
- Maximum 3 paragraphs.
```
