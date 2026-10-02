# ai-usage

macOS command-line usage digest for Claude Code, Codex, and Cursor. It prints a terminal report and can post the same report to Slack Incoming Webhooks.

## What can be collected

| Service | Built-in source | Notes |
| --- | --- | --- |
| Claude Code | `ccusage claude blocks --json` and `ccusage claude weekly --json` | Reads local Claude Code logs. These are activity/token/cost reports; they are **not** official plan quota or remaining-limit values. |
| Codex | `ccusage codex daily --json` | Reads local Codex logs for activity/token/cost. This is not the official plan quota or remaining-limit value. The Codex CLI `/status` is interactive. |
| Cursor | Cursor dashboard `GET /api/usage-summary` with a signed-in session cookie | Reports IDE plan usage pools and billing cycle. This endpoint is undocumented and may change. |

When [CodexBar](https://github.com/steipete/CodexBar) is installed, `ai-usage` also reads its quota snapshot for all three providers. This adds remaining percentages, reset countdowns, and pace-based depletion forecasts while keeping the local Claude/Codex activity totals. CodexBar reuses existing sign-ins and can obtain Cursor usage from Cursor.app or browser sessions, so a manually copied Cursor session token is not required.

The terminal and Slack use the same compact report. Provider quota/reset information is shown separately from `ccusage` local activity; estimated token costs are not plan balances or invoices. When a local activity total is unusually large, the report flags it for review instead of presenting it as quota consumption.

Install CodexBar with `brew install --cask codexbar`, open it once, and enable Claude, Codex, and Cursor under Settings → Providers. Its CLI data source is read-only; `ai-usage` does not read or store provider credentials when using this path. The pace forecast compares current quota use with the elapsed reset window; it is an estimate, not a guarantee of future usage.

Provider command output must be JSON. Commands are configured as argument arrays in `~/.config/ai-usage/config.toml`, for example:

```toml
[commands.codex]
argv = ["my-codex-usage-exporter", "--json"]
```

The command may return any JSON value; it is included in the report as returned. Do not put secrets in command arguments.

If CodexBar is unavailable, Cursor can still use the dashboard's undocumented usage-summary endpoint. Cursor's User API key from Dashboard → API & SSH Keys is for Cloud Agents; it does not provide IDE plan usage. As a fallback, copy the `WorkosCursorSessionToken` cookie from a signed-in `https://cursor.com/dashboard/usage` session using Chrome DevTools → Application → Cookies → `https://cursor.com`, then add it to `~/.config/ai-usage/environment`:

```sh
CURSOR_SESSION_TOKEN=your_workos_cursor_session_token
```

Keep the file private with `chmod 600 ~/.config/ai-usage/environment`. This value is a sensitive login session credential: do not share it or commit it. It can expire; if Cursor returns 401, copy a fresh cookie from the dashboard. The fallback reports current usage but does not provide pace history.

## Install

Requires macOS and Python 3.11+ (no third-party Python packages).

```sh
./install.sh
```

The installer creates `~/.local/bin/ai-usage` and the user configuration and snapshot directories. Add `~/.local/bin` to `PATH` if needed.

Add a Slack Incoming Webhook URL to `~/.config/ai-usage/config.toml`:

```toml
[slack]
webhook_url_env = "AI_USAGE_SLACK_WEBHOOK_URL"
```

Store the webhook in your shell environment or a launchd-readable environment file. For scheduled delivery, use the launchd environment file:

```sh
mkdir -p ~/.config/ai-usage
printf '%s\n' 'AI_USAGE_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...' > ~/.config/ai-usage/environment
chmod 600 ~/.config/ai-usage/environment
```

Do not commit the webhook URL.

## Use

```sh
ai-usage                 # terminal digest
ai-usage --slack         # post digest to Slack
ai-usage --json          # machine-readable report
ai-usage install-agent   # load the scheduled job
ai-usage uninstall-agent # unload it
```

The schedule is 09:00, 13:00, and 17:00 local time. `install-agent` generates and loads the plist, reading `~/.config/ai-usage/environment` for the webhook environment variable.

### Manual snapshots

Save provider output as JSON, then:

```sh
ai-usage snapshot codex ~/Downloads/codex-usage.json
ai-usage snapshot cursor ~/Downloads/cursor-usage.json
```

Snapshots are stored under `~/.config/ai-usage/snapshots/` and are shown with their capture time.

## Data and security

- Local source: `~/.claude` usage logs through `ccusage` (ccusage must be installed separately).
- Manual snapshots and config stay on the Mac.
- Cursor dashboard collection uses an undocumented endpoint and a sensitive browser session token; Cursor may change the endpoint or expire the token.
- Optional quota context and pace projections come from CodexBar's read-only CLI snapshot; provider credentials remain managed by CodexBar.
- Slack delivery sends the rendered digest to the configured Incoming Webhook.
- Provider commands run as the current user. Configure only commands you trust.
- `launchd` logs are written to `~/Library/Logs/ai-usage/`.

## Development

The CLI uses only the Python standard library.
