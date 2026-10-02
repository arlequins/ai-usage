#!/usr/bin/env python3
"""Local usage digest for Claude Code, Codex, and Cursor."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import plistlib
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
    api_key = setting("CURSOR_API_KEY") or setting("CURSOR_ADMIN_API_KEY")
    if not api_key:
        return {"source": "not configured", "status": "Set CURSOR_API_KEY to enable Cursor User API."}

    def get_json(path: str) -> Any:
        req = urllib.request.Request(
            f"https://api.cursor.com{path}",
            headers={"Authorization": f"Basic {base64.b64encode(f'{api_key}:'.encode()).decode()}"},
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode())

    try:
        identity = get_json("/v1/me")
        agents = get_json("/v1/agents?limit=20&includeArchived=true")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"source": "Cursor User API", "error": "Cursor rejected the API key. Check that it is the User API key shown in Cursor Dashboard → API & SSH Keys."}
        return {"source": "Cursor User API", "error": f"Cursor API request failed: HTTP {e.code} {e.reason}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return {"source": "Cursor User API", "error": f"Cursor API request failed: {e}"}

    totals = {"inputTokens": 0, "outputTokens": 0, "cacheWriteTokens": 0, "cacheReadTokens": 0, "totalTokens": 0}
    scanned = 0
    failed = 0
    for agent in agents.get("items", []):
        try:
            usage = get_json(f"/v1/agents/{agent['id']}/usage").get("totalUsage", {})
            for field in totals:
                totals[field] += usage.get(field, 0) or 0
            scanned += 1
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, KeyError):
            failed += 1
    return {
        "source": "Cursor User API · Cloud Agent usage (latest 20 agents; not IDE plan usage)",
        "captured_at": now().isoformat(timespec="minutes"),
        "usage": {
            "email": identity.get("userEmail", "unknown"),
            "agents_scanned": scanned,
            "more_agents_available": bool(agents.get("nextCursor")),
            "agents_failed": failed,
            **totals,
        },
    }


def collect(config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"generated_at": now().isoformat(timespec="minutes"), "services": {}}
    for service in ("claude", "codex", "cursor"):
        try:
            configured = run_configured(service, config)
        except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError) as e:
            configured = {"source": "configured command failed", "error": str(e)}
        if service == "cursor" and not configured and (setting("CURSOR_API_KEY") or setting("CURSOR_ADMIN_API_KEY")):
            data = cursor_data()
        else:
            data = configured or snapshot(service)
        if service == "claude" and data is None:
            data = claude_data()
        if service == "codex" and data is None:
            data = codex_data()
        if service == "cursor" and data is None:
            data = cursor_data()
        if data is None:
            data = {"source": "not configured", "status": "No automatic source or snapshot is configured."}
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


def render(report: dict[str, Any]) -> str:
    stamp = dt.datetime.fromisoformat(report["generated_at"]).strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"🤖 AI Usage — {stamp}"]
    titles = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"}
    for service, title in titles.items():
        data = report["services"][service]
        lines.append(f"\n{title} · {data.get('source', 'unknown')}")
        if data.get("error"):
            lines.append(f"  ⚠ {data['error']}")
            continue
        if data.get("status"):
            lines.append(f"  {data['status']}")
            continue
        if service == "cursor" and "usage" in data:
            usage = data["usage"]
            lines.append(f"  Account: {usage['email']}")
            lines.append(f"  Cloud agents scanned: {usage['agents_scanned']}")
            lines.append(f"  Input tokens: {usage['inputTokens']}")
            lines.append(f"  Output tokens: {usage['outputTokens']}")
            lines.append(f"  Cache tokens: {usage['cacheWriteTokens'] + usage['cacheReadTokens']}")
            lines.append(f"  Total tokens: {usage['totalTokens']}")
            if usage.get("more_agents_available"):
                lines.append("  Note: Only the 20 newest agents are included.")
            if usage.get("agents_failed"):
                lines.append(f"  Agents with unavailable usage: {usage['agents_failed']}")
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
