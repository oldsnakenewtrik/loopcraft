#!/usr/bin/env python3
"""
loopcraft v0.2.2 — a project-agnostic stacked-loop agent harness.

Loop 1 (tokens)   : inside the model. free.
Loop 2 (turn)     : one headless CLI invocation (claude -p / codex exec).
Loop 3 (/goal)    : run -> verify -> judge -> retry. exit: verify+judge pass.
Loop 4 (metaloop) : race (worktree-per-agent, judge picks winner) or relay
                    (builder/reviewer alternation). exit: winner merged.
Loop 5 (????)     : backlog.md checklist driven. exit: list done (or --forever).

Workers (expensive, agentic)  -> your locally-authenticated claude/codex CLIs.
Judge   (cheap, single call)  -> OpenRouter model, or a CLI as fallback.

Authority order, hard-coded:  verify command > judge opinion.
A judge can never mark "pass" while the verify command fails.

Personal/local use only: this drives the CLIs installed and authenticated on
YOUR machine. Do not host it for others or route third parties through your
subscription credentials. Loopcraft never reads or exports CLI auth tokens,
and by default it strips ANTHROPIC_API_KEY / OPENAI_API_KEY from worker
subprocesses so the CLIs use your subscription login, not API billing.

Zero dependencies. Python 3.9+. State & logs land in <project>/.loopcraft/.
"""

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- defaults

DEFAULT_JUDGE = "verify-only"   # no LLM call; verify command is the verdict
SUGGESTED_LLM_JUDGE = "openrouter:deepseek/deepseek-chat-v3.1"
DEFAULT_WORKER = "claude"
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_MAX_TURNS = 30
DEFAULT_TIMEOUT = 1800
EVIDENCE_TAIL = 4000
DIFF_CAP = 6000

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

VERIFY_HINTS = [
    ("package.json", "npm test"),
    ("pnpm-lock.yaml", "pnpm test"),
    ("yarn.lock", "yarn test"),
    ("pyproject.toml", "pytest -q"),
    ("setup.py", "pytest -q"),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
    ("Makefile", "make test"),
]

# ---------------------------------------------------------------- redaction

REDACT_PATTERNS = [
    r"sk-or-[A-Za-z0-9_\-]{10,}",
    r"sk-ant-[A-Za-z0-9_\-]{10,}",
    r"sk-[A-Za-z0-9_\-]{20,}",
    r"ghp_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"gho_[A-Za-z0-9]{20,}",
    r"xox[baprs]-[A-Za-z0-9\-]{10,}",
    r"npm_[A-Za-z0-9]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}",  # JWT
    r"(?i:(api[_-]?key|token|secret|password)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{12,})",
]
_REDACT_RE = re.compile("|".join(f"(?:{p})" for p in REDACT_PATTERNS))


def redact(text: str) -> str:
    return _REDACT_RE.sub("[REDACTED]", text or "")


def child_env(args):
    """Environment for worker subprocesses: scrub keys that could flip the
    CLIs from subscription auth to API billing, and never leak the
    OpenRouter key to workers."""
    env = dict(os.environ)
    env.pop("OPENROUTER_API_KEY", None)
    if not args.allow_api_billing:
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
    return env


# ---------------------------------------------------------------- logging

