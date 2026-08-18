#!/usr/bin/env python3
"""downshift — pick the cheapest executor that the task's own verifiability allows.

Any harness (Claude Code, Codex CLI, OpenCode) can be the orchestrator. It scores
three axes, calls this, and gets back one executor plus a context budget.
Stdlib only. `python3 route.py selftest` runs the checks.
"""
import argparse, json, sys
from pathlib import Path

MATRIX = Path(__file__).with_name("routing.json")
TIERS = ["T0", "T1", "T2", "T3", "T4", "T5"]
_CACHE = None


def load_matrix():
    """Read routing.json once. Callers route thousands of times per session."""
    global _CACHE
    if _CACHE is None:
        m = json.loads(MATRIX.read_text())
        for tier, pool in m["executors"].items():
            for e in pool:
                missing = [k for k in ("family", "harness", "model", "pool", "weight") if k not in e]
                if missing:
                    raise SystemExit(
                        f"routing.json: executor '{e.get('model', '?')}' at {tier} is missing "
                        f"{', '.join(missing)}. Add models with `route.py add`, which fills every field.")
        _CACHE = m
    return _CACHE


def route(hard=None, spec=None, check=None, files=1, critical=False, orchestrator="claude",
          attempt=0, matrix=None, task_class=None, pool_used=None):
    m = matrix or load_matrix()
    p = m["policy"]
    pool_used = pool_used or {}
    why = []

    if task_class:
        c = m["task_classes"][task_class]
        hard = c["hard"] if hard is None else hard
        spec = c["spec"] if spec is None else spec
        check = c["check"] if check is None else check
        critical = critical or c.get("critical", False)
        why.append(f"classe {task_class} ({c['name']}): {c['what']}")
    if hard is None or spec is None or check is None:
        raise ValueError("give --class, or all three of --hard --spec --check")
    for name, value in (("hard", hard), ("spec", spec), ("check", check)):
        if not 0 <= value <= 5:
            raise ValueError(f"{name}={value}: the axes run from 0 to 5")
    if files < 0:
        raise ValueError(f"files={files}: cannot be negative")
    for name, value in pool_used.items():
        if not 0 <= value <= 1:
            raise ValueError(f"pool '{name}' usage = {value}: it runs from 0 to 1 "
                             "(0.4 for 40%, not 40)")

    i = max(0, min(5, hard))
    why.append(f"hard={hard} -> {TIERS[i]}")
    if files >= p["files_bump_at"]:
        i += 1
        why.append(f"{files} files >= {p['files_bump_at']} -> +1 tier")
    if check >= p["downshift_check_min"] and not critical:
        i -= 1
        why.append(f"check={check}: result is cheap to verify -> downshift 1 tier")
    if critical:
        i = max(i, TIERS.index(p["critical_min_tier"]))
        why.append(f"critical -> floor {p['critical_min_tier']}, independent second opinion required")

    blocked = None
    if attempt < 0:
        raise ValueError(f"attempt={attempt}: cannot be negative")
    up = max(0, min(attempt, p["max_upshifts"]))
    if up:
        i += up
        why.append(f"attempt {attempt}: upshift {up} (carry the failure evidence, do not restart)")
    if attempt > p["max_upshifts"]:
        blocked = (f"{p['max_upshifts']} escalations already spent without a result: "
                   "this needs a human decision, not another attempt")
        why.append(blocked)

    i = max(0, min(5, i))
    tier = TIERS[i]

    judgement, execution = p["judgement_family"], p["execution_family"]
    if spec >= p["spec_execution_min"]:
        family, fwhy = execution, f"spec={spec}: target is fully specified -> execution family"
    elif spec <= p["spec_judgement_max"]:
        family, fwhy = judgement, f"spec={spec}: target is ambiguous -> judgement family"
    else:
        family = judgement if hard >= 4 else execution
        fwhy = f"spec={spec} is borderline, hard={hard} decides -> {family}"
    why.append(fwhy)

    def preference(e):
        # Most headroom first (unknown usage counts as 0.5 — neither free nor full);
        # on a tie, the model that bites less of its own pool.
        return (1.0 - pool_used.get(e["pool"], 0.5), -e["weight"])

    candidates = [e for e in m["executors"][tier] if e["family"] == family]
    if not candidates and family != p["judgement_family"]:
        # A fully specified task may go to whoever sits at that tier.
        candidates = m["executors"][tier]
        if candidates:
            why.append(f"no {family} executor at {tier}; the task is specified enough")
    if candidates:
        ex = max(candidates, key=preference)
        if len(candidates) > 1:
            why.append(f"{len(candidates)} equivalent models at {tier}: picked pool '{ex['pool']}' "
                       f"because it has the most quota headroom")
    else:
        # Ambiguity is the one thing never substituted away: if the judgement family
        # is missing at this tier, climb until it appears.
        ex = None
        for j in range(i + 1, 6):
            up = [e for e in m["executors"][TIERS[j]] if e["family"] == family]
            if up:
                ex = max(up, key=preference)
                why.append(f"no {family} executor at {tier} and the task is ambiguous -> {TIERS[j]}")
                i, tier = j, TIERS[j]
                break
        if ex is None:
            for j in list(range(i, 6)) + list(range(i - 1, -1, -1)):
                if m["executors"][TIERS[j]]:
                    ex = max(m["executors"][TIERS[j]], key=preference)
                    i, tier = j, TIERS[j]
                    why.append(f"no {family} executor anywhere: falling back to {tier}")
                    break
        if ex is None:
            raise SystemExit("routing.json has no executors at all: "
                             "add one with `route.py add`.")

    ex, tier, i = _respect_pool_reserve(m, ex, tier, i, family, task_class, pool_used, why, p)

    reviewer = None
    if critical:
        # A critical task must always get a second opinion from a different family.
        # Search from the top tier down, not just this tier: a tier that happens to hold
        # only one family must never silently leave the task unreviewed.
        for j in range(5, -1, -1):
            reviewer = next((e for e in m["executors"][TIERS[j]] if e["family"] != ex["family"]), None)
            if reviewer:
                break
        if reviewer is None:
            blocked = (f"critical task, but every registered executor is family '{ex['family']}': "
                       "no independent second opinion is possible. Register an executor "
                       "of another family before going ahead.")
            why.append(blocked)

    orch_i = TIERS.index(m["orchestrator_tier"].get(orchestrator, "T5"))
    delegate_decision = orch_i < i
    if delegate_decision:
        why.append(
            f"orchestrator '{orchestrator}' sits at {TIERS[orch_i]} but the task needs {tier}: "
            "it must hand over the judgement too, not just the work")

    t = m["tiers"][tier]
    return {
        "tier": tier, "tier_label": t["label"], "executor": ex, "reviewer": reviewer,
        "pool": ex["pool"], "task_class": task_class, "blocked": blocked,
        "context_budget_tokens": t["context_budget"], "context_allow": t["allow"],
        "never_send": p["never_send"],
        "worker_may_write": not critical,
        "orchestrator_must_delegate_decision": delegate_decision,
        "next_on_failure": None if attempt >= p["max_upshifts"] else f"re-run with --attempt {attempt + 1}",
        "why": why,
    }


