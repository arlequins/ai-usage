# ai-usage

macOS command-line usage digest for Claude Desktop/Code, Codex, and Cursor. It prints a terminal report and can post the same report to Slack Incoming Webhooks.

## What can be collected

| Service | Built-in source | Notes |
| --- | --- | --- |
| Claude Desktop / Code | Encrypted Claude Desktop token cache, Claude Code OAuth credentials, and local `ccusage` logs | Reads plan utilization/reset windows from an existing OAuth session and reports local Claude Code activity separately. The token formats and usage endpoint are undocumented. |
| Codex | `ccusage codex daily --json` and Codex CLI `app-server` | Shows local activity plus account rate-limit windows and reset times from the signed-in Codex CLI session. |
| Cursor | Cursor.app local session plus dashboard `GET /api/usage-summary` | Reads the existing Cursor login token from the local app database, then reports IDE plan usage pools and billing cycle. The endpoint and token format are undocumented and may change. |

The terminal and Slack use the same compact report. Provider quota/reset information is shown separately from `ccusage` local activity; estimated token costs are not plan balances or invoices. For quota windows with known duration, `ai-usage` estimates whether the current average consumption rate could exhaust the limit before reset. This is a projection from one usage snapshot and assumes a steady rate; it is not a guarantee. When a local activity total is unusually large, the report flags it for review instead of presenting it as quota consumption.

Claude quota is read from the same undocumented OAuth usage endpoint used by Claude's usage view. `ai-usage` prefers `CLAUDE_CODE_OAUTH_TOKEN` when set, then the Claude Code credentials file or Keychain item. If those do not contain a usable OAuth token, macOS Claude Desktop is checked: its encrypted `oauth:tokenCacheV2` or `oauth:tokenCache` value is decrypted in memory using the `Claude Safe Storage` Keychain key. The active desktop cache is read-only; `ai-usage` does not refresh tokens or write to Claude's config. Tokens are never printed or stored by `ai-usage`. If a token is expired, reopen Claude Desktop or Claude Code to refresh sign-in. Keychain access may require the login keychain to be unlocked. These token formats and the usage endpoint are undocumented and may change.

Codex quota is requested from the locally installed Codex CLI, or the CLI bundled with the ChatGPT macOS app, using its `app-server` protocol with read-only sandbox and no approval prompts. `ai-usage` does not read or refresh Codex credentials itself. Codex must be installed and signed in for quota windows to appear. The quota request runs alongside activity collection and has a 15-second timeout.

Provider command output must be JSON. Commands are configured as argument arrays in `~/.config/ai-usage/config.toml`, for example:

```toml
[commands.codex]
argv = ["my-codex-usage-exporter", "--json"]
```

The command may return any JSON value; it is included in the report as returned. Do not put secrets in command arguments.

Cursor's User API key from Dashboard → API & SSH Keys is for Cloud Agents; it does not provide IDE plan usage. By default, `ai-usage` reads Cursor's existing app session from `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` in read-only mode. It does not modify the database or print/store the token. The token is sent only to `cursor.com` in a request to its undocumented usage-summary endpoint. Open Cursor and sign in first. If automatic session discovery does not work, a dashboard cookie can be configured as a fallback by copying the `WorkosCursorSessionToken` value from a signed-in `https://cursor.com/dashboard/usage` session and adding it to `~/.config/ai-usage/environment`:

```sh
CURSOR_SESSION_TOKEN=your_workos_cursor_session_token
```

Keep the file private with `chmod 600 ~/.config/ai-usage/environment`. This value is a sensitive login session credential: do not share it or commit it. It can expire; if Cursor returns 401, reopen Cursor or sign in to the dashboard to refresh the session. Cursor may change the private app database key, session format, or endpoint at any time.

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

Interactive terminals show color-coded quota bars and a provider summary. Slack and redirected output stay plain text. Set `NO_COLOR=1` to turn off terminal colors.

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
- Claude quota collection reads the current OAuth access token without printing it, storing it, or refreshing it; the token is sent only to `api.anthropic.com` for the undocumented usage request.
- Codex quota collection uses the Codex CLI app-server read-only request and its existing sign-in.
- Cursor usage collection reads the local Cursor app session database in read-only mode and calls an undocumented dashboard endpoint. Its session token is sensitive and remains in memory only.
- No Cursor User API key is used; those keys currently serve Cloud Agent API access rather than IDE plan usage.
- Slack delivery sends the rendered digest to the configured Incoming Webhook.
- Provider commands run as the current user. Configure only commands you trust.
- `launchd` logs are written to `~/Library/Logs/ai-usage/`.

## Development

The CLI uses only the Python standard library.