class Log:
    def __init__(self, project: Path):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.stamp = stamp
        self.dir = project / ".loopcraft" / f"run-{stamp}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = open(self.dir / "run.log", "a", encoding="utf-8")
        self.cost_usd = 0.0   # summed across worker calls that report cost

    def add_cost(self, usd):
        self.cost_usd += (usd or 0.0)

    def __call__(self, msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {redact(msg)}"
        print(line, flush=True)
        self.file.write(line + "\n")
        self.file.flush()

    def save(self, name: str, content: str):
        (self.dir / name).write_text(redact(content or ""), encoding="utf-8")


# ---------------------------------------------------------------- shell

def run_cmd(cmd, cwd, timeout, env=None):
    """Run a command, return (rc, stdout, stderr). Never raises on failure."""
    try:
        p = subprocess.run(cmd, cwd=str(cwd), timeout=timeout,
                           capture_output=True, text=True, env=env)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"


def git(cwd, *args, timeout=120):
    return run_cmd(["git", *args], cwd, timeout)


# ---------------------------------------------------------------- workers
# Loop 2: one invocation == one agent turn-loop ("the agent loop").

def claude_safety_flags(args):
    if args.safety == "yolo":
        return ["--dangerously-skip-permissions"]
    if args.safety == "strict":
        # dontAsk = deny-by-default (fail loud, never stall); allowlist
        # pre-approves only these; MCP tools denied outright.
        tools = args.allowed_tools or "Read,Grep,Glob,Edit,Write"
        return ["--permission-mode", "dontAsk",
                "--allowedTools", tools,
                "--disallowedTools", "mcp__*"]
    # normal
    flags = ["--permission-mode", "acceptEdits"]
    if args.allowed_tools:
        flags += ["--allowedTools", args.allowed_tools]
    return flags


def run_claude_worker(prompt, cwd, args, log):
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--max-turns", str(args.max_turns)] + claude_safety_flags(args)
    if args.claude_model:
        cmd += ["--model", args.claude_model]
    log(f"  worker=claude  (safety={args.safety}, max {args.max_turns} turns)")
    rc, out, err = run_cmd(cmd, cwd, args.timeout, env=child_env(args))
    text, meta, hit_cap = out.strip(), {}, False
    try:
        obj = json.loads(out)
        text = obj.get("result") or text
        meta = {k: obj.get(k) for k in
                ("num_turns", "duration_ms", "total_cost_usd", "session_id")
                if k in obj}
        hit_cap = obj.get("subtype") == "error_max_turns"
    except (json.JSONDecodeError, TypeError):
        pass
    cost = meta.get("total_cost_usd")
    if cost is not None:
        log.add_cost(cost)
        log(f"  claude: {meta.get('num_turns', '?')} turns, ${cost:.2f}")
    if hit_cap:
        # The attempt was cut off mid-task. Surface it as actionable (raise
        # --max-turns) and tell the judge/next attempt the work is partial —
        # otherwise it just looks like an opaque verify failure.
        log(f"  WARNING: claude hit the --max-turns cap ({args.max_turns}); "
            "attempt cut off mid-task. Raise --max-turns for large goals.")
        text = (f"[loopcraft: this attempt hit the --max-turns limit "
                f"({args.max_turns}) and was cut off before finishing — the "
                f"work below is incomplete.]\n\n{text}")
    if rc != 0 and not text:
        text = f"[claude exited {rc}] {err.strip()[-1500:]}"
    return {"worker": "claude", "rc": rc, "report": redact(text),
            "meta": meta, "hit_cap": hit_cap}


def run_codex_worker(prompt, cwd, args, log):
    if args.safety == "yolo":
        # --yolo == --dangerously-bypass-approvals-and-sandbox (both, not just
        # the sandbox mode). exec already downgrades approvals to never.
        cmd = ["codex", "exec", "--skip-git-repo-check", "--yolo",
               "-C", str(cwd)]
        log("  worker=codex   (--yolo: approvals+sandbox bypassed)")
    else:
        cmd = ["codex", "exec", "--skip-git-repo-check",
               "--sandbox", "workspace-write", "-C", str(cwd)]
        log("  worker=codex   (sandbox=workspace-write)")
    if args.codex_model:
        cmd += ["-m", args.codex_model]
    cmd += [prompt]
    rc, out, err = run_cmd(cmd, cwd, args.timeout, env=child_env(args))
    text = out.strip()  # codex exec: final message on stdout, progress on stderr
    if rc != 0 and not text:
        text = f"[codex exited {rc}] {err.strip()[-1500:]}"
    return {"worker": "codex", "rc": rc, "report": redact(text), "meta": {}}


def run_openrouter_worker(prompt, cwd, args, log):
    """Advisory worker: no file tools, returns a plan/patch as text."""
    model = args.or_worker_model
    log(f"  worker=openrouter:{model} (advisory — no file access)")
    sys_p = ("You are a senior engineer advising on a codebase you cannot "
             "open. Produce a precise, step-by-step plan or unified diff "
             "for the goal. Be concrete; name files and functions.")
    text = openrouter_chat(model, [{"role": "system", "content": sys_p},
                                   {"role": "user", "content": prompt}])
    return {"worker": f"openrouter:{model}", "rc": 0,
            "report": redact(text), "meta": {}}


WORKERS = {"claude": run_claude_worker,
           "codex": run_codex_worker,
           "openrouter": run_openrouter_worker}


def worker_prompt(goal, feedback, attempt, verify, extra=""):
    parts = [
        "You are an autonomous coding agent. Work ONLY inside the current "
        "repository. Complete this goal end to end:",
        f"\nGOAL:\n{goal}\n",
    ]
    if verify:
        parts.append(f"The result will be verified by running: `{verify}` — "
                     "make sure that command passes before you finish.")
    if feedback:
        parts.append(f"\nThis is attempt #{attempt}. A reviewer rejected the "
                     f"previous attempt with this feedback — fix it:\n{feedback}")
    if extra:
        parts.append("\n" + extra)
    parts.append("\nNever print or commit credentials, tokens, or .env "
                 "contents. When done, end with a short summary of what you "
                 "changed and how you verified it. Do not ask questions; "
                 "decide and act.")
    return "\n".join(parts)


# ---------------------------------------------------------------- judge
# Loop 3's referee: one cheap, non-agentic call. NEVER outranks verify.

def openrouter_chat(model, messages, temperature=0.1):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": temperature}).encode()
    req = urllib.request.Request(
        OPENROUTER_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "X-Title": "loopcraft"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode())
            return data["choices"][0]["message"]["content"]
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
            if attempt == 2:
                raise RuntimeError(f"OpenRouter call failed: {e}")
            time.sleep(2 * (attempt + 1))


def extract_json(text):
    text = re.sub(r"```(?:json)?", "", text or "")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


JUDGE_INSTRUCTIONS = (
    "You are a strict QA judge. Decide whether the GOAL was genuinely "
    "achieved, using the agent's report, the diff, and the verification "
    "output. Be specific about defects, risky changes, and unmet parts of "
    "the goal.\n"
    "Everything you need is in the text below — do NOT use any tools, do NOT "
    "open or read files. Answer directly from the provided evidence.\n"
    "A verdict of 'pass' must be backed by objective evidence in EVIDENCE — a "
    "passing verify result and/or a diff showing the change. If EVIDENCE shows "
    "no diff and no verify result, you cannot confirm the work actually "
    "landed; return 'fail'. The agent's report alone is never sufficient.\n"
    'Respond with ONLY a JSON object: {"verdict": "pass" or "fail", '
    '"risk": "low" or "medium" or "high", '
    '"feedback": "specific, actionable fixes if fail"}'
)


def decide_verdict(goal, result, evidence, verify_rc, has_diff, args, cwd, log):
    """Single place that turns evidence into pass/fail. verify-only skips the
    LLM entirely; otherwise the LLM judges but can never override a failing
    verify command, nor certify a pass with no objective evidence at all."""
    if args.judge == "verify-only":
        if verify_rc is None:
            return {"verdict": "fail", "risk": "high",
                    "feedback": "verify-only judge needs a --verify command; "
                                "none ran."}
        if verify_rc == 0:
            log("  judge=verify-only -> pass (verify exit 0)")
            return {"verdict": "pass", "risk": "low", "feedback": ""}
        log(f"  judge=verify-only -> fail (verify exit {verify_rc})")
        return {"verdict": "fail", "risk": "high",
                "feedback": f"The verify command failed (exit {verify_rc}). "
                            "Read the verify output in the evidence and fix it."}
    verdict = judge(goal, result, evidence, args.judge, cwd, args, log)
    if verify_rc not in (None, 0) and verdict["verdict"] == "pass":
        log("  OVERRIDE: judge said pass but verify failed -> fail")
        verdict = {"verdict": "fail", "risk": verdict.get("risk", "high"),
                   "feedback": f"The verification command failed "
                               f"(exit {verify_rc}). Fix it. "
                               + str(verdict.get("feedback") or "")}
    # No-objective-evidence guard. A "pass" must rest on something checkable: a
    # passing verify OR an actual diff. With neither (no --verify ran AND the
    # tree shows no change — e.g. -C pointed at a non-git dir), the judge has
    # only the agent's self-report, which can certify work that never landed.
    if verdict["verdict"] == "pass" and verify_rc is None and not has_diff:
        log("  OVERRIDE: pass with no objective evidence "
            "(no verify ran, no git diff) -> fail")
        verdict = {"verdict": "fail", "risk": "high",
                   "feedback": "No objective evidence the work was done: no "
                   "--verify command ran and the target shows no git diff (is "
                   "it a git repo?). Refusing to certify success on the agent's "
                   "report alone — add a real --verify (e.g. a file/content "
                   "assertion) or run inside the git repo that should receive "
                   "the change."}
    return verdict


def judge(goal, result, evidence, judge_spec, cwd, args, log):
    body = redact(f"GOAL:\n{goal}\n\nAGENT REPORT:\n{result['report'][:6000]}"
                  f"\n\nEVIDENCE:\n{evidence}")
    if judge_spec.startswith("openrouter:"):
        model = judge_spec.split(":", 1)[1]
        log(f"  judge=openrouter:{model}")
        raw = openrouter_chat(model, [
            {"role": "system", "content": JUDGE_INSTRUCTIONS},
            {"role": "user", "content": body}])
    else:
        log(f"  judge={judge_spec} (CLI fallback)")
        prompt = JUDGE_INSTRUCTIONS + "\n\n" + body
        if judge_spec == "claude":
            # Non-agentic: the verdict is one model reply, but claude -p spends
            # a turn on its final answer (and may probe with a tool first), so
            # --max-turns 1 frequently returns an error_max_turns envelope with
            # no usable result. Give it headroom and read the envelope honestly.
            rc, out, _ = run_cmd(["claude", "-p", prompt, "--output-format",
                                  "json", "--max-turns", "6"],
                                 cwd, 300, env=child_env(args))
            raw = out
            try:
                obj = json.loads(out)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                if obj.get("is_error") or not obj.get("result"):
                    return {"verdict": "fail", "risk": "high",
                            "feedback": "Judge call returned no verdict "
                            f"(claude: {obj.get('subtype', 'error')}). This is "
                            "a judge failure, not necessarily a code failure — "
                            "re-run, or switch to --judge verify-only / "
                            "--judge openrouter:<model> for a steadier judge."}
                raw = obj.get("result")
        elif judge_spec == "codex":
            rc, out, _ = run_cmd(["codex", "exec", "--skip-git-repo-check",
                                  "--sandbox", "read-only", "-C", str(cwd),
                                  prompt], cwd, 300, env=child_env(args))
            raw = out
        else:
            raise SystemExit(f"unknown judge: {judge_spec}")
    verdict = extract_json(raw) or {}
    if verdict.get("verdict") not in ("pass", "fail"):
        verdict = {"verdict": "fail", "risk": "high",
                   "feedback": f"Judge output unparseable: {raw[:800]}"}
    return verdict


# ---------------------------------------------------------------- evidence

def gather_evidence(cwd, verify, timeout, args, log):
    """Returns (evidence_text, verify_rc, has_diff). verify_rc is None if no
    verify ran; has_diff is True only when git shows actual working-tree changes
    (so callers can tell apparent success from objectively-evidenced success)."""
    chunks, verify_rc, has_diff = [], None, False
    rc, out, _ = git(cwd, "status", "--porcelain")
    if rc == 0:
        has_diff = bool(out.strip())
        chunks.append("git status (changed files):\n"
                      + (out.strip()[:1500] or "(clean)"))
        _, stat, _ = git(cwd, "diff", "--stat")
        _, diff, _ = git(cwd, "diff")
        chunks.append(f"git diff --stat:\n{stat.strip()[:1500]}")
        chunks.append(f"git diff (truncated):\n{diff[:DIFF_CAP]}")
    if verify:
        log(f"  verify: {verify}")
        verify_rc, vout, verr = run_cmd(["bash", "-lc", verify], cwd, timeout,
                                        env=child_env(args))
        tail = (vout + "\n" + verr)[-EVIDENCE_TAIL:]
        chunks.append(f"VERIFY CMD `{verify}` exit={verify_rc}\n"
                      f"output tail:\n{tail}")
        chunks.append("VERIFY RESULT: "
                      + ("PASSED" if verify_rc == 0 else "FAILED"))
    return redact("\n\n".join(chunks)), verify_rc, has_diff


# ---------------------------------------------------------------- loop 3

def goal_loop(goal, worker_name, cwd, args, log, tag=""):
    """run -> verify -> judge -> retry. Verify outranks judge, hard-coded."""
    feedback = None
    run_fn = WORKERS[worker_name]
    verdict, result = {}, {"report": ""}
    for attempt in range(1, args.max_attempts + 1):
        log(f"loop3{tag} attempt {attempt}/{args.max_attempts}  goal: {goal[:80]}")
        prompt = worker_prompt(goal, feedback, attempt, args.verify)
        result = run_fn(prompt, cwd, args, log)
        log.save(f"attempt{tag}-{attempt}-{worker_name}.txt", result["report"])
        evidence, verify_rc, has_diff = gather_evidence(
            cwd, args.verify, args.timeout, args, log)
        verdict = decide_verdict(goal, result, evidence, verify_rc,
                                 has_diff, args, cwd, log)
        log(f"  verdict: {verdict['verdict']} (risk {verdict.get('risk')}) "
            f"{str(verdict.get('feedback') or '')[:120]}")
        if verdict["verdict"] == "pass":
            return True, verdict, result
        feedback = verdict.get("feedback") or \
            "Goal not met; try a different approach."
    return False, verdict, result


# ---------------------------------------------------------------- loop 4

def ensure_git_exclude(cwd):
    """Keep loopcraft's own state out of the user's repo. Resolves the shared
    git common dir, so this also works inside a worktree — where `.git` is a
    file (not a dir) and info/exclude lives in the main repo's git dir. The old
    `cwd/.git/info` check silently no-op'd in worktrees, leaking `.loopcraft/`
    into `git status`."""
    rc, common, _ = git(cwd, "rev-parse", "--git-common-dir")
    if rc != 0:
        return
    gitdir = Path(common.strip())
    if not gitdir.is_absolute():
        gitdir = (cwd / gitdir).resolve()
    info = gitdir / "info"
    try:
        info.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    exclude = info / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".loopcraft/" not in existing:
        with open(exclude, "a", encoding="utf-8") as f:
            f.write("\n# added by loopcraft\n.loopcraft/\n")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except PermissionError:
        return True  # exists, owned by someone else


def acquire_lock(project, force, log):
    lockfile = project / ".loopcraft" / "lock"
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    if lockfile.exists():
        try:
            old = int(lockfile.read_text().split()[0])
        except (ValueError, IndexError):
            old = None
        if old and pid_alive(old):
            if not force:
                raise SystemExit(
                    f"Another loopcraft run holds the lock (pid {old}) on "
                    f"this repo. Wait for it, or pass --force-unlock if you "
                    "are sure it's dead.")
            log(f"--force-unlock: taking lock from pid {old}")
        else:
            log(f"recovering stale lock (pid {old} not running)")
    lockfile.write_text(f"{os.getpid()} {datetime.now().isoformat()}\n")

    def release():
        try:
            if lockfile.exists() and \
                    lockfile.read_text().split()[0] == str(os.getpid()):
                lockfile.unlink()
        except (OSError, IndexError):
            pass
    atexit.register(release)
    return lockfile


def codex_auth_preflight(args, log):
    rc, out, err = run_cmd(["codex", "login", "status"], ".", 30)
    text = (out + err)
    if rc != 0:
        log("WARNING: `codex login status` did not report a login — "
            "run `codex login` (ChatGPT) before using codex as a worker.")
        return
    log(f"  codex auth: {text.strip().splitlines()[0][:70] if text.strip() else '?'}")
    if "api key" in text.lower() and not args.allow_api_billing:
        raise SystemExit(
            "Codex is authenticated with an API key, which bills your OpenAI "
            "Platform account — not your ChatGPT subscription.\n"
            "Re-login with `codex login` (ChatGPT sign-in), or pass "
            "--allow-api-billing if you intend to use API billing.")


def require_clean_git(cwd):
    rc, out, _ = git(cwd, "status", "--porcelain")
    if rc != 0:
        raise SystemExit("race/relay modes need the project to be a git repo.")
    dirty = [ln for ln in out.splitlines()
             if ln.strip() and ".loopcraft" not in ln]
    if dirty:
        raise SystemExit("race mode needs a clean working tree "
                         "(commit/stash first).")


def race(goal, cwd, args, log):
    """Each contender gets its own git WORKTREE from the same base commit —
    branches alone don't isolate untracked files, caches, or build junk."""
    require_clean_git(cwd)
    _, base_ref, _ = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    base_ref = base_ref.strip() or "main"
    _, base_sha, _ = git(cwd, "rev-parse", "HEAD")
    base_sha = base_sha.strip()
    contenders = [w.strip() for w in args.race_workers.split(",")]
    wt_root = cwd / ".loopcraft" / "worktrees" / f"run-{log.stamp}"
    wt_root.mkdir(parents=True, exist_ok=True)
    entries = []
    for w in contenders:
        branch = f"loopcraft/{w}-{log.stamp}"
        wt = wt_root / w
        log(f"loop4 race: contender '{w}' -> worktree {wt} (branch {branch})")
        rc, _, err = git(cwd, "worktree", "add", "-b", branch,
                         str(wt), base_sha)
        if rc != 0:
            log(f"  worktree add failed: {err.strip()[:300]} — skipping {w}")
            continue
        ok, verdict, result = goal_loop(goal, w, wt, args, log, tag=f"-{w}")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", f"loopcraft[{w}]: {goal[:60]}",
            "--allow-empty")
        _, diff, _ = git(cwd, "diff", "--stat", f"{base_sha}..{branch}")
        entries.append({"worker": w, "branch": branch, "passed": ok,
                        "risk": verdict.get("risk", "high"),
                        "summary": result["report"][:2000],
                        "diffstat": diff[:1500]})
        git(cwd, "worktree", "remove", "--force", str(wt))
    if not entries:
        log("loop4: no contenders completed")
        return False
    passing = [e for e in entries if e["passed"]] or entries
    if len(passing) == 1:
        winner = passing[0]
    else:
        prompt = ("Pick the best solution for the goal. Respond ONLY with "
                  'JSON {"winner": "<worker name>", "why": "..."}.\n'
                  f"GOAL: {goal}\n\n" +
                  "\n\n".join(f"CANDIDATE {e['worker']} "
                              f"(passed={e['passed']}, risk={e['risk']})\n"
                              f"{e['diffstat']}\n{e['summary'][:800]}"
                              for e in passing))
        if args.judge.startswith("openrouter:"):
            raw = openrouter_chat(args.judge.split(":", 1)[1],
                                  [{"role": "user", "content": redact(prompt)}])
        else:
            raw = json.dumps({"winner": passing[0]["worker"]})
        pick = (extract_json(raw) or {}).get("winner")
        winner = next((e for e in passing if e["worker"] == pick), passing[0])
    log(f"loop4 winner: {winner['worker']} on {winner['branch']} "
        f"(passed={winner['passed']}, risk={winner['risk']})")
    if args.merge_winner and winner["passed"]:
        git(cwd, "merge", "--no-ff", winner["branch"],
            "-m", f"loopcraft: merge winner {winner['worker']}")
        log(f"merged {winner['branch']} into {base_ref}")
        # A patch can pass in isolation but break after merge (conflicts,
        # base movement, generated files). Re-verify in the real checkout.
        if args.verify:
            log("  post-merge verify in target checkout...")
            _, pmrc, _ = gather_evidence(cwd, args.verify, args.timeout,
                                         args, log)
            if pmrc not in (None, 0):
                log(f"  POST-MERGE VERIFY FAILED (exit {pmrc}) — rolling back "
                    f"to {base_sha[:8]}; branch {winner['branch']} kept for "
                    "manual inspection")
                git(cwd, "reset", "--hard", base_sha)
                return False
            log("  post-merge verify passed")
    else:
        log("review branches manually, then merge the one you like: "
            + ", ".join(e["branch"] for e in entries))
    return winner["passed"]


