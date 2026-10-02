# ai-usage

macOS command-line usage digest for Claude Code, Codex, and Cursor. It prints a terminal report and can post the same report to Slack Incoming Webhooks.

## What can be collected

| Service | Built-in source | Notes |
| --- | --- | --- |
| Claude Code | `ccusage claude blocks --json` and `ccusage claude weekly --json` | Reads local Claude Code logs. These are activity/token/cost reports; they are **not** official plan quota or remaining-limit values. |
| Codex | `ccusage codex daily --json` | Reads local Codex logs for activity/token/cost. This is not the official plan quota or remaining-limit value. The Codex CLI `/status` is interactive. |
| Cursor | User API `/v1/me` and Cloud Agent usage endpoints | Reports token usage for the 20 newest Cloud Agents. It does not expose Cursor IDE usage or remaining plan allowance. |

Provider command output must be JSON. Commands are configured as argument arrays in `~/.config/ai-usage/config.toml`, for example:

```toml
[commands.codex]
argv = ["my-codex-usage-exporter", "--json"]
```

The command may return any JSON value; it is included in the report as returned. Do not put secrets in command arguments.

For Cursor Cloud Agent collection, add the User API key from Cursor Dashboard → API & SSH Keys to `~/.config/ai-usage/environment`:

```sh
CURSOR_API_KEY=your_cursor_user_api_key
```

Keep the file private with `chmod 600 ~/.config/ai-usage/environment`. The User API key can list Cloud Agents and their token usage. Cursor's public documentation directs users to the Spending dashboard for included usage pools and remaining allowance; those values are not exposed by the documented User API endpoints.

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

Snapshots are stored under `~/.config/ai-usage/snapshots/` and are shown with their capture time. They remain available if you want to record values from the Cursor Spending dashboard; the application does not scrape browser sessions.

## Data and security

- Local source: `~/.claude` usage logs through `ccusage` (ccusage must be installed separately).
- Manual snapshots and config stay on the Mac.
- Slack delivery sends the rendered digest to the configured Incoming Webhook.
- Provider commands run as the current user. Configure only commands you trust.
- `launchd` logs are written to `~/Library/Logs/ai-usage/`.

## Development

The CLI uses only the Python standard library.
