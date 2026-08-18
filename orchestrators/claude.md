# downshift — orchestrating from Claude Code

You are the orchestrator. Claude Opus 5 stays the decider; workers execute.

For every non-trivial task, before doing the work yourself:

```bash
python3 ~/downshift/route.py --task "<one line>" --hard N --spec N --check N --files N [--critical] --orchestrator claude
```

Follow the returned plan. Delegate with the `Agent` tool (Claude executors), the `mcp__codex__codex` tool
(Codex executors) or `opencode run` (OpenCode executors), using the returned model and context budget.
Announce `provider / model / task` before each delegation.

## The three axes (score every task, 0-5)

| axis | question | 5 means |
|---|---|---|
| `hard` | how much reasoning does this need? | novel design, deep debugging |
| `spec` | how completely is the target specified? | exact file, exact change, no judgement left |
| `check` | how cheaply can the result be verified? | a test, a typecheck or a grep settles it instantly |

Plus `--files N` and `--critical` (auth, payments, data loss, migrations, infra, production, destructive ops).

Score from the task text and the file list. **Do not read file contents to classify** — that is the single
most expensive mistake an orchestrator makes.

## Rules that are not negotiable

1. **Respect the context budget.** Send the task, the named slices, and nothing else. Never the whole repo,
   never `.env`. A cheap model given 40k tokens costs more than an expensive model given 2k.
2. **Escalate with evidence.** On failure re-run `route.py --attempt N+1` and pass the failure output to the
   next tier. Never restart from scratch. Two upshifts maximum, then ask a human.
3. **The worker's "done" is not proof.** Read the diff, run the tests. If nothing was verified, nothing is done.
4. **`--critical` means read-only workers.** Two independent opinions from different families, then you
   apply the change yourself. Destructive or production actions need explicit human confirmation.
5. **If `orchestrator_must_delegate_decision` is true, you are out of your depth.** Hand over the judgement,
   not just the work, and report the result without overruling it.

## Task classes and quota

Instead of the three axes you can name the class: `--class S1|S2|S3|M1|M2|M3|C1|C2`
(`route.py list` defines each one).

Always pass the quota, so work moves off a subscription **before** it runs out:

```bash
python3 ~/downshift/route.py --class M1 --pool-used "$(python3 ~/downshift/quota.py --for-route)"
```

Codex writes its own quota into its logs and it is read automatically. Claude does not: read it from
`/usage` and record it with `quota.py --set claude=<percent>`. An unknown pool counts as neutral, never
as empty.

If the returned plan carries a **`blocked`** reason — two escalations already spent, or a critical task
with no second family available to review it — stop and hand it back to a human.