def _respect_pool_reserve(m, ex, tier, i, family, task_class, pool_used, why, p):
    """Move work off a pool before it runs out, not after it 429s.

    Each pool keeps a reserve above a threshold: past it, only the task classes that
    pool is genuinely needed for may draw on it. Everything else moves to a pool with
    headroom, at the same tier or higher — never lower, capability is not negotiable.
    """
    pool = ex["pool"]
    info = m["pools"].get(pool, {})
    used = pool_used.get(pool)
    if used is None:
        return ex, tier, i
    exhausted = used >= p.get("pool_exhausted_at", 0.95)
    reserved = task_class in info.get("reserve_for", [])
    if not exhausted and (used < info.get("reserve_above", 1.01) or reserved):
        return ex, tier, i

    for j in range(i, 6):
        for cand in m["executors"][TIERS[j]]:
            if cand["pool"] == pool:
                continue
            if family == p["judgement_family"] and cand["family"] != family:
                continue
            cand_used = pool_used.get(cand["pool"])
            if cand_used is not None and cand_used >= p.get("pool_exhausted_at", 0.95):
                continue
            reason = "exhausted" if exhausted else f"past its {info.get('reserve_above'):.0%} reserve"
            why.append(f"pool '{pool}' {reason} ({used:.0%} used) and this class is not one it reserves for "
                       f"-> moving to '{cand['pool']}' at {TIERS[j]}")
            return cand, TIERS[j], j

    why.append(f"WARNING: pool '{pool}' at {used:.0%} and no other pool covers this work. "
               "Wait for the reset or register another executor.")
    return ex, tier, i


