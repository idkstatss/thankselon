# X → Discord Notifier (free, serverless, GitHub Actions)

Polls one or more X accounts every 15 minutes using a cookie-authenticated
session (no paid API), and posts to a Discord webhook when there's a new
original post or quote tweet. State is tracked in `state.json`, committed
back to the repo by the workflow itself.

## Files in this repo

```
.github/workflows/x_ping.yml   # the scheduled GitHub Actions workflow
check_x.py                     # the script the workflow runs
requirements.txt                # pinned Python dependencies
state.json                      # tracks last-seen tweet ID per account
```

## 1. Create the repository

1. Create a **new, private** GitHub repository (private matters — your
   burner account's cookies live in Secrets, and you don't want this repo
   or its Actions logs public).
2. Add these four files to the repo root (and the workflow under
   `.github/workflows/`), preserving the folder structure.
3. Commit and push to `main` (or whatever your default branch is — the
   workflow pushes to `GITHUB_REF_NAME`, so it'll match whatever branch
   triggered it).

## 2. Get `auth_token` and `ct0` from your burner X account

Use a **burner/secondary account**, not your main one — this stores
long-lived session cookies as GitHub Secrets, and if the repo or secrets
ever leaked, that account's session would be exposed. Treat this like a
password.

1. Open a private/incognito browser window and log into X with the burner
   account.
2. Open Developer Tools:
   - Chrome/Edge: `F12` or `Ctrl+Shift+I` (`Cmd+Option+I` on Mac)
   - Firefox: `F12`
3. Go to the **Application** tab (Chrome/Edge) or **Storage** tab
   (Firefox).
4. In the left sidebar, expand **Cookies** → click `https://x.com`.
5. Find the row named `auth_token` — copy its **Value** column. This is a
   long hex string.
6. Find the row named `ct0` — copy its **Value** column too.
7. Close the dev tools. **Do not log out** of that browser session until
   you've confirmed the bot works — logging out can invalidate the
   cookies immediately. After confirming it works, you can stay logged in
   or not; the copied cookie values are independent of the browser
   session once GitHub has them, but they can still expire or be
   invalidated by X (password changes, suspicious-activity flags,
   extended inactivity).

These cookies are **not permanent** — X can expire or invalidate them. If
the bot suddenly stops working, check the Actions run logs first (an auth
failure will show up clearly); you may just need to repeat this step.

## 3. Create a Discord webhook

1. In your Discord server, go to the target channel's **Settings →
   Integrations → Webhooks → New Webhook**.
2. Name it (e.g. "X Notifier"), pick the channel, and copy the **Webhook
   URL**.

## 4. (Optional) Get the Discord role ID to ping

1. In Discord, enable **Developer Mode**: User Settings → Advanced →
   Developer Mode.
2. Go to Server Settings → Roles, right-click the role you want pinged,
   and choose **Copy Role ID**.
3. Make sure the role is **mentionable**, or the webhook's mention won't
   actually ping anyone (Server Settings → Roles → [role] → toggle
   "Allow anyone to @mention this role", or add the webhook's integration
   permission to mention it).

## 5. Add GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add each of these:

| Secret name           | Value                                                        |
|------------------------|---------------------------------------------------------------|
| `X_AUTH_TOKEN`         | the `auth_token` cookie value from step 2                     |
| `X_CT0`                | the `ct0` cookie value from step 2                             |
| `TARGET_USERS`         | comma-separated usernames, no `@`, e.g. `nasa,spacex`          |
| `DISCORD_WEBHOOK_URL`  | the webhook URL from step 3                                    |
| `DISCORD_ROLE_ID`      | (optional) the role ID from step 4 — omit this secret entirely if you don't want a role ping |

No secret needs quotes — paste the raw value.

## 6. Enable Actions and test it

1. Go to the **Actions** tab of your repo. If prompted, click "I
   understand my workflows, go ahead and enable them."
2. Select **X to Discord Notifier** in the left sidebar, then click **Run
   workflow** (this is the `workflow_dispatch` trigger) to test manually
   rather than waiting up to 15 minutes.
3. Check the run's logs. On the first run for each account you should see
   `first run for @username; baselining ... (no notification sent)` and
   `state.json` will get a commit from `github-actions[bot]` with that
   account's current latest tweet ID.
4. Run it again (or wait for the next scheduled run) after the target
   account posts something new — you should get a Discord message like:

   ```
   @RoleName **New post from @username**
   <tweet text, truncated to 300 chars>
   https://fxtwitter.com/username/status/1234567890
   ```

## Notes, limits, and things that will eventually need attention

- **GitHub Actions free tier**: public repos get unlimited Actions
  minutes; **private** repos get 2,000 free minutes/month (free plan).
  Each run here is typically under a minute, and at every-15-minutes
  (~2,880 runs/month) that comfortably fits, but if you add many target
  accounts and runs start taking a few minutes each, keep an eye on
  **Settings → Billing → Plans and usage**.
- **`cron` scheduling is best-effort**: GitHub explicitly says scheduled
  workflows can be delayed (sometimes by 10+ minutes) during high load,
  and a schedule gets **disabled automatically after 60 days of repo
  inactivity** (no pushes). The state-commit from this workflow counts as
  activity, so as long as it's finding *something* to commit periodically
  it should stay alive — but if you don't touch the repo and nothing new
  posts for 60+ days, re-enable it manually from the Actions tab if it
  stops.
- **Cookie expiry is the most likely failure mode.** twikit and X's web
  client are not an official, supported integration — X can log out
  sessions, require re-verification, or rate-limit/flag automated-looking
  access at any time, with no advance notice. If that happens, repeat
  step 2 with a fresh login and update the two secrets.
- **`twikit`'s pinned version may drift out of date.** X's internal web
  API changes periodically and twikit releases updates to track it. If
  the bot starts failing with parsing or auth errors that step 2 doesn't
  fix, check the [twikit PyPI page](https://pypi.org/project/twikit/) for
  a newer release and bump the version pinned in `requirements.txt`.
- This script skips retweets always, and skips replies unless you set
  `INCLUDE_REPLIES: "true"` in the workflow's `env:` block. Quote tweets
  are treated as original posts and will notify.
- Only the single most recent eligible tweet is checked per run (not a
  full backlog). If an account posts multiple times within one 15-minute
  window, only the latest will be reported — you won't get duplicate
  pings, but you could miss an interim post. Lower the cron interval
  (e.g. `*/5 * * * *`) if that matters to you, keeping the minutes budget
  above in mind.