def relay(goal, cwd, args, log):
    """Builder writes, reviewer fixes, judge referees. Alternate until pass."""
    order = [w.strip() for w in args.race_workers.split(",")]
    feedback = None
    for attempt in range(1, args.max_attempts + 1):
        w = order[(attempt - 1) % len(order)]
        role = ("builder" if attempt % 2 == 1 else
                "reviewer: audit the previous agent's work, fix defects, "
                "improve quality")
        log(f"loop4 relay attempt {attempt}: {w} ({role.split(':')[0]})")
        prompt = worker_prompt(goal, feedback, attempt, args.verify,
                               extra=f"Your role this round: {role}.")
        result = WORKERS[w](prompt, cwd, args, log)
        log.save(f"relay-{attempt}-{w}.txt", result["report"])
        evidence, verify_rc, has_diff = gather_evidence(
            cwd, args.verify, args.timeout, args, log)
        verdict = decide_verdict(goal, result, evidence, verify_rc,
                                 has_diff, args, cwd, log)
        log(f"  verdict: {verdict['verdict']} (risk {verdict.get('risk')})")
        if verdict["verdict"] == "pass":
            return True
        feedback = verdict.get("feedback")
    return False


# ---------------------------------------------------------------- loop 5

BACKLOG_RE = re.compile(r"^(\s*[-*]\s*)\[( |x|X)\]\s*(.+)$")


