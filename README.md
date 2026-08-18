# downshift

**Routing for people who pay subscriptions, not tokens.**

If you run Claude Code, Codex CLI and OpenCode side by side, you hold three separate quota windows.
Dollar-per-token routers optimise the wrong currency. What actually runs out is a *window* — and the
cheapest thing you own is the pool nobody is touching right now.

downshift decides, before dispatch, three things:

1. **Which model** — the lowest tier the task's own verifiability allows.
2. **Which pool** — spread across subscriptions, and move work off a pool *before* it runs dry rather
   than after it 429s.
3. **How much context** — a per-tier budget, because a cheap model handed 40k tokens costs more window
   than an expensive one handed 2k.

Whatever agent you launched is the orchestrator: Claude Code, Codex CLI, OpenCode, or a local model.
Same matrix, same rules, any entry point.

## The task classes

Eight classes, defined by what makes them hard rather than by keywords. `route.py list` prints them.

| class | task | goes to | why |
|---|---|---|---|
| **S1** lookup | find, read, list, git log. No edit. | free tier | nothing to get wrong that a glance won't catch |
| **S2** mechanical | rename, format, imports, docstrings — a lint or typecheck closes it | free tier | wrong answer costs one retry |
| **S3** local | one or two files, cause already known, test already exists | cheap tier, **whichever pool has room** | Haiku and Luna are the same job in two subscriptions |
| **M1** feature | clear spec, 3–8 files, tests still to write | mid tier | the check does not exist yet, so no downshift |
| **M2** debug | red test, reproducible, cause unknown | workhorse, judgement family | finding a cause is judgement, not execution |
| **M3** refactor | 8+ files or shared interfaces, target already decided | deep tier | breadth, not ambiguity |
| **C1** design | ambiguous requirement, trade-offs, architecture, migration plan | frontier | the only check is a human |
| **C2** critical | auth, payments, data deletion, migrations, infra, production | frontier + independent second family, workers read-only | |

