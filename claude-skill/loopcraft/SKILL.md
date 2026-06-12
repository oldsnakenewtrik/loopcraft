---
name: loopcraft
description: >-
  Orchestrate the loopcraft harness to run a hands-off, verify-gated autonomous
  coding loop on a target repo, driven by the user's local Claude and Codex CLI
  logins. Use this whenever the user says "loopcraft" or "/loopcraft", or asks
  to run an unattended/autonomous agent loop, work a backlog of goals
  automatically, "run agents until the tests pass", let an agent grind on a task
  while they step away, or set up a run→verify→judge→retry loop. Covers picking
  AND adversarially hardening the verify command (the trust anchor), writing the
  goal backlog, launching the loop in the background, and reporting results from
  the logs. Reach for this even when the user doesn't name loopcraft explicitly
  but clearly wants hands-off, test-gated agent automation on a repo.
---

# loopcraft

Loopcraft is a project-agnostic harness that drives the user's *already-
installed, already-logged-in* agent CLIs (`claude`, `codex`) in a loop until a
goal is verifiably met. This skill is the operating ritual around it — the part
that makes a run trustworthy enough to walk away from.

**Invoking the harness:** prefer `loopcraft` if it's on PATH (installed via
`install.sh`). Otherwise use `$LOOPCRAFT_BIN` or `python3 /path/to/loopcraft.py`.
All examples below say `loopcraft`; substitute the form available on this
machine. Confirm with `loopcraft --help` (or `python3 loopcraft.py --help`).

The harness has no opinion about *what* to work on or *how to know it worked*.
That judgment is yours, and it lives almost entirely in **one decision: the
verify command.** Get that right and the loop is safe to leave unattended; get
it wrong and the loop will confidently do the wrong thing.

## The loop ladder

```
loop 5  backlog   work through a markdown checklist     exit: list done / --max-goals
loop 4  metaloop  race (worktree-per-agent) or relay    exit: winner merged
loop 3  /goal     run -> verify -> judge -> retry        exit: verify AND judge pass
```

Always start at **loop 3 single mode** on a new repo. Escalate to race/relay
only when single mode proves insufficient for a specific stubborn goal.

## Authority order (never violate this)

```
1. the --verify command (tests)   <- ground truth, HARD-CODED, frozen at runtime
2. git diff / status evidence
3. the LLM judge (optional)        <- advisory; can fail a run, can NEVER
                                      override a failing verify into a pass
```