def backlog_loop(path, cwd, args, log):
    """Work through unchecked items in a markdown checklist; tick them off."""
    done = 0
    while True:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        todo = None
        for i, line in enumerate(lines):
            m = BACKLOG_RE.match(line)
            if m and m.group(2) == " ":
                todo = (i, m.group(1), m.group(3).strip())
                break
        if todo is None:
            if args.forever:
                log(f"loop5: backlog empty, sleeping {args.sleep}s "
                    "(forever mode)")
                time.sleep(args.sleep)
                continue
            log("loop5: backlog complete.")
            return True
        if args.max_goals and done >= args.max_goals:
            log(f"loop5: hit --max-goals {args.max_goals}, stopping.")
            return True
        i, prefix, goal = todo
        log(f"loop5: next goal -> {goal}")
        ok = dispatch(goal, cwd, args, log)
        done += 1
        if ok:
            lines[i] = f"{prefix}[x] {goal}"
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            log(f"loop5: checked off '{goal[:60]}'")
        else:
            lines[i] = (f"{prefix}[ ] {goal}  "
                        "<!-- loopcraft: failed, see logs -->")
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            log(f"loop5: FAILED '{goal[:60]}' — annotated")
            if args.stop_on_fail:
                log("loop5: --stop-on-fail set, stopping.")
                return False


