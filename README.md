# loopcraft v0.2.4

A project-agnostic harness that stacks the loops from the "Loopcraft" diagram
on top of the agent CLIs you already have installed and logged in.

```
loop 5  ????      backlog.md checklist             exit: list done / --forever
loop 4  metaloop  race or relay between agents     exit: winner merged
loop 3  /goal     run -> verify -> judge -> retry  exit: verify AND judge pass
loop 2  turn      one `claude -p` / `codex exec`   exit: no more tool calls
loop 1  tokens    inside the model                 exit: stop token
```

## How billing works (read this)

Loopcraft invokes the official local CLIs already installed on your machine.
When those CLIs are authenticated with subscription accounts, worker calls use
the CLIs' included plan usage/quotas rather than loopcraft making direct API
calls. Loopcraft never reads, proxies, or exports Claude/Codex auth tokens.

Protections on by default:

- `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are **stripped from worker
  subprocesses**, so a stray exported key can't silently flip the CLIs into
  API billing. Non-interactive `claude -p` will use `ANTHROPIC_API_KEY` ahead
  of your subscription login if it's present, which is exactly the leak this
  prevents. Pass `--allow-api-billing` if you actually want API billing.
- **Codex auth is checked** before running: loopcraft runs `codex login
  status` and refuses if Codex is on API-key auth (which bills your OpenAI
  Platform account) unless `--allow-api-billing` is set. Stripping the env var
  isn't enough on its own, because `codex login --with-api-key` caches the key
  locally — the status check is what actually catches it.
- `OPENROUTER_API_KEY` is only ever used for the judge HTTP call, never passed
  to workers.

Claude note: `claude -p` quota accounting is provider-defined and may be
tracked differently from interactive Claude Code usage. Loopcraft still invokes
the official CLI under your login, but quota behavior can change — run
`claude /status` interactively if you're unsure where usage is landing.

OpenRouter cost (only if you choose an LLM judge) depends on the model, prompt
size, and retry count. Loopcraft keeps judge prompts small and non-agentic.

**Local/personal use only.** This is your script driving your CLIs under your
logins on your machine. Don't host it as a service or route other people's
jobs through your subscription credentials — provider terms require API-key
auth for anything product-shaped.

## Authority order (who can declare success)

```
1. the --verify command (tests)      <- ground truth, hard-coded
2. git diff / status evidence
3. the LLM judge (optional)          <- advisory; can fail, can never
                                        override a failing verify
```

The **default judge is `verify-only`**: no LLM call at all, so v0 costs
nothing beyond your worker quota. Success = verify exits 0. For semantic
checking ("tests pass but did it actually do the thing?"), opt into an LLM
judge with `--judge openrouter:<model>`, `--judge claude`, or `--judge codex`
— and even then, a judge can never pass a run whose verify command failed.

## Install

One file, zero dependencies, Python 3.9+. Pick one:

**Onto your PATH (run `loopcraft` from any project root or VM):**

```bash
curl -fsSL https://raw.githubusercontent.com/oldsnakenewtrik/loopcraft/v0.2.4/install.sh | bash
```

Installs to `~/.local/bin/loopcraft` (override with `LOOPCRAFT_BIN`, pin a
different ref with `LOOPCRAFT_REF`). `curl | bash` runs remote code — the
one-liner is pinned to the `v0.2.4` tag so it can't change under you; read
`install.sh` first if you'd rather not pipe to a shell.

**Or just grab the single file into one project (no install):**

```bash
curl -fsSL https://raw.githubusercontent.com/oldsnakenewtrik/loopcraft/v0.2.4/loopcraft.py -o loopcraft.py
python3 loopcraft.py --help
```

A Claude Code skill is bundled under `claude-skill/loopcraft/` — copy it to
`~/.claude/skills/loopcraft/` to drive the whole ritual with `/loopcraft`.

## Setup

1. Log in once interactively to each CLI you'll use:
   - `claude` (Claude Code — sign in with your subscription account)
   - `codex login` (Codex — sign in with ChatGPT)
2. (Optional) `export OPENROUTER_API_KEY=sk-or-...` — only needed if you want
   semantic judging. The default `verify-only` judge needs no key and no
   credits.

(`install.sh` puts `loopcraft` on your PATH; the examples below use
`python3 loopcraft.py` but `loopcraft …` works identically once installed.)

## Not sure what to run? `--suggest`

```bash
loopcraft -C ~/code/myproject --suggest
```

Scans the repo (no LLM, no agents, no execution) and prints a few ready-to-run
loops — each with a goal **and** a real verify command already attached, ranked
by how trustworthy that verify is. It favours objectively gateable work
(typecheck, lint, build) over vague ideas, because a loop is only as good as its
`--verify`. STRONG suggestions are safe to run as-is; MEDIUM/WEAK ones tell you
what to harden first. Copy the one you want, confirm it's green at baseline, run.

## Start here (prove loop 3 first)

```bash
python3 loopcraft.py -C ~/code/myproject \
  -g "Fix the failing auth tests" \
  --worker claude \
  --verify "pytest -q" \
  --max-attempts 3
