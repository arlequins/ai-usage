#!/usr/bin/env python3
"""Local usage digest for Claude Code, Codex, and Cursor."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import os
import plistlib
import shutil
import subprocess
import sys
import urllib.error
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
        blocks = command_json(["ccusage", "claude", "blocks", "--json"])
        weekly = command_json(["ccusage", "claude", "weekly", "--json"])
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


def cursor_data() -> dict[str, Any]:
    session_token = setting("CURSOR_SESSION_TOKEN")
    if not session_token:
        return {
            "source": "not configured",
            "status": "Cursor User API keys are for Cloud Agents, not IDE plan usage. Enable Cursor in CodexBar or configure a signed-in dashboard session.",
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
            return {"source": "Cursor dashboard session (unofficial)", "error": "Cursor rejected the session token. Copy a fresh WorkosCursorSessionToken from the signed-in dashboard."}
        return {"source": "Cursor dashboard session (unofficial)", "error": f"Cursor usage request failed: HTTP {e.code} {e.reason}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return {"source": "Cursor dashboard session (unofficial)", "error": f"Cursor usage request failed: {e}"}

    return {
        "source": "Cursor dashboard session (unofficial; IDE plan usage)",
        "captured_at": now().isoformat(timespec="minutes"),
        "usage": summary,
    }


def codexbar_executable() -> str | None:
    candidates = [
        shutil.which("codexbar"),
        "/opt/homebrew/bin/codexbar",
        "/usr/local/bin/codexbar",
        "/Applications/CodexBar.app/Contents/Helpers/CodexBarCLI",
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def codexbar_data() -> dict[str, Any] | None:
    """Read quota windows and pacing from CodexBar when it is installed."""
    executable = codexbar_executable()
    if not executable:
        return None
    try:
        return command_json([executable, "dashboard", "--identity", "redacted"], timeout=15)
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as e:
        return {"error": str(e)}


def quota_rows(snapshot: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("providers"), list):
        return {}
    return {
        row["id"]: row
        for row in snapshot["providers"]
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }


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


def has_quota_data(row: dict[str, Any]) -> bool:
    windows = row.get("windows")
    return bool(
        (isinstance(windows, list) and any(isinstance(window, dict) and not window.get("idle") for window in windows))
        or row.get("pace")
        or row.get("credits")
    )


def collect(config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"generated_at": now().isoformat(timespec="minutes"), "services": {}}
    # The providers are independent. Run local log scans and the optional quota
    # snapshot concurrently so one slow source does not add to every other wait.
    with ThreadPoolExecutor(max_workers=4) as pool:
        quota_future = pool.submit(codexbar_data)
        service_futures = {
            service: pool.submit(service_data, service, config)
            for service in ("claude", "codex", "cursor")
        }
        quota_snapshot = quota_future.result()
        service_results = {service: future.result() for service, future in service_futures.items()}
    quotas = quota_rows(quota_snapshot)
    for service in ("claude", "codex", "cursor"):
        data = service_results[service]
        if service in quotas and has_quota_data(quotas[service]):
            data["quota"] = quotas[service]
            data["quota_source"] = "CodexBar"
        elif isinstance(quota_snapshot, dict) and quota_snapshot.get("error"):
            data["quota_status"] = f"CodexBar quota read failed: {quota_snapshot['error']}"
        elif quota_snapshot is None:
            data["quota_status"] = "CodexBar CLI is not installed; plan limits and pace forecasts are unavailable."
        else:
            data["quota_status"] = "No quota data returned. Enable and sign in to this provider in CodexBar."
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


def progress_bar(remaining: float, width: int = 10) -> str:
    filled = max(0, min(width, round(width * remaining / 100)))
    return "█" * filled + "░" * (width - filled)


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


def render_quota(row: dict[str, Any]) -> list[str]:
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
            lines.append(f"  {label:<10} {progress_bar(remaining)}  {remaining:.0f}% remaining")
        reset = reset_label(window.get("resetAt"))
        if reset:
            lines.append(f"  Reset      {reset}")

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
                lines.append(f"  ⚠ {label}: projected empty in {duration(eta)}")
            elif will_last is True:
                lines.append(f"  ✓ {label}: pace is sufficient through reset")
            elif isinstance(summary, str) and summary:
                lines.append(f"  {label}: {summary}")
    for summary in row.get("paceSummaries", []):
        if isinstance(summary, str):
            lines.append(f"  Pace: {summary}")

    identity = row.get("identity")
    if isinstance(identity, dict) and identity.get("plan"):
        lines.insert(0, f"  Plan       {identity['plan']}")
    credits = row.get("credits")
    if isinstance(credits, dict) and credits.get("remaining") is not None:
        unit = credits.get("unit", "credits")
        lines.append(f"  Credits    {credits['remaining']} {unit} remaining")
    return lines


def render(report: dict[str, Any]) -> str:
    stamp = dt.datetime.fromisoformat(report["generated_at"]).strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"🤖 AI Usage — {stamp}"]
    titles = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"}
    for service, title in titles.items():
        data = report["services"][service]
        quota = data.get("quota")
        source = data.get("source", "unknown")
        if isinstance(quota, dict) and source == "not configured":
            source = "CodexBar quota"
        lines.append(f"\n{title} · {source}")
        if isinstance(quota, dict):
            lines.append("  Quota and pace · CodexBar")
            lines.extend(render_quota(quota))
        elif data.get("quota_status"):
            lines.append(f"  Quota: {data['quota_status']}")
        if data.get("error"):
            lines.append(f"  ⚠ {data['error']}")
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
                if used is not None and limit is not None:
                    lines.append(f"  Plan usage: {used} / {limit}")
                if remaining is not None:
                    lines.append(f"  Remaining: {remaining}")
                for name, key in (("Auto / Cursor models", "autoPercentUsed"), ("API / other models", "apiPercentUsed"), ("Total", "totalPercentUsed")):
                    percent = plan.get(key)
                    if isinstance(percent, (int, float)):
                        lines.append(f"  {name}: {100 - percent:.1f}% remaining")
            if usage.get("billingCycleStart") or usage.get("billingCycleEnd"):
                lines.append(f"  Billing cycle: {usage.get('billingCycleStart', '?')} → {usage.get('billingCycleEnd', '?')}")
            on_demand = usage.get("individualUsage", {}).get("onDemand", {})
            if on_demand.get("enabled"):
                lines.append(f"  On-demand used: {on_demand.get('used', 'unknown')} (dashboard units)")
            if data.get("captured_at"):
                lines.append(f"  Captured: {data['captured_at']}")
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
        message = render(report)
        print(message)
        if args.slack:
            try:
                post_slack(message, config)
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