The assignments follow each vendor's own guidance rather than a guess: Luna for "clear and repeatable
tasks", Terra for "everyday development", Sol for "complex, ambiguous, or high-consequence work"
([Cerebras](https://www.cerebras.ai/blog/getting-the-most-out-of-gpt-5-6-sol-terra-and-luna),
[DataStudios](https://www.datastudios.org/post/gpt-5-6-sol-vs-terra-vs-luna-explained-speed-api-cost-reasoning-depth-performance-differences-a));
Haiku for "simple, high-volume" work and the most rate-limit-efficient option, Sonnet for "the bulk of
real work", Opus for what is "genuinely hard" and for the parent agent that plans and integrates
([Anthropic](https://claude.com/resources/tutorials/choosing-the-right-claude-model)).

## Seeing it, and overriding it

Every decision prints as one line, and says what the matrix would have chosen if you overrode it:

```bash
$ python3 route.py --class M1 --plain
M1 -> codex/gpt-5.6-terra (medium effort)  [T3 workhorse, pool codex, 16000 tok]
  · class M1 (feature): clear spec across 3-8 files, tests still to write...
  · hard=3 -> T3
  · spec=5: target is fully specified -> execution family
```

The matrix is a default, not a verdict. Override it for one call, or for good:

```bash
python3 route.py --class S3 --force-model claude-opus-5 --plain   # this call only
python3 route.py pin M1 gpt-5.6-sol                               # every M1 from now on
python3 route.py unpin M1                                         # back to the matrix
```

A pin shows up in `route.py list`, and every plan carries `pinned_by_you` plus a line naming what the
matrix would have picked — so an override you set months ago never quietly becomes the new normal.
An unregistered model is refused rather than silently ignored.

## Pools and the reserve rule

Every executor belongs to a pool — the subscription it draws on. **Tiers inside a pool share one window**:
dropping Opus → Haiku saves less than you think, because it is the same Claude quota. The move that
matters is changing pool.

Each pool keeps a reserve. Above its threshold, only the classes that pool is genuinely needed for may
draw on it; everything else moves to a pool with headroom, at the same tier or higher — never lower.

```
pool claude    reserve above 70%  for C1, C2, M2
pool codex     reserve above 75%  for M1, M3, C2
pool opencode  free, always available
```

So at 90% Claude, a small local fix leaves for Codex while design and critical work stay where judgement
lives. That is the difference from a proxy that rotates accounts once you are already blocked.

```bash
python3 quota.py                 # what is left in each pool
python3 quota.py --set claude=40 # the number /usage showed you
python3 route.py --class M1 --pool-used "$(python3 quota.py --for-route)"
```

**Where the numbers come from, honestly.** Codex writes its real usage into its session logs
(`rate_limits.primary.used_percent`), so `quota.py` reads it directly. Claude Code exposes its
subscription percentage only in the status line and `/usage` — there is no supported programmatic source
([issue 35992](https://github.com/anthropics/claude-code/issues/35992),
[issue 44328](https://github.com/anthropics/claude-code/issues/44328)) — so it is entered by hand and
reported with its age, and goes stale rather than pretending to be fresh. An unknown pool is treated as
neutral, never as empty. Nothing here is estimated and presented as measured.

## Install

```bash
git clone https://github.com/<you>/downshift ~/downshift
python3 ~/downshift/route.py selftest        # stdlib only, no dependencies
```

| harness | file | line to add |
|---|---|---|
| Claude Code | `~/.claude/CLAUDE.md` | `See ~/downshift/orchestrators/claude.md` |
| Codex CLI | `~/.codex/AGENTS.md` | `See ~/downshift/orchestrators/codex.md` |
| OpenCode | `opencode.json` → `instructions` | `~/downshift/orchestrators/opencode.md` |

## Configuring your own models

Local models, other CLIs, other vendors — one command, never hand-edit the JSON:

```bash
python3 route.py list
python3 route.py add --tier T1 --family codex --harness ollama --model qwen3-coder:30b --orchestrator
python3 route.py remove qwen3-coder:30b
```

- **`--tier`** T0–T5 is capability, not price.
- **`--family`**: two roles, named in `policy` and renameable. The *judgement* family is the one you trust
  with an ambiguous target and is never substituted away; the *execution* family takes fully specified work.
- **`--pool`** is the subscription it draws on (defaults to the harness name, and a new pool is created with
  no reserve until you configure one). **`--weight`** is how hard it bites that pool, and only breaks ties
  between two models in the same pool.
- **`--orchestrator`** registers the harness as one that can drive, at its own tier.

## How the axes work

The orchestrator scores three axes from the task text and the file list — **without reading file
contents**, which is where naive orchestration burns most of its window:

| axis | question | 5 means |
|---|---|---|
| `hard` | how much reasoning does this need? | novel design, deep debugging |
| `spec` | how completely is the target specified? | exact file, exact change |
| `check` | how cheaply can the result be verified? | a test or a typecheck settles it |

`hard` picks the tier, `check` pulls it down, `spec` picks the family, quota picks the pool. `--class`
fills all three from the table above. `--files ≥6` pushes up a tier; `--attempt N` upshifts after a
failure carrying the failure output forward.

Two situations hand the task back to you, and they are reported in a **`blocked`** field rather than only
in prose, so an orchestrator cannot skim past them: more than two escalations without a result, and a
critical task for which no second family is registered to review it. When `blocked` is set, stop.

| tier | context budget | what is allowed in |
|---|---|---|
| T0 | 2 000 | task + one file slice |
| T1 | 4 000 | + up to 3 slices, error text |
| T2 | 8 000 | + failing test output, diff |
| T3 | 16 000 | + related files, interfaces |
| T4 | 32 000 | + full test output, call sites |
| T5 | 48 000 | + design docs, relevant history |

## Prior art

Two families exist already, and downshift is neither.

**Proxies that react after you hit the wall** — [9Router](https://github.com/decolua/9router) (tracks 5h
and weekly quota, switches provider when it runs out),
[CC-Router](https://github.com/VictorMinemu/CC-Router) (round-robins several Claude Max accounts on 429),
[OmniRoute](https://explainx.ai/blog/omniroute-ai-gateway-free-llm-proxy-claude-code-2026) (quota-share
across keys), [claude-auto-retry](https://github.com/cheapestinference/claude-auto-retry) (waits for the
reset), [claude-code-router](https://github.com/musistudio/claude-code-router) (local gateway). They sit
in the request path and rotate accounts of the same product. downshift never proxies anything — native
auth and native sandboxes stay intact — and it splits work across *different* subscriptions before either
is exhausted.

**Tier delegation inside one harness** —
[opencode-model-router](https://github.com/marco-jardim/opencode-model-router) injects a keyword taxonomy
and a produce → verify → escalate loop into the system prompt. **That escalate loop is not novel here;
that project already does it.** What differs: verifiability chosen *before* dispatch, a context budget,
and more than one vendor.

**Multi-harness orchestration** —
[cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator) coordinates Claude Code,
Codex and others in tmux sessions with no cost or quota policy at all;
[wshobson/agents](https://github.com/wshobson/agents) ships one Markdown source to many harnesses.
downshift is the policy you would run on top of either.

**Measurement only** — `ccusage`, `claude-monitor` and friends tell you what you spent. They do not decide
anything.

The combination I could not find: verifiability-gated tiers, per-tier context budgets, reserve-based pool
switching before exhaustion, and an orchestrator whose own tier is an input.

## What this does not claim

No benchmark. I have not measured a savings figure across a task suite, so none is quoted here. The
mechanism is stated instead, and `route.py` prints the reason for every decision so you can audit it
against your own usage rather than trust a number.

It does not proxy traffic, does not make a weak model strong, and cannot read Claude's quota for you.

## License

MIT