All adversarial scrutiny of the verify command belongs at *authoring* time
(below), and **none** at runtime. Never let an agent debate, relax, or
regenerate the verify command mid-loop — that's how a worker argues its way to a
false pass (Goodhart's law).

## Step 0 — Preflight

```bash
python3 --version           # need 3.9+
which claude codex          # workers must be on PATH
codex login status          # must say "Logged in using ChatGPT", NOT an API key
env | grep -E 'ANTHROPIC_API_KEY|OPENAI_API_KEY'   # ideally empty
```

Loopcraft strips `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` from worker subprocesses
by default and refuses to run if Codex is on API-key auth — unless
`--allow-api-billing` is passed. Local/personal use under the user's own logins.

## Step 1 — Nail the verify command (the hard part)

Find the **one command that asserts success**. Three required properties:

1. **Correct runner** — what the project actually uses, not the generic name. A
   repo with `.venv`/`uv.lock` needs `uv run pytest`, not bare `pytest`. Getting
   this wrong fails every attempt for environmental, not agent, reasons.
2. **Fast** — it reruns every attempt. If the suite is slow or hangs
   (integration/live-API/browser tests), narrow it, e.g.
   `pytest -m "not integration and not live_e2e and not browser"`.
3. **Green at baseline** — run it once yourself first. A pre-existing red suite
   means the loop can't tell your goal's failure from the old one.

If undeterminable, loopcraft auto-detects project type and tells you what to
pass. For docs-only tasks, pass `--verify none` explicitly.

**Aim at the OUTCOME, not a proxy.** Unit tests passing ≠ goal achieved. Climb
only as high as the cost of a *false pass* forces you to; start at the existing
fast suite plus a semantic judge, and promote a goal to a smoke/E2E check only
after a real false-pass proves it needs one.

## Step 2 — Harden the verify adversarially (then freeze it)

Before committing to the verify command, attack it — it's where all the trust
sits and it's human-authored, the weakest link.

**(a) Red-team it.** Look for: *too weak/gameable* (goal "satisfied" while
undone — e.g. goal "add validation", verify "tests pass" passes with nothing
added) and *too strong/flaky/slow* (rejects good work, flakes on a live
dependency, hangs).

**(b) Falsify it empirically — the stronger check:**

```
1. Make a deliberately-WRONG change to the goal's target (or leave it undone)
2. Run verify  ->  it MUST go red
3. Revert
4. Run verify  ->  it MUST go green
```

If step 2 stays green, the verify is a tautology that rubber-stamps anything —
fix it before running loopcraft. Mutation-testing logic applied to your gate.
Once hardened, **freeze the verify command.**

## Step 3 — Write the goal(s) and pick the judge

Single goal: `-g "..."`. Multiple: a markdown checklist, one outcome per line:

```markdown
- [ ] <concrete goal the verify command can confirm>
- [ ] <next one>
```

Judge = the optional semantic "but did it actually do the thing?" check:
- `--judge verify-only` (default) — no LLM, deterministic, can't misfire. **Most
  robust for unattended runs.** Free.
- `--judge claude` / `--judge codex` — semantic check on the user's quota; give
  it `--max-attempts` headroom (a flaky judge gates good code).
- `--judge openrouter:<model>` — real completion endpoint; needs
  `OPENROUTER_API_KEY`. Steadier than a CLI judge.

## Step 4 — Launch hands-off, then read the log

```bash
loopcraft -C <repo> -g "<goal>" \
  --worker claude \
  --verify '<hardened verify cmd>' \
  --judge verify-only \
  --safety strict \
  --max-attempts 3
```

For a backlog (loop 5): swap `-g` for `--backlog <file.md>` and add
`--max-goals N --stop-on-fail`. `--forever` *requires* `--max-goals N` so an
unattended loop can't drain quota.

Run it in the **background** and watch the log instead of babysitting:

```
<repo>/.loopcraft/run-<timestamp>/run.log      # live progress + verdicts
<repo>/.loopcraft/run-<timestamp>/attempt*.txt # each attempt's redacted report
```

`.loopcraft/` is auto-excluded from the repo's git; a PID lockfile stops two
runs colliding. When it finishes, report which goals passed/failed, attempts
used, and — crucially — whether a *judge misfired* vs. the *code actually
failed*, since those mean very different things.

## Safety profiles

| Profile | Use when |
|---|---|
| `--safety strict` *(recommended hands-off)* | deny-by-default tools, fail-loud, no MCP; a non-approved tool is denied instead of hanging on a prompt no one can answer |
| `--safety normal` *(default)* | acceptEdits — fine when watching |
| `--safety yolo` | no prompts + wide sandbox — refuses without `--i-understand-yolo`; container/VM only |

## Escalating beyond single mode

- **Race** — N agents, same goal, each in its own git **worktree**; best passing
  branch wins, merged only with `--merge-winner` (loopcraft re-verifies in the
  real checkout post-merge, rolls back if it breaks). Needs a clean tree.
  `--mode race --race-workers claude,codex --merge-winner`
- **Relay** — builder/reviewer alternation in one tree; judge referees.
  `--mode relay --race-workers claude,codex`

## Cleanup

If the run was practice/throwaway, offer to revert (`git checkout -- <files>`
plus any test artifacts the suite created). Leave the repo as the user expects.

## Mental model

Harden the verify command adversarially up front, freeze it, then let loopcraft
grind run→verify→judge→retry hands-off — your trust is only ever as good as that
one frozen command. Full reference: the repo `README.md` / `loopcraft --help`.
