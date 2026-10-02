# ai-usage

macOS command-line usage digest for Claude Desktop/Code, Codex, and Cursor. It prints a detailed terminal report and can send a compact, configurable summary to Slack through a bot or Incoming Webhook.

## What can be collected

| Service | Built-in source | Notes |
| --- | --- | --- |
| Claude Desktop / Code | Encrypted Claude Desktop token cache, Claude Code OAuth credentials, and local `ccusage` logs | Reads plan utilization/reset windows from an existing OAuth session and reports local Claude Code activity separately. The token formats and usage endpoint are undocumented. |
| Codex | `ccusage codex daily --json` and Codex CLI `app-server` | Shows local activity plus account rate-limit windows and reset times from the signed-in Codex CLI session. |
| Cursor | Cursor.app local session plus dashboard `GET /api/usage-summary` | Reads the existing Cursor login token from the local app database, then reports IDE plan usage pools and billing cycle. The endpoint and token format are undocumented and may change. |

The terminal report separates provider quota/reset information from `ccusage` local activity; estimated token costs are not plan balances or invoices. The Slack summary only includes Claude weekly usage and Cursor usage, with their next reset times; Codex is omitted. For quotas with enough timing data, the summary estimates whether usage will run out before reset. This projection assumes a steady rate and is not a guarantee. When a local activity total is unusually large, the terminal report flags it for review instead of presenting it as quota consumption.

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

## Installation

Requirements:

- macOS
- Python 3.11 or newer
- `ccusage` is optional and provides local activity/cost reports. Provider quota collection does not depend on it.

### Install the packaged CLI

The GitHub release includes a Python wheel and source archive. `pipx` installs the command into an isolated environment and adds the executable to your user path. Install `pipx` if needed, then install the release wheel:

```zsh
brew install pipx
pipx ensurepath
```

Restart Terminal after `pipx ensurepath`, then run:

```zsh
pipx install https://github.com/arlequins/ai-usage/releases/download/v0.1.0/ai_usage-0.1.0-py3-none-any.whl
ai-usage --version
ai-usage
```

To upgrade to a newer release, replace the version in the wheel URL and run `pipx install --force "https://github.com/arlequins/ai-usage/releases/download/v0.1.1/ai_usage-0.1.1-py3-none-any.whl"` (substitute the current release version).

### Install from a checkout

The repository installer remains available and copies the standalone script to `~/.local/bin`:

```zsh
git clone https://github.com/arlequins/ai-usage.git
cd ai-usage
./install.sh
```

The installer creates `~/.local/bin/ai-usage`, `~/.config/ai-usage/config.toml`, and the snapshots directory. Add `~/.local/bin` to `PATH` if needed. For an existing checkout, update it with `git pull` before running `./install.sh` again.

### Optional: install `ccusage`

`ccusage` is needed only for Claude Code and Codex local activity/cost sections. Install it with npm, then verify that the command is available:

```zsh
npm install -g ccusage
ccusage --version
```

### Sign in to provider apps

- **Claude:** Sign in to Claude Desktop or Claude Code. macOS may ask to allow access to the `Claude Safe Storage` Keychain item. Quota uses an undocumented Anthropic endpoint; activity logs are separate.
- **Codex:** Sign in to Codex CLI or the Codex app. The plan quota collector uses the local Codex app-server; local cost/activity reporting additionally uses `ccusage`.
- **Cursor:** Open Cursor and sign in. The collector reads Cursor's existing local app session. A User API key is not used for IDE plan usage.

Create the app configuration directory if you installed with `pipx` and plan to use local snapshots, Slack options, or provider command overrides:

```zsh
mkdir -p ~/.config/ai-usage/snapshots
vi ~/.config/ai-usage/config.toml
```

The CLI works with built-in defaults if no config file exists.

### Slack bot delivery

Create a Slack app, add the `chat:write` bot token scope, install it in your workspace, and invite the bot to the destination channel. Copy the Bot User OAuth Token (`xoxb-…`) and the channel ID. Slack may require a workspace admin to approve app installation. The bot posts using `chat.postMessage`; the bot must be a member of the channel. See Slack's [`chat.postMessage` documentation](https://api.slack.com/methods/chat.postMessage).

Enter the token and channel ID without putting the token in shell history:

```zsh
mkdir -p ~/.config/ai-usage
read -r -s "AI_USAGE_SLACK_BOT_TOKEN?Slack Bot User OAuth Token: "
printf '\n'
read -r "AI_USAGE_SLACK_CHANNEL?Slack channel ID: "
printf '\nAI_USAGE_SLACK_BOT_TOKEN=%s\nAI_USAGE_SLACK_CHANNEL=%s\n' "$AI_USAGE_SLACK_BOT_TOKEN" "$AI_USAGE_SLACK_CHANNEL" >> ~/.config/ai-usage/environment
chmod 600 ~/.config/ai-usage/environment
unset AI_USAGE_SLACK_BOT_TOKEN AI_USAGE_SLACK_CHANNEL
ai-usage --slack
```

The environment file can also be used by the scheduled `launchd` job. Bot credentials take precedence when both bot and webhook credentials are configured.

Slack messages contain a compact summary of Claude's weekly usage and Cursor's usage; Codex is omitted. Each service includes its next reset date. If the current pace projects exhaustion before reset, the summary includes an estimated exhaustion time; otherwise it indicates that usage should last through reset. The default language is English. To switch the Slack summary to Japanese, add `language = "ja"` under the existing `[slack]` section in `~/.config/ai-usage/config.toml`. If there is no `[slack]` section yet, add:

```toml
[slack]
language = "ja" # Use "en" for English
```

The message includes `@Wonho An` by default. To notify the user with a real Slack mention, save the member ID in `~/.config/ai-usage/environment` as `AI_USAGE_SLACK_MENTION_USER_ID=U...`. In Slack, open the user's profile, select **More**, then **Copy member ID**. The ID overrides the display name and is kept out of the repository. Keep this setting in the same private environment file as your bot token.

Incoming Webhooks are also supported as an alternative. Add a Slack Incoming Webhook URL to `~/.config/ai-usage/config.toml`:

```toml
[slack]
webhook_url_env = "AI_USAGE_SLACK_WEBHOOK_URL"
```

Store the webhook in the launchd-readable environment file:

```sh
mkdir -p ~/.config/ai-usage
printf '%s\n' 'AI_USAGE_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...' >> ~/.config/ai-usage/environment
chmod 600 ~/.config/ai-usage/environment
```

Do not commit the webhook URL.

## Use

```sh
ai-usage                 # terminal digest
ai-usage --slack         # post compact summary to Slack
ai-usage --json          # machine-readable report
ai-usage install-agent   # load the scheduled job
ai-usage uninstall-agent # unload it
```

Interactive terminals show color-coded quota bars and a provider summary. Slack and redirected output stay plain text. Set `NO_COLOR=1` to turn off terminal colors.

The schedule is 09:00, 13:00, and 17:00 local time. `install-agent` generates and loads the plist, reading `~/.config/ai-usage/environment` for the Slack credentials.

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
- Slack delivery sends the compact summary through the configured bot token/channel or Incoming Webhook.
- Provider commands run as the current user. Configure only commands you trust.
- `launchd` logs are written to `~/Library/Logs/ai-usage/`.

## Development

The CLI uses only the Python standard library.