def selftest():
    r = route(hard=1, spec=5, check=5, files=1)
    assert r["tier"] == "T0", r["tier"]                      # trivial + verifiable -> free
    r = route(hard=3, spec=5, check=5, files=1)
    assert r["tier"] == "T2" and r["executor"]["family"] == "codex", r
    r = route(hard=3, spec=5, check=0, files=1)
    assert r["tier"] == "T3", r["tier"]                      # same task, no cheap check -> no downshift
    r = route(hard=2, spec=0, check=1, files=1)
    assert r["executor"]["family"] == "claude", r            # ambiguous -> judgement family
    r = route(hard=1, spec=5, check=5, critical=True)
    assert r["tier"] == "T4" and r["reviewer"] and not r["worker_may_write"], r
    assert r["reviewer"]["family"] != r["executor"]["family"], r
    for h in range(6):                                       # critical always gets a second family
        r = route(hard=h, spec=5, check=0, critical=True)
        assert r["reviewer"] and r["reviewer"]["family"] != r["executor"]["family"], (h, r)
    m = load_matrix()                                        # ogni executor deve essere completo
    for tier, pool in m["executors"].items():
        for e in pool:
            assert {"family", "harness", "model", "pool", "weight"} <= set(e), (tier, e)
    for bad in (dict(hard=9, spec=1, check=1), dict(hard=1, spec=-1, check=1),
                dict(task_class="S1", pool_used={"claude": 40})):
        try:
            route(**bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"nonsense input accepted: {bad}")
    r = route(task_class="S2")                               # a class fills the axes
    assert r["tier"] == "T0", r["tier"]
    r = route(task_class="C2")
    assert r["reviewer"] and not r["worker_may_write"], r
    # reserve: with Claude nearly full an M1 leaves the Claude pool, a C1 does not
    full = {"claude": 0.9, "codex": 0.1}
    assert route(task_class="M1", pool_used=full)["executor"]["pool"] != "claude"
    assert route(task_class="C1", pool_used=full)["executor"]["pool"] == "claude"
    # genuinely exhausted: it moves anyway, if there is anywhere to move
    assert route(task_class="S3", pool_used={"codex": 0.99})["executor"]["pool"] != "codex"
    assert route(task_class="S1", attempt=3)["blocked"], "3 attempts must block"
    assert route(task_class="S1")["blocked"] is None
    solo_claude = {**load_matrix(), "executors": {t: [e for e in load_matrix()["executors"][t]
                                                     if e["family"] == "claude"] for t in TIERS}}
    r = route(task_class="C2", matrix=solo_claude)
    assert r["blocked"] and r["reviewer"] is None, "critical without a second family must block"
    r = route(hard=5, spec=2, check=0, orchestrator="opencode")
    assert r["orchestrator_must_delegate_decision"], r
    r = route(hard=5, spec=2, check=0, orchestrator="claude")
    assert not r["orchestrator_must_delegate_decision"], r
    r = route(hard=2, spec=5, check=5, files=1, attempt=9)
    assert r["next_on_failure"] is None and r["tier"] == "T3", r  # capped at 2 upshifts
    print("selftest ok")


def _save(m):
    global _CACHE
    MATRIX.write_text(json.dumps(m, indent=2) + "\n")
    _CACHE = m


def cmd_list(m):
    print(f"{'tier':5} {'pool':9} {'family':9} {'model':26} {'effort':7} peso")
    for t in TIERS:
        for e in m["executors"][t]:
            print(f"{t:5} {e['pool']:9} {e['family']:9} {e['model']:26} {e.get('effort','-'):7} {e['weight']}")
    print()
    for name, c in m["task_classes"].items():
        print(f"  {name} {c['name']:11} {c['what']}")
    print()
    for name, info in m["pools"].items():
        print(f"  pool {name:9} reserved above {info['reserve_above']:.0%} for {', '.join(info['reserve_for']) or '-'}"
              f"  | quota: {info['quota_source']}")
    print("\norchestrators:", ", ".join(f"{k}={v}" for k, v in m["orchestrator_tier"].items()))
    print("judgement family (never substituted):", m["policy"]["judgement_family"])


def cmd_add(m, a):
    e = {"family": a.family, "harness": a.harness, "model": a.model,
         "pool": a.pool or a.harness, "weight": a.weight}
    if a.effort:
        e["effort"] = a.effort
    m["executors"][a.tier] = [x for x in m["executors"][a.tier] if x["model"] != a.model]
    m["executors"][a.tier].append(e)
    if a.orchestrator:
        m["orchestrator_tier"][a.harness] = a.tier
    _save(m)
    print(f"added {a.model} at {a.tier} (family {a.family}, pool {e['pool']}, weight {a.weight})")
    if e["pool"] not in m["pools"]:
        m["pools"][e["pool"]] = {"window": "fill this in", "quota_source": "n/a",
                                 "reserve_above": 1.01, "reserve_for": [],
                                 "reserve_why": "no reserve until you configure one"}
        _save(m)
        print(f"created pool '{e['pool']}': no reserve until you configure one in routing.json")
    if a.orchestrator:
        print(f"'{a.harness}' can now be used as --orchestrator, sitting at {a.tier}")