# ---------------------------------------------------------------- dispatch

def dispatch(goal, cwd, args, log):
    if args.mode == "race":
        return race(goal, cwd, args, log)
    if args.mode == "relay":
        return relay(goal, cwd, args, log)
    ok, _, _ = goal_loop(goal, args.worker, cwd, args, log)
    return ok


def detect_verify(cwd: Path):
    for fname, cmd in VERIFY_HINTS:
        if (cwd / fname).exists():
            return cmd
    return None


def preflight(args, cwd, log):
    needed = {args.worker} | set(w.strip() for w in args.race_workers.split(","))
    if args.mode == "single":
        needed = {args.worker}
    if "claude" in needed:
        rc, _, _ = run_cmd(["claude", "--version"], ".", 30)
        if rc != 0:
            log("WARNING: `claude` CLI not found or not working")
    if "codex" in needed:
        rc, _, _ = run_cmd(["codex", "--version"], ".", 30)
        if rc != 0:
            log("WARNING: `codex` CLI not found or not working")
        else:
            codex_auth_preflight(args, log)
    # billing-routing checks
    for key, cli in (("ANTHROPIC_API_KEY", "claude"),
                     ("OPENAI_API_KEY", "codex")):
        if os.environ.get(key):
            if args.allow_api_billing:
                log(f"NOTE: {key} is set and --allow-api-billing given — "
                    f"{cli} runs may bill your API account.")
            else:
                log(f"NOTE: {key} is set; stripping it from worker "
                    f"subprocesses so {cli} uses its CLI login, not API "
                    "billing. Pass --allow-api-billing to keep it.")
    if "claude" in needed:
        log("NOTE: `claude -p` quota accounting is provider-defined and may "
            "differ from interactive Claude Code usage; run `claude /status` "
            "interactively if unsure.")
    if args.judge.startswith("openrouter:") \
            and not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit(
            "Judge uses OpenRouter but OPENROUTER_API_KEY is not set.\n"
            "Either export it, or use --judge verify-only / claude / codex.")
    if args.judge == "verify-only" and not args.verify:
        raise SystemExit(
            "--judge verify-only needs a --verify command to judge against.\n"
            "Provide --verify \"<cmd>\", or pick an LLM judge "
            "(--judge openrouter:<model> / claude / codex).")
    if args.safety == "yolo" and not args.i_understand_yolo:
        raise SystemExit(
            "--safety yolo disables permission prompts and sandboxing.\n"
            "Run it only in a container/VM or disposable checkout, and "
            "confirm with --i-understand-yolo.")
    # A non-git target produces no diff evidence; with --verify none that
    # leaves the judge nothing objective to check, and runs can "pass" on the
    # agent's word alone. Warn loudly, and point at child repos if this looks
    # like a container directory (-C parent instead of the actual repo).
    rc, _, _ = git(cwd, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        log("WARNING: target is not a git repository — no diff evidence will "
            "be available. Pair with a real --verify, or success can't be "
            "objectively confirmed (see the no-evidence guard).")
        kids = sorted({p.parent.name for p in Path(cwd).glob("*/.git")})
        if kids:
            shown = ", ".join(kids[:6]) + ("…" if len(kids) > 6 else "")
            log(f"  NOTE: this dir contains child git repos ({shown}). Did you "
                "mean to point -C at one of those instead?")


def resolve_verify(args, cwd, log):
    if args.verify == "none":
        args.verify = None
        log("verify: explicitly disabled (judge-only — weaker guarantees)")
        return
    if args.verify:
        return
    hint = detect_verify(cwd)
    msg = ("No --verify command given. Verification is what makes loop 3 "
           "converge instead of rubber-stamping.\n")
    if hint:
        msg += f"Detected project type suggests:  --verify \"{hint}\"\n"
    msg += "Pass --verify \"<cmd>\" or --verify none for docs-only tasks."
    raise SystemExit(msg)


def main():
    ap = argparse.ArgumentParser(
        description="loopcraft v0.2.2: stacked-loop agent harness "
                    "(workers = your local CLI logins, judge = cheap "
                    "OpenRouter). Local/personal use only.")
    ap.add_argument("-g", "--goal", help="single goal to achieve (loop 3/4)")
    ap.add_argument("--backlog", help="markdown checklist of goals (loop 5)")
    ap.add_argument("-C", "--dir", default=".", help="target project directory")
    ap.add_argument("--worker", default=DEFAULT_WORKER,
                    choices=list(WORKERS), help="worker for single mode")
    ap.add_argument("--mode", default="single",
                    choices=["single", "race", "relay"], help="loop-4 strategy")
    ap.add_argument("--race-workers", default="claude,codex",
                    help="comma list of contenders for race/relay")
    ap.add_argument("--judge", default=DEFAULT_JUDGE,
                    help="verify-only (default, no LLM) | openrouter:<model> "
                         "| claude | codex")
    ap.add_argument("--verify",
                    help="shell command that must pass, e.g. 'pytest -q'. "
                         "Use 'none' to skip (docs-only tasks).")
    ap.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    ap.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS,
                    help="cap on claude tool-turns per invocation")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help="seconds per worker invocation")
    ap.add_argument("--claude-model", help="override claude model")
    ap.add_argument("--codex-model", help="override codex model (-m)")
    ap.add_argument("--or-worker-model", default="deepseek/deepseek-chat-v3.1",
                    help="model when --worker openrouter (advisory)")
    ap.add_argument("--safety", default="normal",
                    choices=["strict", "normal", "yolo"],
                    help="strict: tool whitelist / normal: acceptEdits / "
                         "yolo: no prompts + wide sandbox")
    ap.add_argument("--i-understand-yolo", action="store_true",
                    help="required acknowledgement for --safety yolo")
    ap.add_argument("--allowed-tools",
                    help="claude --allowedTools value (overrides profile)")
    ap.add_argument("--allow-api-billing", action="store_true",
                    help="do NOT strip ANTHROPIC_API_KEY/OPENAI_API_KEY from "
                         "worker subprocesses (may bill your API accounts)")
    ap.add_argument("--merge-winner", action="store_true",
                    help="race mode: auto-merge winning branch if it passed")
    ap.add_argument("--max-goals", type=int, default=0,
                    help="loop 5: stop after N goals this run (0 = all)")
    ap.add_argument("--stop-on-fail", action="store_true",
                    help="loop 5: stop at the first failed goal")
    ap.add_argument("--force-unlock", action="store_true",
                    help="take the repo lock even if another run claims it")
    ap.add_argument("--forever", action="store_true",
                    help="loop 5: keep polling the backlog file")
    ap.add_argument("--sleep", type=int, default=300,
                    help="loop 5 forever mode: seconds between polls")
    args = ap.parse_args()

    if not args.goal and not args.backlog:
        ap.error("provide --goal or --backlog")
    if args.forever and not args.max_goals:
        ap.error("--forever requires --max-goals N (quota protection)")
    cwd = Path(args.dir).resolve()
    if not cwd.is_dir():
        raise SystemExit(f"not a directory: {cwd}")

    log = Log(cwd)
    ensure_git_exclude(cwd)
    acquire_lock(cwd, args.force_unlock, log)
    log(f"loopcraft v0.2.2 start  project={cwd}  mode={args.mode}  "
        f"safety={args.safety}  judge={args.judge}")
    resolve_verify(args, cwd, log)
    preflight(args, cwd, log)

    if args.backlog:
        ok = backlog_loop(args.backlog, cwd, args, log)
    else:
        ok = dispatch(args.goal, cwd, args, log)
    cost = (f"  claude-cost=${log.cost_usd:.2f}" if log.cost_usd else "")
    log(f"loopcraft done  success={ok}  logs={log.dir}{cost}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