```

`--verify` is required: if you omit it, loopcraft detects your project type
(package.json -> `npm test`, pyproject.toml -> `pytest -q`, Cargo.toml ->
`cargo test`, go.mod -> `go test ./...`, Makefile -> `make test`) and tells
you what to pass. For docs-only tasks, say so explicitly: `--verify none`.

Once single mode behaves on your repo, graduate to the fancier loops:

**Race (loop 4).** Contenders run **concurrently**, each in its own **git
worktree** off the same base commit — branches alone don't isolate untracked
files, caches, or build artifacts, so agents would stomp each other. Each
worktree is verified independently; the winning branch is merged only if it
passed *and* you said `--merge-winner`. After merging, loopcraft **re-runs
`--verify` in the real checkout** — a patch can pass in isolation but break on
merge (conflicts, base movement, generated files) — and rolls back to the base
commit if that final verify fails, leaving the winner branch for you to inspect.

Two race gotchas worth knowing:

- A fresh worktree has **no gitignored deps** (`node_modules`, `.venv`, build
  caches). If your verify needs them, install them once per worktree with
  `--worktree-setup "pnpm install"` (or `uv sync`, etc.).
- Picking the *best* of several passing contenders needs a judge that can
  compare them: `--judge openrouter:<model>`, `--judge claude`, or `--judge
  codex`. With `--judge verify-only` there's no LLM to compare, so the first
  passing contender wins. Loser branches are pruned after (keep them with
  `--keep-branches`).

```bash
python3 loopcraft.py -C ~/code/myproject -g "..." \
  --mode race --race-workers claude,codex \
  --verify "npm test" --worktree-setup "npm ci" \
  --judge claude --merge-winner
```

**Relay (loop 4).** Builder/reviewer alternation in one tree: one agent
writes, the other audits and fixes, judge referees each round.

```bash
python3 loopcraft.py -C ~/code/myproject -g "..." \
  --mode relay --race-workers claude,codex --verify "pytest -q"
```

**Backlog (loop 5).** A markdown checklist; loopcraft works through unchecked
items, ticks off successes, annotates failures.

```bash
cat > backlog.md <<'EOF'
- [ ] Fix the flaky DateTime test in tests/test_utils.py
- [ ] Add a --json flag to the CLI
EOF
python3 loopcraft.py -C ~/code/myproject --backlog backlog.md \
  --verify "pytest -q" --max-goals 2 --stop-on-fail
```

`--forever` (keep polling the file) deliberately **requires `--max-goals N`**
so an unattended loop can't quietly drain your Claude/Codex quotas or
OpenRouter credits. Add `--sleep 300` to set the poll interval.

## Safety profiles

| Profile | Claude behavior | Codex behavior |
|---|---|---|
| `--safety strict` | `--permission-mode dontAsk` (deny-by-default, fail-loud) + `--allowedTools Read,Grep,Glob,Edit,Write` + `--disallowedTools mcp__*` | `codex exec --sandbox workspace-write` |
| `--safety normal` (default) | `--permission-mode acceptEdits` | `codex exec --sandbox workspace-write` |
| `--safety yolo` | `--dangerously-skip-permissions` | `codex exec --yolo` (bypasses approvals *and* sandbox) |

`dontAsk` is what makes strict usable headless: a tool that isn't pre-approved
is denied immediately rather than hanging on a prompt nobody can answer.
Customize the allowlist with `--allowed-tools "Read,Edit,Bash(git status *),Bash(pytest *)"`.

`yolo` refuses to run without `--i-understand-yolo`, and should only ever run
inside a container/VM or a disposable checkout.

## Secret hygiene

- Common token shapes (`sk-...`, `sk-or-...`, `sk-ant-...`, `ghp_...`,
  `github_pat_...`, `xox?-...`, `npm_...`, `AKIA...`, JWTs,
  `key/token/secret/password = ...`) are **redacted** from logs, saved
  reports, and everything sent to the judge.
- Worker prompts instruct agents never to print or commit credentials or
  .env contents.
- Loopcraft never touches `~/.codex/auth.json` or Claude's credential store —
  treat those files like passwords.

Redaction is regex-based and best-effort; don't point this at a repo whose
working tree contains live production secrets.

## Who does what

| Role | Pick | Why |
|---|---|---|
| Worker (writes code) | `claude` and/or `codex` | agentic, tool-using, runs on your CLI logins |
| Judge (default) | `verify-only` | no LLM call — tests are the verdict |
| Judge (semantic) | `openrouter:<cheap model>` | one small call per attempt |
| Advisory planner | `--worker openrouter` | no file access; plan/diff as text only |
| $0 semantic judge | `--judge claude` / `--judge codex` | uses quota instead of credits |

Judge model is configurable — swap in whatever's cheap and good this month,
e.g. `--judge openrouter:google/gemini-2.5-flash`.

## Preflight & concurrency

Before running workers, loopcraft checks: the directory exists; for race/relay
the repo is git and the tree is clean (ignoring `.loopcraft/`); the worker CLI
is on PATH; Codex auth mode (refuses API-key billing unless opted in); a verify
command is present or `--verify none` is explicit; and `--judge verify-only`
has something to verify against.

A PID lockfile at `<project>/.loopcraft/lock` stops two loopcraft runs from
editing the same repo or backlog at once. A live lock blocks the second run
(override with `--force-unlock`); a stale lock from a dead process is recovered
automatically. The lock is released on clean exit.

## Logs

Every run writes to `<project>/.loopcraft/run-<timestamp>/`: the run log plus
each attempt's (redacted) agent report. `.loopcraft/` is auto-added to
`.git/info/exclude` so it never pollutes your repo.

## Knobs

`--max-attempts` (loop-3 retries, default 4) · `--max-turns` (per-Claude
tool-call cap, default 30) · `--timeout` (seconds per worker run, default
1800) · `--claude-model` / `--codex-model` · `--race-workers a,b` ·
`--worktree-setup "<cmd>"` / `--keep-branches` (race) ·
`--max-goals` / `--stop-on-fail` / `--sleep` (loop 5).
