#!/usr/bin/env python3
"""Local usage digest for Claude Code, Codex, and Cursor."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import os
import plistlib
import queue
import sqlite3
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

APP_DIR = Path.home() / ".config" / "ai-usage"
CONFIG = APP_DIR / "config.toml"
SNAPSHOTS = APP_DIR / "snapshots"
LABEL = "com.local.ai-usage"


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def load_config() -> dict[str, Any]:
    path = Path(os.environ.get("AI_USAGE_CONFIG", CONFIG))
    if not path.exists():
        return {}
    try:
        import tomllib
    except ImportError:
        print("ai-usage requires Python 3.11+ to read TOML configuration", file=sys.stderr)
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def command_json(argv: list[str], timeout: int = 45) -> Any:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode:
        detail = proc.stderr.strip() or f"exited with status {proc.returncode}"
        raise RuntimeError(detail)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"command did not return valid JSON: {e}") from e


def run_configured(label: str, config: dict[str, Any]) -> dict[str, Any] | None:
    argv = config.get("commands", {}).get(label, {}).get("argv")
    if not argv:
        return None
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        raise RuntimeError(f"commands.{label}.argv must be an array of strings")
    return {"source": "configured command", "captured_at": now().isoformat(timespec="minutes"), "data": command_json(argv)}


def snapshot(label: str) -> dict[str, Any] | None:
    path = SNAPSHOTS / f"{label}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {"source": "snapshot error", "error": str(e)}


def setting(name: str) -> str:
    value = os.environ.get(name, "")
    if value:
        return value
    env_file = APP_DIR / "environment"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, sep, raw = line.partition("=")
            if sep and key.strip() == name:
                return raw.strip().strip("\"'")
    return ""


def claude_data() -> dict[str, Any]:
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            blocks_future = pool.submit(command_json, ["ccusage", "claude", "blocks", "--json"])
            weekly_future = pool.submit(command_json, ["ccusage", "claude", "weekly", "--json"])
            blocks = blocks_future.result()
            weekly = weekly_future.result()
        return {
            "source": "ccusage local logs (activity, not plan quota)",
            "captured_at": now().isoformat(timespec="minutes"),
            "blocks": blocks,
            "weekly": weekly,
        }
    except FileNotFoundError:
        return {"source": "unavailable", "error": "ccusage is not installed"}
    except (subprocess.TimeoutExpired, RuntimeError) as e:
        return {"source": "unavailable", "error": str(e)}


def claude_quota_data() -> dict[str, Any]:
    """Read Claude Code plan limits using its current OAuth token, without refreshing it."""
    credentials_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
    credentials_path = credentials_dir / ".credentials.json"
    try:
        credentials = json.loads(credentials_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"error": "Claude Code OAuth credentials were not found. Sign in to Claude Code first."}
    oauth = credentials.get("claudeAiOauth", {}) if isinstance(credentials, dict) else {}
    access_token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    expires_at = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    if not isinstance(access_token, str) or not access_token:
        return {"error": "Claude Code OAuth token was not found in its local credentials."}
    if isinstance(expires_at, (int, float)) and expires_at <= dt.datetime.now(dt.timezone.utc).timestamp() * 1000 + 60_000:
        return {"error": "Claude Code OAuth token is expired or nearly expired. Open Claude Code to refresh sign-in."}

    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={"Authorization": f"Bearer {access_token}", "anthropic-beta": "oauth-2025-04-20"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            usage = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"error": "Anthropic rejected the Claude Code session. Open Claude Code to refresh sign-in."}
        if e.code == 429:
            return {"error": "Anthropic temporarily rate-limited the usage request. Try again later."}
        return {"error": f"Claude usage request failed: HTTP {e.code} {e.reason}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return {"error": f"Claude usage request failed: {e}"}
    if not isinstance(usage, dict):
        return {"error": "Claude returned an unexpected usage response."}

    windows: list[dict[str, Any]] = []
    limits = usage.get("limits")
    if isinstance(limits, list) and limits:
        for limit in limits:
            if not isinstance(limit, dict):
                continue
            kind = limit.get("kind")
            scope = limit.get("scope")
            model = scope.get("model", {}) if isinstance(scope, dict) else {}
            model_name = model.get("display_name") if isinstance(model, dict) else None
            if kind == "session":
                label, seconds = "5-hour", 5 * 3600
            elif kind == "weekly_all":
                label, seconds = "Weekly", 7 * 24 * 3600
            elif kind == "weekly_scoped":
                label, seconds = f"Weekly ({model_name})" if model_name else "Weekly (model)", 7 * 24 * 3600
            else:
                continue
            window = quota_window(label, limit.get("percent"), limit.get("resets_at"), seconds)
            if window:
                windows.append(window)
    else:
        for key, label, seconds in (
            ("five_hour", "5-hour", 5 * 3600),
            ("seven_day", "Weekly", 7 * 24 * 3600),
            ("seven_day_sonnet", "Weekly (Sonnet)", 7 * 24 * 3600),
            ("seven_day_opus", "Weekly (Opus)", 7 * 24 * 3600),
        ):
            bucket = usage.get(key)
            if isinstance(bucket, dict):
                window = quota_window(label, bucket.get("utilization"), bucket.get("resets_at"), seconds)
                if window:
                    windows.append(window)
    if not windows:
        return {"error": "Claude returned no plan usage windows for this account."}

    quota: dict[str, Any] = {"windows": windows}
    plan = (oauth.get("subscriptionType") or oauth.get("rateLimitTier")) if isinstance(oauth, dict) else None
    if plan:
        quota["identity"] = {"plan": plan}
    return {"quota": quota, "source": "Claude Code usage endpoint (unofficial)"}


def quota_window(label: str, used: Any, reset_at: Any, duration_seconds: int) -> dict[str, Any] | None:
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    return {
        "label": label,
        "usedPercent": used,
        "remainingPercent": max(0, 100 - used),
        "resetAt": reset_at,
        "windowDurationSeconds": duration_seconds,
    }


def codex_data() -> dict[str, Any]:
    try:
        daily = command_json(["ccusage", "codex", "daily", "--json"])
        return {
            "source": "ccusage local logs (activity, not plan quota)",
            "captured_at": now().isoformat(timespec="minutes"),
            "daily": daily,
        }
    except FileNotFoundError:
        return {"source": "unavailable", "error": "ccusage is not installed"}
    except (subprocess.TimeoutExpired, RuntimeError) as e:
        return {"source": "unavailable", "error": str(e)}


def cursor_app_session() -> str | None:
    """Read Cursor's existing session token without modifying its local database."""
    database = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if not database.is_file():
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=0.5)
        row = connection.execute(
            "SELECT value FROM ItemTable WHERE key = ? LIMIT 1", ("cursorAuth/accessToken",)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        if connection:
            connection.close()
    if not row or row[0] is None:
        return None
    value = row[0]
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            try:
                value = value.decode("utf-16-le")
            except UnicodeDecodeError:
                return None
    if not isinstance(value, str):
        return None
    value = value.strip().strip('"')
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            value = parsed.get("accessToken") or parsed.get("token") or value
    except json.JSONDecodeError:
        pass
    if not isinstance(value, str) or value.count(".") < 2:
        return None
    try:
        payload = value.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        subject = claims.get("sub", "")
        expires = claims.get("exp")
        user_id = subject.rsplit("|", 1)[-1]
        if not user_id or not isinstance(expires, (int, float)) or expires <= dt.datetime.now(dt.timezone.utc).timestamp() + 60:
            return None
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return f"{urllib.parse.quote(user_id, safe='')}%3A%3A{value}"


def cursor_data() -> dict[str, Any]:
    session_token = setting("CURSOR_SESSION_TOKEN")
    source = "Cursor dashboard session (unofficial; IDE plan usage)"
    if not session_token:
        session_token = cursor_app_session() or ""
        source = "Cursor.app session (unofficial; IDE plan usage)"
    if not session_token:
        return {
            "source": "not configured",
            "status": "No valid Cursor session found. Open Cursor and sign in, or set CURSOR_SESSION_TOKEN in ~/.config/ai-usage/environment.",
        }

    req = urllib.request.Request(
        "https://cursor.com/api/usage-summary",
        headers={"Cookie": f"WorkosCursorSessionToken={session_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            summary = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"source": source, "error": "Cursor rejected the session. Open Cursor or the dashboard to refresh sign-in, then try again."}
        return {"source": source, "error": f"Cursor usage request failed: HTTP {e.code} {e.reason}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return {"source": source, "error": f"Cursor usage request failed: {e}"}

    return {
        "source": source,
        "captured_at": now().isoformat(timespec="minutes"),
        "usage": summary,
    }


def codex_executable() -> str | None:
    candidates = [
        shutil.which("codex"),
        "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
        str(Path.home() / ".local" / "bin" / "codex"),
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def codex_quota_data() -> dict[str, Any]:
    """Ask Codex app-server for the signed-in account's rate-limit windows."""
    executable = codex_executable()
    if not executable:
        return {"error": "Codex CLI was not found; install or sign in to Codex CLI to read plan limits."}
    messages: queue.Queue[Any] = queue.Queue()
    stderr_tail: list[str] = []
    try:
        proc = subprocess.Popen(
            [executable, "-s", "read-only", "-a", "never", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except OSError as e:
        return {"error": f"Codex app-server could not read plan limits: {e}"}

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        messages.put(None)

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_tail.append(line.strip())
            del stderr_tail[:-12]

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def receive(request_id: int, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                item = messages.get(timeout=remaining)
            except queue.Empty:
                return None
            if item is None:
                return None
            if isinstance(item, dict) and item.get("id") == request_id:
                return item

    try:
        send({"method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "ai-usage", "title": "AI Usage", "version": "1.0.0"},
            "capabilities": {"experimentalApi": True},
        }})
        initialized = receive(1, 5)
        if initialized is None:
            detail = "; ".join(stderr_tail[-4:])
            return {"error": f"Codex app-server did not complete initialization{': ' + detail if detail else '.'}"}
        if initialized.get("error"):
            error = initialized["error"]
            message = error.get("message", "initialization failed") if isinstance(error, dict) else str(error)
            return {"error": f"Codex app-server initialization failed: {message[:250]}"}
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 2, "params": {}})
        response = receive(2, 10)
        if response is None:
            detail = "; ".join(stderr_tail[-4:])
            return {"error": f"Codex app-server did not return quota data{': ' + detail if detail else '.'}"}
        if response.get("error"):
            error = response["error"]
            message = error.get("message", "quota request failed") if isinstance(error, dict) else str(error)
            return {"error": f"Codex app-server quota request failed: {message[:250]}"}
        payload = response.get("result")
        if not isinstance(payload, dict):
            return {"error": "Codex app-server returned an unexpected quota response."}
        return normalize_codex_quota(payload)
    except (BrokenPipeError, OSError) as e:
        return {"error": f"Codex app-server connection failed: {e}"}
    finally:
        if proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def normalize_codex_quota(payload: dict[str, Any]) -> dict[str, Any]:
    rate_limits = payload.get("rateLimits")
    if not isinstance(rate_limits, dict) or not rate_limits:
        by_id = payload.get("rateLimitsByLimitId")
        rate_limits = by_id.get("codex", {}) if isinstance(by_id, dict) else {}
    windows: list[dict[str, Any]] = []
    for key in ("primary", "secondary"):
        limit = rate_limits.get(key) if isinstance(rate_limits, dict) else None
        if not isinstance(limit, dict):
            continue
        used = limit.get("usedPercent")
        if not isinstance(used, (int, float)):
            continue
        minutes = limit.get("windowDurationMins")
        if isinstance(minutes, (int, float)):
            label = "5-hour" if minutes <= 360 else "Weekly" if minutes <= 10080 else "Monthly"
        else:
            label = "Session" if key == "primary" else "Weekly"
        reset_at = limit.get("resetsAt")
        if isinstance(reset_at, (int, float)):
            reset_at = dt.datetime.fromtimestamp(reset_at, dt.timezone.utc).isoformat()
        windows.append({
            "label": label,
            "usedPercent": used,
            "remainingPercent": max(0, 100 - used),
            "resetAt": reset_at,
            "windowDurationSeconds": int(minutes * 60) if isinstance(minutes, (int, float)) else None,
        })
    if not windows:
        return {"error": "Codex is signed in, but no plan quota windows were returned."}
    snapshot: dict[str, Any] = {"windows": windows}
    plan = rate_limits.get("planType") if isinstance(rate_limits, dict) else None
    if plan:
        snapshot["identity"] = {"plan": plan}
    credits = payload.get("rateLimitResetCredits")
    if isinstance(credits, dict) and credits.get("availableCount") is not None:
        snapshot["credits"] = {"remaining": credits["availableCount"], "unit": "resets"}
    return snapshot


def service_data(service: str, config: dict[str, Any]) -> dict[str, Any]:
    try:
        configured = run_configured(service, config)
    except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError) as e:
        configured = {"source": "configured command failed", "error": str(e)}
    if service == "cursor" and not configured and setting("CURSOR_SESSION_TOKEN"):
        data = cursor_data()
    else:
        data = configured or snapshot(service)
    if service == "claude" and data is None:
        data = claude_data()
    elif service == "codex" and data is None:
        data = codex_data()
    elif service == "cursor" and data is None:
        data = cursor_data()
    return data or {"source": "not configured", "status": "No automatic source or snapshot is configured."}


def collect(config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"generated_at": now().isoformat(timespec="minutes"), "services": {}}
    # Run local activity scans and provider quota requests concurrently.
    with ThreadPoolExecutor(max_workers=5) as pool:
        quota_futures = {
            "claude": pool.submit(claude_quota_data),
            "codex": pool.submit(codex_quota_data),
        }
        service_futures = {
            service: pool.submit(service_data, service, config)
            for service in ("claude", "codex", "cursor")
        }
        quota_snapshots = {service: future.result() for service, future in quota_futures.items()}
        service_results = {service: future.result() for service, future in service_futures.items()}
    for service in ("claude", "codex", "cursor"):
        data = service_results[service]
        if service in quota_snapshots:
            quota_snapshot = quota_snapshots[service]
            if quota_snapshot.get("quota", {}).get("windows"):
                data["quota"] = quota_snapshot["quota"]
                data["quota_source"] = quota_snapshot.get("source", "provider")
            elif quota_snapshot.get("windows"):
                data["quota"] = quota_snapshot
                data["quota_source"] = "Codex app-server"
            elif quota_snapshot.get("error"):
                data["quota_status"] = quota_snapshot["error"]
        result["services"][service] = data
    return result


def summarize(value: Any, depth: int = 0) -> list[str]:
    """Render useful fields from arbitrary ccusage JSON without assuming a fixed schema."""
    if depth > 3:
        return []
    if isinstance(value, dict):
        preferred = ("label", "date", "startTime", "endTime", "isActive", "totalTokens", "inputTokens", "outputTokens", "totalCost", "costUSD", "burnRate", "projection", "utilization", "resets_at")
        lines = []
        for key in preferred:
            if key in value and not isinstance(value[key], (dict, list)):
                lines.append(f"{key}: {value[key]}")
        if lines:
            return lines[:8]
        for key in ("totals", "data", "blocks", "weekly", "daily"):
            if key in value:
                found = summarize(value[key], depth + 1)
                if found:
                    return found
        return [json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:500]]
    if isinstance(value, list):
        if not value:
            return ["(no records)"]
        return summarize(value[-1], depth + 1)
    return [str(value)]


ACTIVITY_FIELDS = ("totalTokens", "inputTokens", "outputTokens", "totalCost", "totalCostUSD", "costUSD")


def activity_record(value: Any, sections: tuple[str, ...]) -> dict[str, Any] | None:
    """Select the newest report row from the supported ccusage JSON shapes."""
    def newest(rows: list[Any]) -> dict[str, Any] | None:
        records = [row for row in rows if isinstance(row, dict)]
        if not records:
            return None
        date_fields = ("startTime", "blockStart", "firstActivity", "week", "date")
        dated = [(next((str(row[key]) for key in date_fields if row.get(key)), ""), row) for row in records]
        dated = [(stamp, row) for stamp, row in dated if stamp]
        return max(dated, key=lambda item: item[0])[1] if dated else records[-1]

    if isinstance(value, list):
        return newest(value)
    if not isinstance(value, dict):
        return None
    for section in sections:
        child = value.get(section)
        if isinstance(child, list):
            row = newest(child)
            if row:
                return row
        if isinstance(child, dict):
            row = activity_record(child, sections)
            if row:
                return row
    if any(field in value for field in ACTIVITY_FIELDS):
        return value
    for key in ("summary", "totals", "data"):
        child = value.get(key)
        if isinstance(child, dict):
            row = activity_record(child, sections)
            if row:
                return row
    return None


def number(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def local_period(start: Any, end: Any) -> str | None:
    def parse(value: Any) -> dt.datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone()
        except ValueError:
            return None

    first, last = parse(start), parse(end)
    if first and last:
        return f"{first:%m-%d %H:%M}–{last:%H:%M}"
    if first:
        return first.strftime("%m-%d %H:%M")
    return None


def activity_line(label: str, record: dict[str, Any] | None) -> str | None:
    if not record:
        return None
    metrics = dict(record)
    nested = record.get("tokenCounts")
    if isinstance(nested, dict):
        metrics.update(nested)
    start = record.get("startTime") or record.get("blockStart") or record.get("firstActivity")
    end = record.get("endTime") or record.get("blockEnd") or record.get("lastActivity")
    period = local_period(start, end) if start else None
    if not period and record.get("week"):
        period = f"week of {record['week']}"
    elif not period and record.get("date"):
        period = str(record["date"])
    token_value = metrics.get("totalTokens")
    tokens = number(token_value)
    cost = next((metrics[key] for key in ("totalCostUSD", "totalCost", "costUSD") if isinstance(metrics.get(key), (int, float))), None)
    details: list[str] = []
    if period:
        details.append(period)
    if record.get("isActive") is True:
        details.append("active")
    elif record.get("isActive") is False:
        details.append("ended")
    if tokens:
        details.append(f"{tokens} tokens")
    if isinstance(cost, (int, float)):
        details.append(f"~${cost:,.2f} estimated")
    if not details:
        return None
    line = f"  {label}: " + " · ".join(details)
    input_tokens = number(metrics.get("inputTokens"))
    output_tokens = number(metrics.get("outputTokens"))
    if input_tokens or output_tokens:
        line += f" (in {input_tokens or '?'} / out {output_tokens or '?'})"
    if isinstance(token_value, (int, float)) and not isinstance(token_value, bool) and token_value >= 1_000_000_000:
        line += " ⚠ unusually large local-log total; verify ccusage output"
    return line


def progress_bar(remaining: float, width: int = 10) -> str:
    filled = max(0, min(width, round(width * remaining / 100)))
    return "█" * filled + "░" * (width - filled)


def colorize(value: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{value}\033[0m" if enabled else value


def remaining_color(remaining: float) -> str:
    return "32" if remaining >= 50 else "33" if remaining >= 20 else "31"


def duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    value = max(0, int(seconds))
    days, value = divmod(value, 86400)
    hours, value = divmod(value, 3600)
    minutes = value // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def reset_label(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        reset_at = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=dt.timezone.utc)
        local = reset_at.astimezone()
        seconds = (local - now()).total_seconds()
        return f"{duration(seconds)} / {local.strftime('%a %H:%M')}"
    except ValueError:
        return value


def parsed_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def pace_assessment(used_percent: Any, reset_at: Any, duration_seconds: Any) -> str | None:
    """Estimate whether the current average usage pace lasts to the reset."""
    if not isinstance(used_percent, (int, float)) or isinstance(used_percent, bool):
        return None
    reset = parsed_time(reset_at)
    if not reset or not isinstance(duration_seconds, (int, float)):
        return None
    seconds_to_reset = (reset - dt.datetime.now(dt.timezone.utc)).total_seconds()
    elapsed = duration_seconds - seconds_to_reset
    if seconds_to_reset <= 0 or elapsed < 60:
        return None
    used = max(0.0, min(100.0, float(used_percent)))
    if used == 0:
        return f"✓ Pace: no quota used yet; reset in {duration(seconds_to_reset)}"
    estimated_empty = max(0.0, (100 - used) * elapsed / used)
    if estimated_empty < seconds_to_reset:
        return f"⚠ Pace: could run out in {duration(estimated_empty)} at the current rate"
    return f"✓ Pace: likely enough through reset ({duration(seconds_to_reset)} left)"


def render_quota(row: dict[str, Any], color: bool = False) -> list[str]:
    lines: list[str] = []
    windows = row.get("windows", [])
    if not isinstance(windows, list):
        windows = []
    for window in windows:
        if not isinstance(window, dict) or window.get("idle"):
            continue
        label = window.get("label") or window.get("kind") or "Usage"
        used = window.get("usedPercent")
        remaining = window.get("remainingPercent")
        if not isinstance(remaining, (int, float)) and isinstance(used, (int, float)):
            remaining = 100 - used
        if isinstance(remaining, (int, float)):
            remaining = max(0, min(100, remaining))
            code = remaining_color(remaining)
            bar = colorize(progress_bar(remaining), code, color)
            pct = colorize(f"{remaining:.0f}% left", code, color)
            lines.append(f"    {label:<17} {bar}  {pct}")
        reset = reset_label(window.get("resetAt"))
        if reset:
            lines.append(f"      ↻ Reset in {reset}")
        pace = pace_assessment(used, window.get("resetAt"), window.get("windowDurationSeconds"))
        if pace:
            code = "31" if pace.startswith("⚠") else "32"
            lines.append(f"      {colorize(pace, code, color)}")

    pace = row.get("pace")
    if isinstance(pace, dict):
        for key, label in (("primary", "Pace"), ("secondary", "Weekly pace"), ("tertiary", "Other pace")):
            detail = pace.get(key)
            if not isinstance(detail, dict):
                continue
            will_last = detail.get("willLastToReset")
            eta = detail.get("etaSeconds")
            summary = detail.get("summary")
            if will_last is False and isinstance(eta, (int, float)):
                lines.append(f"      ⚠ {label}: projected empty in {duration(eta)}")
            elif will_last is True:
                lines.append(f"      ✓ {label}: pace is sufficient through reset")
            elif isinstance(summary, str) and summary:
                lines.append(f"      {label}: {summary}")
    for summary in row.get("paceSummaries", []):
        if isinstance(summary, str):
            lines.append(f"      Pace: {summary}")

    identity = row.get("identity")
    if isinstance(identity, dict) and identity.get("plan"):
        lines.insert(0, f"    Plan: {identity['plan']}")
    credits = row.get("credits")
    if isinstance(credits, dict) and credits.get("remaining") is not None:
        lines.append(f"    Resets available: {credits['remaining']}")
    return lines


def quota_summary(data: dict[str, Any], service: str) -> tuple[str, str]:
    quota = data.get("quota")
    windows = quota.get("windows", []) if isinstance(quota, dict) else []
    if isinstance(windows, list) and windows:
        remaining = [
            window.get("remainingPercent") if isinstance(window.get("remainingPercent"), (int, float))
            else 100 - window.get("usedPercent", 0)
            for window in windows
            if isinstance(window, dict) and isinstance(window.get("usedPercent"), (int, float))
        ]
        if remaining:
            lowest = min(remaining)
            state = "⚠ low" if lowest < 20 else "✓ available"
            return f"{state} · {lowest:.0f}% min left", "31" if lowest < 20 else "32"
    if service == "cursor":
        usage = data.get("usage", {})
        plan = usage.get("individualUsage", {}).get("plan", {}) if isinstance(usage, dict) else {}
        used = plan.get("totalPercentUsed") if isinstance(plan, dict) else None
        if isinstance(used, (int, float)):
            remaining = max(0, 100 - used)
            return f"{'⚠ low' if remaining < 20 else '✓ available'} · {remaining:.1f}% left", "31" if remaining < 20 else "32"
    if data.get("quota_status"):
        return "⚠ quota unavailable", "33"
    if data.get("error"):
        return "⚠ source unavailable", "33"
    return "activity only", "36"


def render(report: dict[str, Any], color: bool = False) -> str:
    stamp = dt.datetime.fromisoformat(report["generated_at"]).strftime("%Y-%m-%d %H:%M %Z")
    lines = [colorize("🤖 AI Usage", "1;37", color), f"   Updated {stamp}", colorize("─" * 58, "90", color), colorize("At a glance", "1", color)]
    titles = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"}
    for service, title in titles.items():
        status, code = quota_summary(report["services"][service], service)
        lines.append(f"  {title:<13} {colorize(status, code, color)}")
    for service, title in titles.items():
        data = report["services"][service]
        quota = data.get("quota")
        source = data.get("source", "unknown")
        if isinstance(quota, dict) and source == "not configured":
            source = data.get("quota_source", "provider quota")
        lines.extend(["", colorize("─" * 58, "90", color), colorize(title.upper(), "1;36", color), f"  Source: {source}"])
        if isinstance(quota, dict):
            lines.append(f"  PLAN · {data.get('quota_source', 'provider')}")
            lines.extend(render_quota(quota, color))
        elif data.get("quota_status"):
            lines.append(f"  PLAN · {colorize('Unavailable', '33', color)} — {data['quota_status']}")
        if data.get("error"):
            lines.append(f"  ⚠ {data['error']}")
            continue
        if service in ("claude", "codex") and data.get("source", "").startswith("ccusage"):
            lines.append("  ACTIVITY · local logs; separate from plan quota")
            if service == "claude":
                block = activity_record(data.get("blocks"), ("blocks", "data"))
                weekly = activity_record(data.get("weekly"), ("weekly", "data"))
                for label, record in (("Recent 5h", block), ("Weekly", weekly)):
                    line = activity_line(label, record)
                    if line:
                        lines.append(f"    {line.strip()}")
            else:
                daily = activity_record(data.get("daily"), ("daily", "data"))
                line = activity_line("Latest daily", daily)
                if line:
                    lines.append(f"    {line.strip()}")
            if data.get("captured_at"):
                lines.append(f"    Captured {data['captured_at']}")
            continue
        if data.get("status") and not quota:
            lines.append(f"  {data['status']}")
            continue
        if service == "cursor" and "usage" in data and not quota:
            usage = data["usage"]
            plan = usage.get("individualUsage", {}).get("plan", {})
            if plan:
                used = plan.get("used")
                limit = plan.get("limit")
                remaining = plan.get("remaining")
                lines.append("  INCLUDED USAGE")
                if used is not None and limit is not None:
                    lines.append(f"    Used {used} / {limit}" + (f" · {remaining} remaining" if remaining is not None else ""))
                total_percent = plan.get("totalPercentUsed")
                if isinstance(total_percent, (int, float)):
                    remaining_percent = max(0, min(100, 100 - total_percent))
                    code = remaining_color(remaining_percent)
                    remaining_label = colorize(f"{remaining_percent:.1f}% left", code, color)
                    lines.append(f"    {colorize(progress_bar(remaining_percent), code, color)}  {remaining_label}")
                lines.append("  MODEL POOLS")
                for name, key in (("Auto", "autoPercentUsed"), ("API / other", "apiPercentUsed")):
                    percent = plan.get(key)
                    if isinstance(percent, (int, float)):
                        pool_remaining = max(0, min(100, 100 - percent))
                        code = remaining_color(pool_remaining)
                        pool_label = colorize(f"{pool_remaining:.1f}% left", code, color)
                        lines.append(f"    {name:<12} {colorize(progress_bar(pool_remaining), code, color)}  {pool_label}")
                cycle_start = parsed_time(usage.get("billingCycleStart"))
                cycle_end = parsed_time(usage.get("billingCycleEnd"))
                if cycle_start and cycle_end:
                    cycle_seconds = (cycle_end - cycle_start).total_seconds()
                    pace = pace_assessment(plan.get("totalPercentUsed"), usage.get("billingCycleEnd"), cycle_seconds)
                    if pace:
                        lines.append(f"    {colorize(pace, '31' if pace.startswith('⚠') else '32', color)}")
                    local_start = cycle_start.astimezone()
                    local_end = cycle_end.astimezone()
                    until_reset = duration((cycle_end - dt.datetime.now(dt.timezone.utc)).total_seconds())
                    lines.append(f"    Cycle {local_start:%b} {local_start.day} → {local_end:%b} {local_end.day} · resets in {until_reset}")
            on_demand = usage.get("individualUsage", {}).get("onDemand", {})
            if on_demand.get("enabled"):
                lines.append(f"  ON-DEMAND · {on_demand.get('used', 'unknown')} dashboard units used")
            if data.get("captured_at"):
                lines.append(f"    Captured {data['captured_at']}")
            continue
        if service in ("claude", "codex") and "weekly" in data:
            if "blocks" in data:
                lines.append("  Recent block activity:")
                lines.extend(f"    {line}" for line in summarize(data["blocks"]))
            lines.append("  Weekly activity:")
            lines.extend(f"    {line}" for line in summarize(data["weekly"]))
        else:
            lines.extend(f"  {line}" for line in summarize(data.get("data", data)))
            if data.get("captured_at"):
                lines.append(f"  Captured: {data['captured_at']}")
    return "\n".join(lines)


def post_slack(message: str, config: dict[str, Any]) -> None:
    slack = config.get("slack", {})
    env_name = slack.get("webhook_url_env", "AI_USAGE_SLACK_WEBHOOK_URL")
    url = os.environ.get(env_name, "")
    if not url:
        env_file = APP_DIR / "environment"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith(env_name + "="):
                    url = line.split("=", 1)[1].strip().strip("\"'")
                    break
    if not url.startswith("https://hooks.slack.com/services/"):
        raise RuntimeError(f"Set {env_name} to a Slack Incoming Webhook URL")
    req = urllib.request.Request(url, data=json.dumps({"text": message}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.status >= 300:
                raise RuntimeError(f"Slack returned HTTP {response.status}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Slack delivery failed: {e}") from e


def create_agent() -> Path:
    home = Path.home()
    bin_path = home / ".local" / "bin" / "ai-usage"
    logs = home / "Library" / "Logs" / "ai-usage"
    logs.mkdir(parents=True, exist_ok=True)
    plist_path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    env = {}
    env_file = APP_DIR / "environment"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, val = line.split("=", 1)
                env[key.strip()] = val.strip().strip("\"'")
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(bin_path), "--slack"],
        "StartCalendarInterval": [{"Hour": h, "Minute": 0} for h in (9, 13, 17)],
        "EnvironmentVariables": env,
        "StandardOutPath": str(logs / "stdout.log"),
        "StandardErrorPath": str(logs / "stderr.log"),
    }
    plist_path.write_bytes(plistlib.dumps(payload))
    plist_path.chmod(0o600)
    return plist_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-usage", description="Local AI service usage digest")
    parser.add_argument("--slack", action="store_true", help="send the report to Slack")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("command", nargs="?", choices=("install-agent", "uninstall-agent", "snapshot"))
    parser.add_argument("service", nargs="?", choices=("claude", "codex", "cursor"))
    parser.add_argument("snapshot_file", nargs="?")
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        if not args.service or not args.snapshot_file:
            parser.error("snapshot requires a service and JSON file path")
        try:
            data = json.loads(Path(args.snapshot_file).expanduser().read_text())
            SNAPSHOTS.mkdir(parents=True, exist_ok=True)
            record = {"source": "manual snapshot", "captured_at": now().isoformat(timespec="minutes"), "data": data}
            (SNAPSHOTS / f"{args.service}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        except (OSError, json.JSONDecodeError) as e:
            print(f"ai-usage: {e}", file=sys.stderr)
            return 1
        print(f"Saved {args.service} snapshot")
        return 0
    if args.command == "install-agent":
        path = create_agent()
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=False)
        print(f"Installed scheduled job: {path}")
        return 0
    if args.command == "uninstall-agent":
        path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], check=False)
        path.unlink(missing_ok=True)
        print("Removed scheduled job")
        return 0
    config = load_config()
    report = collect(config)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        use_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
        message = render(report, color=use_color)
        print(message)
        if args.slack:
            try:
                post_slack(render(report), config)
                print("\nSent to Slack")
            except RuntimeError as e:
                print(f"\nai-usage: {e}", file=sys.stderr)
                return 1
    if args.json and args.slack:
        try:
            post_slack(render(report), config)
        except RuntimeError as e:
            print(f"ai-usage: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