def cmd_remove(m, model):
    hit = False
    for t in TIERS:
        before = len(m["executors"][t])
        m["executors"][t] = [e for e in m["executors"][t] if e["model"] != model]
        hit = hit or len(m["executors"][t]) != before
    if not hit:
        raise SystemExit(f"no executor named {model}")
    live = {e["harness"] for t in TIERS for e in m["executors"][t]}
    for gone in [h for h in m["orchestrator_tier"] if h not in live]:
        del m["orchestrator_tier"][gone]
        print(f"'{gone}' has no executors left; unregistered as an orchestrator")
    live_pools = {e["pool"] for t in TIERS for e in m["executors"][t]}
    for gone in [name for name in m["pools"] if name not in live_pools]:
        del m["pools"][gone]
        print(f"pool '{gone}' has no executors left; removed")
    _save(m)
    print(f"removed {model}")


if __name__ == "__main__":
    argv, m = sys.argv[1:], load_matrix()
    cmd = argv[0] if argv else ""

    if cmd == "selftest":
        selftest(); raise SystemExit(0)
    if cmd == "list":
        cmd_list(m); raise SystemExit(0)
    if cmd == "remove":
        if len(argv) < 2:
            raise SystemExit("usage: route.py remove <model>   (route.py list shows them)")
        cmd_remove(m, argv[1]); raise SystemExit(0)
    if cmd == "add":
        ap = argparse.ArgumentParser(prog="route.py add",
                                     description="add or replace an executor; works for any CLI or local endpoint")
        ap.add_argument("--tier", required=True, choices=TIERS)
        ap.add_argument("--family", required=True,
                        help=f"'{m['policy']['judgement_family']}' for models you trust with ambiguity, "
                             "anything else for models you only trust with a specified target")
        ap.add_argument("--harness", required=True, help="the CLI that runs it: claude, codex, opencode, ollama, ...")
        ap.add_argument("--model", required=True)
        ap.add_argument("--effort", default=None)
        ap.add_argument("--pool", default=None,
                        help="the subscription it draws on; defaults to the harness name")
        ap.add_argument("--weight", type=float, default=1.0,
                        help="how hard it bites its pool's quota, relative")
        ap.add_argument("--orchestrator", action="store_true",
                        help="also register this harness as one that can orchestrate, at this tier")
        cmd_add(m, ap.parse_args(argv[1:])); raise SystemExit(0)

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 epilog="other commands: list | add | remove <model> | selftest")
    ap.add_argument("--task", default="", help="the task, for your own logs")
    ap.add_argument("--class", dest="task_class", choices=list(m["task_classes"]),
                    help="task class; fills the axes (route.py list defines them)")
    ap.add_argument("--hard", type=int, help="0-5 reasoning depth needed")
    ap.add_argument("--spec", type=int, help="0-5 how completely the target is specified")
    ap.add_argument("--check", type=int, help="0-5 how cheaply the result can be verified")
    ap.add_argument("--pool-used", dest="pool_used", default="",
                    help="quota already used per pool, e.g. claude=0.7,codex=0.02 (quota.py computes it)")
    ap.add_argument("--files", type=int, default=1)
    ap.add_argument("--critical", action="store_true", help="auth, payments, data loss, migration, infra, prod")
    ap.add_argument("--orchestrator", default="claude", help="the harness you launched: " + ", ".join(m["orchestrator_tier"]))
    ap.add_argument("--attempt", type=int, default=0, help="0 first try, 1..2 after a failure")
    a = ap.parse_args()
    if a.orchestrator not in m["orchestrator_tier"]:
        raise SystemExit(
            f"unknown orchestrator '{a.orchestrator}'. Known: {', '.join(m['orchestrator_tier'])}.\n"
            f"Register it with: route.py add --tier <T0-T5> --family <f> --harness {a.orchestrator} "
            f"--model <model> --orchestrator")
    used = {}
    for part in filter(None, a.pool_used.split(",")):
        name, sep, value = part.partition("=")
        if not sep or not value.strip():
            raise SystemExit(f"--pool-used: '{part}' has no value. Expected form: claude=0.4,codex=0.02")
        try:
            used[name.strip()] = float(value)
        except ValueError:
            raise SystemExit(f"--pool-used: '{value}' is not a number. Expected form: claude=0.4")
    try:
        out = route(a.hard, a.spec, a.check, a.files, a.critical, a.orchestrator, a.attempt,
                    matrix=m, task_class=a.task_class, pool_used=used)
    except ValueError as error:
        raise SystemExit(f"route.py: {error}")
    out["task"] = a.task
    print(json.dumps(out, indent=2))
