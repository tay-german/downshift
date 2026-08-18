#!/usr/bin/env python3
"""Quanta quota resta in ogni pool. Stdlib.

Codex la scrive davvero nei log di sessione. Claude no: le issue 35992 e 44328 chiedono
proprio quell'endpoint. Quindi Claude si legge a mano da /usage e si tiene qui, con la
sua eta': un numero vecchio va mostrato come vecchio, non spacciato per fresco.

  quota.py                      -> JSON con tutti i pool
  quota.py --for-route          -> "claude=0.40,codex=0.02" da passare a route.py
  quota.py --set claude=0.40    -> registra il valore letto in /usage
"""
import json, re, sys, time
from pathlib import Path

STATE = Path.home() / ".downshift-quota.json"
STALE_HOURS = 3


def codex_pool():
    """rate_limits nell'ultimo rollout di sessione. Fonte reale, non stimata."""
    root = Path.home() / ".codex" / "sessions"
    files = sorted(root.rglob("rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    for path in files[:5]:
        hits = re.findall(r'"rate_limits":\s*(\{.*?"credits")', path.read_text(errors="ignore"))
        if not hits:
            continue
        raw = json.loads(hits[-1].rsplit(',"credits', 1)[0] + "}")
        primary = raw.get("primary") or {}
        if "used_percent" not in primary:
            continue
        return {"used": round(primary["used_percent"] / 100, 4),
                "window_minutes": primary.get("window_minutes"),
                "resets_at": primary.get("resets_at"),
                "source": f"reale: {path.name}"}
    return {"used": None, "source": "nessun rate_limits nei log recenti"}


def manual_pool(name):
    try:
        entry = json.loads(STATE.read_text())[name]
        used, at = float(entry["used"]), float(entry["at"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"used": None, "source": "n/d — leggilo da /usage e registra con quota.py --set"}
    entry = {"used": used, "at": at}
    age_h = (time.time() - entry["at"]) / 3600
    return {"used": entry["used"], "age_hours": round(age_h, 1),
            "stale": age_h > STALE_HOURS,
            "source": f"manuale, {age_h:.1f}h fa" + (" — VECCHIO, rileggi /usage" if age_h > STALE_HOURS else "")}


def snapshot():
    return {"claude": manual_pool("claude"), "codex": codex_pool(),
            "opencode": {"used": 0.0, "source": "modelli FREE, nessuna quota"}}


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--set":
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        for part in args[1:]:
            name, _, value = part.partition("=")
            v = float(value)
            state[name] = {"used": v / 100 if v > 1 else v, "at": time.time()}
        STATE.write_text(json.dumps(state, indent=2))
        print(f"registrato: {', '.join(args[1:])}")
    elif args and args[0] == "--for-route":
        # Un dato vecchio non entra nel routing: meglio "ignoto" (neutro) che un numero
        # arbitrariamente stantio spacciato per la quota di adesso.
        fresh = {k: v for k, v in snapshot().items()
                 if v["used"] is not None and not v.get("stale")}
        print(",".join(f"{k}={v['used']:.2f}" for k, v in fresh.items()))
    else:
        print(json.dumps(snapshot(), indent=2, ensure_ascii=False))
