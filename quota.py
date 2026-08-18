#!/usr/bin/env python3
"""How much quota is left in each pool. Stdlib only.

Codex really does write it into its session logs. Claude does not — issues 35992 and
44328 ask for exactly that endpoint — so Claude is read by hand from /usage and kept
here with its age: an old number is shown as old, never passed off as current.

  quota.py                      -> JSON for every pool
  quota.py --for-route          -> "claude=0.40,codex=0.02" to hand to route.py
  quota.py --set claude=0.40    -> record what /usage showed you
"""
import json, os, re, sys, tempfile, time
from pathlib import Path

STATE = Path.home() / ".downshift-quota.json"
STALE_HOURS = 3


def codex_pool():
    """rate_limits from the latest session rollout. Measured, not estimated."""
    root = Path.home() / ".codex" / "sessions"
    dated = []
    for path in root.rglob("rollout-*.jsonl"):
        try:                                     # un file puo' sparire durante la scansione
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(dated, reverse=True)[:5]:
        try:
            hits = re.findall(r'"rate_limits":\s*(\{.*?"credits")', path.read_text(errors="ignore"))
            if not hits:
                continue
            raw = json.loads(hits[-1].rsplit(',"credits', 1)[0] + "}")
        except (OSError, ValueError):
            continue
        primary = raw.get("primary") if isinstance(raw, dict) else None
        if not isinstance(primary, dict) or not isinstance(primary.get("used_percent"), (int, float)):
            continue
        return {"used": round(primary["used_percent"] / 100, 4),
                "window_minutes": primary.get("window_minutes"),
                "resets_at": primary.get("resets_at"),
                "source": f"measured: {path.name}"}
    return {"used": None, "source": "no rate_limits in the recent logs"}


def manual_pool(name):
    try:
        entry = json.loads(STATE.read_text())[name]
        used, at = float(entry["used"]), float(entry["at"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"used": None, "source": "n/a — read it from /usage and record it with quota.py --set"}
    entry = {"used": used, "at": at}
    age_h = (time.time() - entry["at"]) / 3600
    return {"used": entry["used"], "age_hours": round(age_h, 1),
            "stale": age_h > STALE_HOURS,
            "source": f"manual, {age_h:.1f}h ago" + (" — STALE, re-read /usage" if age_h > STALE_HOURS else "")}


def snapshot():
    return {"claude": manual_pool("claude"), "codex": codex_pool(),
            "opencode": {"used": 0.0, "source": "FREE models, no quota to speak of"}}


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--set":
        try:
            state = json.loads(STATE.read_text()) if STATE.exists() else {}
            if not isinstance(state, dict):
                state = {}
        except ValueError:
            state = {}
        for part in args[1:]:
            name, sep, value = part.partition("=")
            if not sep:
                raise SystemExit(f"--set: '{part}' has no value. Expected form: claude=40")
            try:
                v = float(value)
            except ValueError:
                raise SystemExit(f"--set: '{value}' is not a number")
            v = v / 100 if v > 1 else v
            if not 0 <= v <= 1:
                raise SystemExit(f"--set {name}: {value} is out of range (0-100%)")
            state[name.strip()] = {"used": v, "at": time.time()}
        fd, temp = tempfile.mkstemp(dir=str(STATE.parent), prefix=".quota.")
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2)
        os.replace(temp, STATE)
        print(f"recorded: {', '.join(args[1:])}")
    elif args and args[0] == "--for-route":
        # Stale readings stay out of routing: "unknown" (neutral) beats an arbitrarily
        # old number presented as the quota right now.
        fresh = {k: v for k, v in snapshot().items()
                 if v["used"] is not None and not v.get("stale")}
        print(",".join(f"{k}={v['used']:.2f}" for k, v in fresh.items()))
    else:
        print(json.dumps(snapshot(), indent=2, ensure_ascii=False))
