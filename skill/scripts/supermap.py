#!/usr/bin/env python3
"""supermap: one map of every project map, and of the work that has no map yet.

Finds every .project-map/ (registered or not), every git repo under your project folders and the
interactive Claude Code sessions behind them. Flags what needs you, what could be lost and what
has gone quiet; writes one self-contained page; restarts any project in one step.

  pm.py super [--days N] [--rescan] [--learn] [--publish] [--json] [--quiet]
                                   scan, write supermap.html and print what needs you
  pm.py super open                 open the page
  pm.py super restart ID [--resume] [--here] [--print] [--copy]
                                   a new terminal tab in the project running claude, primed with a
                                   restart brief (default) or resuming the last session
  pm.py super brief ID             print the restart brief
  pm.py super calls [--json]       every open "Your call" across all maps
  pm.py super answer ID DECISION TEXT
                                   write your answer into that project's plan.md
  pm.py super park ID [--until DATE|14d] [--reason TEXT]   |   unpark ID   |   done ID
  pm.py super publish [--rotate] [--dry-run]
                                   a redacted copy (no paths, session titles or commands) at an
                                   unguessable link
  pm.py super nudge                what slipped, as JSON, for a daily review
  pm.py super learn                set "gone quiet" from your own commit history
  pm.py super preview              screenshots at phone and laptop width

Standard library only. Data: ~/.local/share/project-map/super/ (mode 0700).
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import difflib
import fnmatch
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PM = None  # pm.py, bound by bind(): config, registry, the map contract and hosting
HOME = Path.home()
CLAUDE_PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS_DIR", HOME / ".claude" / "projects"))
STYLE_MEMORY = HOME / ".claude" / "agent-memory" / "project-map"
DEFAULT_ROOTS = ["~/dev", "~/code", "~/src", "~/projects", "~/Developer", "~/work", "~/repos", "~/git"]
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "Pods", "DerivedData", "venv", "__pycache__"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
ASKING_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
TEMP_PREFIXES = ("/private/tmp/", "/tmp/", "/private/var/folders/", "/var/folders/")
QUIET_DEFAULT = 6  # days; `learn` replaces it with the point where your quiet projects more often stay quiet
WALK_TTL = 600  # seconds a folder walk is reused
REPO_TTL = 86400  # a repo nobody touched is rechecked at least daily
WARM_DAYS = 3  # repos with work this recent are rechecked on every scan
IDLE_MINUTES = 30  # a session quiet this long has ended
CACHE_VERSION = 3  # bump when parse_session or repo_state change what they record
UNMAPPED_DAYS = 4  # active days in the window that make a project worth a map
GENERIC_NAMES = {"src", "app", "web", "api", "code", "repo", "main", "server", "client", "frontend", "backend",
                 "site", "ui", "core", "lib", "docs", "origin", "service"}
WAITING_ON_YOU = re.compile(r"\b(you|your|decision|call|approv\w*|sign[- ]?off|answer|account|login|token|"
                            r"credential\w*|key|access|budget|review)\b", re.I)
TS = re.compile(r'"timestamp":"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})')
PATHS = re.compile(r"(?:(?<=[\s'\"=:(])|^)((?:/Users|/home|/private/tmp|/tmp|~)/[^\s'\"`;|&<>)]+)")
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, data TEXT);
CREATE TABLE IF NOT EXISTS repos (root TEXT PRIMARY KEY, fingerprint TEXT, checked REAL, data TEXT);
CREATE TABLE IF NOT EXISTS snapshots (day TEXT, root TEXT, data TEXT, PRIMARY KEY (day, root));
CREATE TABLE IF NOT EXISTS marks (root TEXT PRIMARY KEY, kind TEXT, reason TEXT, until TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS answers (root TEXT, decision TEXT, answer TEXT, at TEXT, PRIMARY KEY (root, decision));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def bind(pm) -> None:
    global PM
    PM = pm


# ---------------------------------------------------------------- settings and storage

def home_dir() -> Path:
    return PM.DATA_DIR / "super"


def settings() -> dict:
    """The `super` section of config.json, with defaults."""
    cfg = PM.load_config().get("super", {})
    roots = cfg.get("roots") or [r for r in DEFAULT_ROOTS if Path(r).expanduser().is_dir()]
    return {"roots": [Path(r).expanduser().resolve() for r in roots], "depth": int(cfg.get("depth", 4)),
            "days": int(cfg.get("days", 30)), "ignore": [str(Path(g).expanduser()) for g in cfg.get("ignore", [])],
            "emails": [e.lower() for e in cfg.get("emails", [])], "areas": cfg.get("areas", {}),
            "claude": cfg.get("claude")}


def connect() -> sqlite3.Connection:
    folder = home_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / "super.db"
    fresh = not path.exists()
    db = sqlite3.connect(path)
    if fresh:
        os.chmod(path, 0o600)
    db.executescript(SCHEMA)
    return db


def meta(db, key: str, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(db, key: str, value) -> None:
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))
    db.commit()


def private_text(path: Path, text: str) -> None:
    PM.private_write(path, text)


def local_day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).date().isoformat()


def iso_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.astimezone()).timestamp()


def tilde(path: str) -> str:
    home = str(HOME)
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


# ---------------------------------------------------------------- finding projects

def walk(roots: list[Path], depth: int) -> tuple[list[str], list[str]]:
    """Every git repo and every .project-map folder under the roots, `depth` folders down."""
    repos, maps = set(), set()
    for base in roots:
        base_level = str(base).rstrip(os.sep).count(os.sep)
        for folder, dirs, files in os.walk(base):
            if ".git" in dirs or ".git" in files:
                repos.add(folder)
            if ".project-map" in dirs and any(
                    (Path(folder) / ".project-map" / name).exists() for name in ("map.json", "index.html")):
                maps.add(folder)
            level = folder.count(os.sep) - base_level
            dirs[:] = [d for d in dirs if level < depth and not d.startswith(".") and d not in SKIP_DIRS]
    return sorted(repos), sorted(maps)


def discover(db, cfg: dict, rescan: bool) -> tuple[list[str], list[str]]:
    """The walk, reused for WALK_TTL seconds, plus every registered map wherever it lives."""
    key = [str(r) for r in cfg["roots"]] + [cfg["depth"]]
    cached = meta(db, "walk")
    if cached and not rescan and cached["key"] == key and time.time() - cached["at"] < WALK_TTL:
        repos, maps = cached["repos"], cached["maps"]
    else:
        repos, maps = walk(cfg["roots"], cfg["depth"])
        set_meta(db, "walk", {"key": key, "at": time.time(), "repos": repos, "maps": maps})
    maps = set(maps)
    for entry in PM.load_links().get("projects", {}).values():
        path = entry.get("path")
        if path and (Path(path) / ".project-map" / "map.json").exists():
            maps.add(path)
    ignored = lambda p: any(fnmatch.fnmatch(p, g) or p.startswith(g.rstrip("*") + os.sep) for g in cfg["ignore"])
    return [r for r in repos if not ignored(r)], sorted(m for m in maps if not ignored(m))


# ---------------------------------------------------------------- git

def git(root: str, *args: str, timeout: int = 30) -> str | None:
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def git_dirs(root: str) -> tuple[Path, Path] | None:
    """The repo's git dir and its common dir (they differ for a linked worktree)."""
    dot = Path(root) / ".git"
    if dot.is_dir():
        return dot, dot
    try:
        found = re.match(r"gitdir:\s*(.+)", dot.read_text().strip())
    except OSError:
        return None
    if not found:
        return None
    gitdir = (Path(root) / found.group(1).strip()).resolve()
    try:
        return gitdir, (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
    except OSError:
        return gitdir, gitdir


def fingerprint(gitdir: Path, common: Path) -> str:
    """Changes whenever git itself changes the repo: a commit, checkout, stash, fetch or index refresh."""
    stamps = []
    for path in (gitdir / "HEAD", gitdir / "index", gitdir / "logs" / "HEAD", common / "packed-refs",
                 common / "refs" / "heads", common / "logs" / "refs" / "stash", common / "FETCH_HEAD"):
        try:
            stamps.append(str(path.stat().st_mtime_ns))
        except OSError:
            stamps.append("-")
    return ".".join(stamps)


def identities(repo_roots: list[str], cfg: dict) -> tuple[set, set]:
    """Your commit emails and names: global git config, each repo's own user.email, and config.json."""
    emails, names = set(cfg["emails"]), set()
    for key, into in (("user.email", emails), ("user.name", names)):
        out = subprocess.run(["git", "config", "--global", "--get-all", key], capture_output=True, text=True)
        into.update(v.strip().lower() if into is emails else v.strip() for v in out.stdout.splitlines() if v.strip())
    for root in repo_roots:
        dirs = git_dirs(root)
        if not dirs:
            continue
        try:
            text = (dirs[1] / "config").read_text(errors="replace")
        except OSError:
            continue
        for found in re.finditer(r"(?m)^\s*email\s*=\s*(\S+@\S+)\s*$", text):
            emails.add(found.group(1).lower())
    return emails, names


def parse_status(text: str) -> dict:
    """`git status --porcelain=v2 --branch`: the branch, its upstream and what isn't committed."""
    state = {"branch": None, "upstream": None, "ahead": 0, "initial": False, "dirty": 0, "untracked": 0, "sample": []}
    for line in text.splitlines():
        if line.startswith("#"):
            if line.startswith("# branch.head "):
                state["branch"] = line[14:]
            elif line.startswith("# branch.oid "):
                state["initial"] = line.endswith("(initial)")
            elif line.startswith("# branch.upstream "):
                state["upstream"] = line[18:]
            elif line.startswith("# branch.ab "):
                state["ahead"] = int(line.split()[2].lstrip("+"))
            continue
        if line.startswith("? "):
            state["untracked"] += 1
            path = line[2:]
        elif line[:2] in ("1 ", "2 ", "u "):
            state["dirty"] += 1
            fields = {"1": 8, "2": 9, "u": 10}[line[0]]
            path = line.split(" ", fields)[-1].split("\t")[0]
        else:
            continue
        if len(state["sample"]) < 8:
            state["sample"].append(path)
    return state


def stash_state(common: Path) -> dict:
    try:
        lines = (common / "logs" / "refs" / "stash").read_text(errors="replace").splitlines()
    except OSError:
        return {"stashes": 0}
    newest = re.search(r"> (\d+) [+-]\d{4}\t(.*)$", lines[-1]) if lines else None
    return {"stashes": len(lines), "stashAt": int(newest.group(1)) if newest else None,
            "stashMessage": newest.group(2)[:100] if newest else ""}


def repo_state(root: str, rel: str | None, since: float, people: tuple[set, set]) -> dict:
    """Git facts for one project. A project in a repo's subfolder (rel) gets only its own changes and commits."""
    dirs = git_dirs(root if not rel else str(Path(root).parents[len(Path(rel).parts) - 1]))
    scope = ["--", "."] if rel else []
    status = git(root, "status", "--porcelain=v2", "--branch", *scope)
    if status is None or not dirs:
        return {"error": "git could not read this repo"}
    state = dict(parse_status(status), v=CACHE_VERSION)
    if state["initial"]:  # no commits to date it by, so date it by its files
        stamps = []
        for name in state["sample"]:
            try:
                stamps.append((Path(root) / name).stat().st_mtime)
            except OSError:
                pass
        state["touchedAt"] = max(stamps, default=None)
    emails, names = people

    def own(line: str) -> tuple[int, str] | None:
        parts = line.split("\x1f")
        if len(parts) == 4 and (parts[1].lower() in emails or parts[2] in names):
            return int(parts[0]), parts[3][:100]
        return None

    since_iso = dt.datetime.fromtimestamp(since).isoformat(timespec="seconds")
    recent = git(root, "log", "--all", f"--since={since_iso}", "--format=%ct%x1f%ae%x1f%an%x1f%s", *scope) or ""
    mine = [c for c in map(own, recent.splitlines()) if c]
    state["commits"] = sorted(([ts, subject] for ts, subject in mine), reverse=True)
    state["lastOwn"] = max((ts for ts, _ in mine), default=None)
    if not state["lastOwn"] and not state["initial"]:
        older = git(root, "log", "--all", "-n", "100", "--format=%ct%x1f%ae%x1f%an%x1f%s", *scope) or ""
        found = [c for c in map(own, older.splitlines()) if c]
        state["lastOwn"] = found[0][0] if found else None
        state["everOwn"] = bool(found)
    else:
        state["everOwn"] = bool(state["lastOwn"])
    if rel:
        return state
    gitdir, common = dirs
    try:
        state["remote"] = '[remote "' in (common / "config").read_text(errors="replace")
    except OSError:
        state["remote"] = False
    if state["remote"] and not state["initial"]:
        count = git(root, "rev-list", "--count", "--branches", "--not", "--remotes")
        state["unpushed"] = int(count.strip()) if count and count.strip().isdigit() else 0
    if gitdir == common:
        state.update(stash_state(common))
    return state


def scan_repos(db, targets: list[tuple[str, str | None]], since: float, people, warm: set, rescan: bool) -> dict:
    """Git state for each (root, rel) target. Quiet repos come from the cache until git changes them or a day passes."""
    now = time.time()
    cached = {row[0]: (row[1], row[2], row[3]) for row in db.execute("SELECT root, fingerprint, checked, data FROM repos")}
    results, todo = {}, []
    for root, rel in targets:
        top = root if not rel else str(Path(root).parents[len(Path(rel).parts) - 1])
        dirs = git_dirs(top)
        if not dirs:
            continue
        old = cached.get(root)
        same = (old and not rescan and old[0] == fingerprint(*dirs) and now - old[1] < REPO_TTL
                and json.loads(old[2]).get("v") == CACHE_VERSION)
        if same and root not in warm:
            results[root] = json.loads(old[2])
        else:  # warm and unchanged by git: only the working tree can have moved, so only status runs
            todo.append((root, rel, dirs, json.loads(old[2]) if same else None))

    def check(job):
        root, rel, dirs, kept = job
        if kept is not None and "error" not in kept:
            status = git(root, "status", "--porcelain=v2", "--branch", *(["--", "."] if rel else []))
            state = dict(kept, **parse_status(status)) if status is not None else repo_state(root, rel, since, people)
        else:
            state = repo_state(root, rel, since, people)
        return root, state, fingerprint(*dirs)  # after status, which may refresh the index

    with ThreadPoolExecutor(16) as pool:
        for root, state, stamp in pool.map(check, todo):
            results[root] = state
            db.execute("INSERT OR REPLACE INTO repos VALUES (?, ?, ?, ?)", (root, stamp, now, json.dumps(state)))
    db.commit()
    return results


# ---------------------------------------------------------------- Claude Code sessions

def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")
    return ""


def ending(records: list[dict]) -> tuple[str, str]:
    """How a session ended: done, question (Claude asked you something), mid-task, unanswered,
    interrupted or error, and the words it ended on."""
    for record in reversed(records):
        if record.get("isMeta") or record.get("isSidechain"):
            continue
        message = record.get("message") or {}
        content = message.get("content")
        if record.get("type") == "assistant":
            if record.get("isApiErrorMessage"):
                return "error", text_of(content)[:280]
            items = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            items = [i for i in items if isinstance(i, dict) and i.get("type") in ("text", "tool_use")]
            if not items:
                continue
            last = items[-1]
            if last.get("type") == "tool_use":
                if last.get("name") in ASKING_TOOLS:
                    asked = last.get("input") or {}
                    questions = asked.get("questions") or [{}]
                    return "question", str(questions[0].get("question") or asked.get("plan") or "")[:280]
                return "mid-task", f"was running {last.get('name')}"
            words = (last.get("text") or "").strip()
            tail = words if len(words) <= 280 else "…" + words[-280:].split(" ", 1)[-1]  # start on a whole word
            return ("question" if words.rstrip("*_`) ").endswith("?") else "done"), tail
        if record.get("type") == "user":
            if isinstance(content, list) and any(isinstance(i, dict) and i.get("type") == "tool_result" for i in content):
                return "mid-task", "stopped after a tool finished"
            words = text_of(content).strip()
            if words.startswith("[Request interrupted"):
                return "interrupted", ""
            if record.get("promptSource") in ("typed", "queued"):
                return "unanswered", words[:280]
    return "done", ""


def parse_session(path: str) -> dict:
    """One transcript, cheaply: substring tests first, JSON only for the lines that matter."""
    info = {"human": False, "v": CACHE_VERSION}
    entry, typed, title, first_prompt, cwd, branch, sid = None, 0, None, None, None, None, None
    days, edits, mentions = collections.Counter(), {}, collections.Counter()
    first_ts = last_ts = None
    tail = collections.deque(maxlen=40)
    day_of = {}
    with open(path, errors="replace") as stream:
        for number, line in enumerate(stream):
            if number == 400 and entry != "cli" and not typed:
                return info  # headless (claude -p, the Agent SDK, scheduled jobs): not your work
            if entry is None and '"entrypoint":"' in line:
                found = re.search(r'"entrypoint":"([^"]+)"', line)
                entry = found.group(1) if found else None
            if '"type":"ai-title"' in line:
                try:
                    title = json.loads(line).get("aiTitle") or title
                except ValueError:
                    pass
                continue
            is_user, is_assistant = '"type":"user"' in line, '"type":"assistant"' in line
            if not (is_user or is_assistant):
                continue
            stamp = TS.search(line)
            if stamp:
                minute = stamp.group(1)
                if minute not in day_of:
                    when = dt.datetime.fromisoformat(minute + ":00+00:00").astimezone()
                    day_of[minute] = (when.date().isoformat(), when.timestamp())
                day, moment = day_of[minute]
                days[day] += 1
                first_ts = first_ts or moment
                last_ts = moment
            else:
                day = None
            if '"promptSource":"typed"' in line or '"promptSource":"queued"' in line:
                typed += 1
                if first_prompt is None:
                    try:
                        first_prompt = text_of(json.loads(line).get("message", {}).get("content")).strip()[:120]
                    except ValueError:
                        pass
            if cwd is None and '"cwd":"' in line:
                try:
                    record = json.loads(line)
                    cwd, branch, sid = record.get("cwd"), record.get("gitBranch"), record.get("sessionId")
                except ValueError:
                    pass
            if is_assistant and '"tool_use"' in line and ('"name":"Bash"' in line or any(f'"name":"{t}"' in line for t in EDIT_TOOLS)):
                try:
                    record = json.loads(line)
                except ValueError:
                    record = {}
                if any(f'"name":"{tool}"' in line for tool in EDIT_TOOLS):
                    for item in record.get("message", {}).get("content") or []:
                        if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") in EDIT_TOOLS:
                            target = (item.get("input") or {}).get("file_path") or (item.get("input") or {}).get("notebook_path")
                            if target and day:
                                edits.setdefault(target, collections.Counter())[day] += 1
                if '"name":"Bash"' in line:
                    for item in record.get("message", {}).get("content") or []:
                        if isinstance(item, dict) and item.get("name") == "Bash":
                            for found in PATHS.findall(str((item.get("input") or {}).get("command", ""))):
                                mentions[os.path.expanduser(found.rstrip(".,"))] += 1
            tail.append(line)
    if entry != "cli" or not typed:
        return info
    records = []
    for line in tail:
        try:
            records.append(json.loads(line))
        except ValueError:
            pass
    ended, last_words = ending(records)
    return {"human": True, "v": CACHE_VERSION, "id": sid or Path(path).stem, "cwd": cwd, "branch": branch,
            "title": (title or first_prompt or "").strip()[:120], "start": first_ts, "end": last_ts,
            "days": dict(days), "typed": typed, "edits": {p: dict(c) for p, c in list(edits.items())[:400]},
            "mentions": dict(mentions.most_common(200)), "ended": ended, "lastWords": last_words}


def scan_sessions(db, since: float) -> list[dict]:
    """Interactive sessions active since `since`, parsed once and cached by size and mtime."""
    cached = {row[0]: (row[1], row[2], row[3]) for row in db.execute("SELECT path, size, mtime, data FROM sessions")}
    found, todo = [], []
    if not CLAUDE_PROJECTS.is_dir():
        return []
    for folder in os.scandir(CLAUDE_PROJECTS):
        if not folder.is_dir():
            continue
        for item in os.scandir(folder.path):
            if not item.name.endswith(".jsonl"):
                continue
            stat = item.stat()
            if stat.st_mtime < since:
                continue
            old = cached.get(item.path)
            data = json.loads(old[2]) if old and old[0] == stat.st_size and old[1] == stat.st_mtime else None
            if data and data.get("v") == CACHE_VERSION:
                found.append(data)
            else:
                todo.append((item.path, stat.st_size, stat.st_mtime))

    def parse(job):
        path, size, mtime = job
        try:
            return job, parse_session(path)
        except (OSError, ValueError) as problem:
            return job, {"human": False, "error": str(problem)[:200]}

    with ThreadPoolExecutor(8) as pool:
        for (path, size, mtime), data in pool.map(parse, todo):
            found.append(data)
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?)", (path, size, mtime, json.dumps(data)))
    db.commit()
    return [s for s in found if s.get("human") and (s.get("end") or 0) >= since]


# ---------------------------------------------------------------- maps

def find_call_line(lines: list[str], call: dict) -> int | None:
    """The plan.md line that carries a decision: its id as the list item's label, else its question."""
    did = re.escape(str(call.get("id") or ""))
    if did:
        label = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*(?:\[[ x]\]\s*)?[*_`]*" + did + r"[*_`]*\s*[:.)—-]", re.I)
        for number, line in enumerate(lines):
            if label.search(line):
                return number
    question = " ".join(str(call.get("question") or "").split())[:40]
    if len(question) >= 12:
        for number, line in enumerate(lines):
            if question in " ".join(line.split()):
                return number
    return None


def answer_in(line: str) -> str:
    found = re.search(r"Answer:(.*)$", line)
    return found.group(1).strip(" _-–—") if found else ""


def read_map(root: str) -> dict:
    """A project's map: where it stands, what's stuck, which calls are still open, and the days it was drawn."""
    folder = Path(root) / ".project-map"
    try:
        data = json.loads((folder / "map.json").read_text())
    except (OSError, ValueError) as problem:
        return {"problem": f"map.json unreadable: {str(problem)[:120]}", "draws": []}
    problems = PM.check_map(data)
    if problems:
        return {"problem": f"map.json breaks the contract: {problems[0]}", "draws": [],
                "title": data.get("project") if isinstance(data, dict) else None}
    plan = ""
    try:
        plan = (folder / "plan.md").read_text(errors="replace")
    except OSError:
        pass
    lines = plan.splitlines()
    calls = []
    for decision in data.get("decisions", []):
        if decision.get("status") != "open":
            continue
        number = find_call_line(lines, decision)
        calls.append({"id": str(decision.get("id") or ""), "question": decision.get("question", ""),
                      "options": [str(o) for o in decision.get("options") or []][:6],
                      "default": decision.get("defaultChoice", ""),
                      "answered": answer_in(lines[number]) if number is not None else ""})
    draws = set()
    try:
        for found in re.finditer(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", (folder / "log.md").read_text(errors="replace")):
            draws.add(local_day(iso_ts(f"{found.group(1)}T{found.group(2)}:00+00:00")))
    except OSError:
        pass
    drawn = iso_ts(data.get("generatedAt"))
    if drawn:
        draws.add(local_day(drawn))
    parts = data.get("parts", [])
    behind = None
    if data.get("commit"):
        count = git(root, "rev-list", "--count", f"{data['commit']}..HEAD")
        behind = int(count.strip()) if count and count.strip().isdigit() else None
    return {"title": data.get("project"), "generatedAt": data.get("generatedAt"), "drawn": drawn,
            "commit": data.get("commit"), "behind": behind, "milestone": data["nextMilestone"].get("name"),
            "left": data["nextMilestone"].get("left", 0), "items": data["nextMilestone"].get("items", [])[:12],
            "nextStep": data["nextStep"].get("text"), "why": data["nextStep"].get("why"),
            "done": sum(p.get("done", 0) for p in parts), "total": sum(p.get("total", 0) for p in parts),
            "parts": [{"name": p.get("name"), "status": p.get("status"), "done": p.get("done"), "total": p.get("total"),
                       "waitingOn": p.get("waitingOn")} for p in parts],
            "calls": calls, "url": data.get("url"), "draws": sorted(draws),
            "index": str(folder / "index.html") if (folder / "index.html").exists() else None}


# ---------------------------------------------------------------- the model

class Finder:
    """Which project a path belongs to: the deepest project root above it (worktrees count as their repo)."""

    def __init__(self, roots: dict):
        self.roots = roots  # root -> project root it belongs to
        self.memo = {}

    def __call__(self, path: str | None) -> str | None:
        if not path:
            return None
        if path in self.memo:
            return self.memo[path]
        probe, found = path.rstrip(os.sep), None
        while probe and probe != os.sep:
            if probe in self.roots:
                found = self.roots[probe]
                break
            probe = os.path.dirname(probe)
        self.memo[path] = found
        return found


def area_of(root: str, cfg: dict) -> str:
    for prefix, name in sorted(cfg["areas"].items(), key=lambda kv: -len(kv[0])):
        prefix = str(Path(prefix).expanduser())
        if root == prefix or root.startswith(prefix + os.sep):
            return name
    for base in cfg["roots"]:
        if root.startswith(str(base) + os.sep):
            parts = Path(root).relative_to(base).parts
            return parts[0] if len(parts) > 1 else "top level"
    return "temp folders" if root.startswith(TEMP_PREFIXES) else "elsewhere"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24].strip("-") or "project"


def until_date(text: str | None) -> str | None:
    if not text:
        return None
    found = re.fullmatch(r"(\d+)\s*([dw])", text.strip())
    if found:
        days = int(found.group(1)) * (7 if found.group(2) == "w" else 1)
        return (dt.date.today() + dt.timedelta(days=days)).isoformat()
    try:
        return dt.date.fromisoformat(text.strip()).isoformat()
    except ValueError:
        raise PM.Fail("--until takes a date (2026-11-01) or a length (14d, 2w)") from None


def flags_for(p: dict, quiet_days: int, now: float) -> list[dict]:
    """What to tell you about one project, each flag with the view it belongs to and a severity (3 = act now)."""
    flags = []
    def add(view, code, sev, text, fix=""):
        flags.append({"view": view, "code": code, "sev": sev, "text": text, "fix": fix})
    g, m = p.get("git") or {}, p.get("map") or {}
    idle_hours = (now - p["lastTs"]) / 3600 if p.get("lastTs") else 1e9
    changed = g.get("dirty", 0) + g.get("untracked", 0)
    local_only = g and not g.get("remote") and not p.get("sub")
    recent = p["activeDays"] > 0  # you worked on it inside the window
    if p.get("missing"):
        add("risk", "missing", 2, "registered map's folder is gone" + (f"; a folder with this name is at {p['movedTo']}" if p.get("movedTo") else ""),
            "run pm.py publish in its new folder, or drop it from links.json")
    if g.get("initial") and changed:
        touched = g.get("touchedAt") or 0
        age = (now - touched) / 86400 if touched else 1e9
        sev = 3 if age <= 30 else 2 if age <= 90 else 1
        when = f", last changed {fmt_age(age * 24)} ago" if touched else ""
        add("risk", "never-committed", sev, f"never committed: {plural(changed, 'file')} exist only here{when}",
            "git add and commit it (or delete it if it was scratch)")
    elif changed and idle_hours >= 24:
        # recent work of yours that isn't committed matters most; a remote-less repo makes it worse
        sev = min(3, (2 if recent else 1) + (1 if local_only else 0)) if g.get("everOwn") or m else 1
        where = "; no remote, so only on this Mac" if local_only else ""
        new = f" ({g['untracked']} never added)" if g.get("untracked") else ""
        add("risk", "uncommitted", sev, f"{plural(changed, 'uncommitted file')}{new}{where}, untouched {fmt_age(idle_hours)}",
            "commit it, or stash it with a message" + ("; then add a remote" if local_only else ""))
    if local_only and g.get("everOwn") and not (g.get("initial") or (changed and idle_hours >= 24)):
        add("risk", "no-remote", 1, "no remote: every commit exists only on this Mac", "add one: gh repo create --private --source . --push")
    if g.get("unpushed") and idle_hours >= 24:
        add("risk", "unpushed", 2 if recent else 1, f"{g['unpushed']} commits on no remote (local branches)", "push the branches")
    if g.get("stashes"):
        age = (now - g["stashAt"]) / 86400 if g.get("stashAt") else 0
        message = f": {g['stashMessage']}" if g.get("stashMessage") else ""
        add("risk", "stash", 2 if age >= 7 else 1, f"{g['stashes']} stashed change{'s' if g['stashes'] > 1 else ''}, newest {fmt_age(age * 24)} old{message}",
            "look with git stash show -p, then apply or drop it")
    if p.get("temp"):
        add("risk", "temp-folder", 2, "work in a temp folder that a reboot or cleanup deletes", "move what matters into your projects folder")
    if m.get("problem"):
        add("risk", "bad-map", 1, m["problem"])
    if p.get("map") and not p.get("registered") and not m.get("problem"):
        add("risk", "unregistered-map", 1, "map isn't in the registry, so `where --all` and Orbital miss it "
                                           "(pm.py publish --local adds it without hosting it)")

    open_calls = [c for c in m.get("calls", []) if not c.get("answered")]
    if open_calls:
        days = (now - m["drawn"]) / 86400 if m.get("drawn") else 0
        add("needs", "calls", 2 if days >= 2 else 1, f"{len(open_calls)} open call{'s' if len(open_calls) > 1 else ''} on the map for {fmt_age(days * 24)}")
    for part in m.get("parts", []):
        if part.get("status") == "stuck" and WAITING_ON_YOU.search(str(part.get("waitingOn") or "")):
            add("needs", "stuck-on-you", 2, f"{part['name']} is stuck waiting on: {part['waitingOn']}")
    last = p["sessions"][0] if p.get("sessions") else None
    if last and (now - (last.get("end") or now)) / 60 >= IDLE_MINUTES:
        if last["ended"] == "question":
            add("needs", "session-question", 2 if now - last["end"] < 7 * 86400 else 1, f"your last session ended on a question to you, {fmt_age((now - last['end']) / 3600)} ago")
        elif last["ended"] in ("mid-task", "unanswered", "error"):
            add("needs", "session-stopped", 1, f"your last session stopped mid-task ({last['ended']}), {fmt_age((now - last['end']) / 3600)} ago")

    if p["state"] == "quiet":
        loops = []
        if m.get("left"):
            loops.append(f"{m['left']} left before {m.get('milestone') or 'the next milestone'}")
        if open_calls:
            loops.append(f"{len(open_calls)} open calls")
        if changed:
            loops.append(plural(changed, "uncommitted file"))
        add("quiet", "quiet", 2 if loops else 1, f"quiet for {p['quietDays']} days" + (": " + "; ".join(loops) if loops else ""))
    if not p.get("map") and not p.get("temp") and p["activeDays"] >= UNMAPPED_DAYS:
        add("unmapped", "unmapped", 1, f"worked on {p['activeDays']} days in the window, no map")
    if p["state"] in ("parked", "done"):
        keep = {"parked": ("risk",), "done": ()}[p["state"]]
        flags = [f for f in flags if f["view"] in keep and (p["state"] != "done" or f["sev"] >= 3)]
    return sorted(flags, key=lambda f: -f["sev"])


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def fmt_age(hours: float) -> str:
    if hours < 1:
        return "under an hour"
    if hours < 48:
        return f"{int(hours)} hour{'s' if int(hours) != 1 else ''}"
    days = int(hours // 24)
    return f"{days} days" if days < 60 else f"{days // 30} months"


def build(cfg: dict, tops: list[str], map_roots: list[str], states: dict, maps: dict, sessions: list[dict],
          links: dict, marks: dict, answers: dict, quiet_days: int, now: float, since: float) -> list[dict]:
    """Join git, maps, sessions and your marks into one record per project."""
    registered = {entry.get("path"): pid for pid, entry in links.get("projects", {}).items() if entry.get("path")}
    alias, projects = {}, {}
    in_repo = Finder({r: r for r in tops})
    for root in tops:
        dirs = git_dirs(root)
        main = str(dirs[1].parent) if dirs and dirs[0] != dirs[1] and dirs[1].name == ".git" else root
        alias[root] = main if main in tops or main == root else root
    for root in map_roots:
        alias[root] = root
    find = Finder(alias)

    for root in set(alias.values()):
        top = None
        if root not in tops:
            top = in_repo(os.path.dirname(root))
        projects[root] = {"root": root, "sub": bool(top), "registered": root in registered,
                          "git": states.get(root), "days": collections.defaultdict(dict), "sessions": [],
                          "worktrees": [w for w, main in alias.items() if main == root and w != root]}
    for worktree, main in alias.items():
        if worktree != main and worktree in states and main in projects:
            state = states[worktree]
            g = projects[main]["git"] = dict(projects[main]["git"] or {})
            # a worktree shares its repo's history (git log --all already saw its commits): add only its changes
            g["dirty"] = g.get("dirty", 0) + state.get("dirty", 0)
            g["untracked"] = g.get("untracked", 0) + state.get("untracked", 0)

    for session in sessions:
        cwd = session.get("cwd") or ""
        touched, edit_days = collections.Counter(), collections.defaultdict(collections.Counter)
        for path, days in session.get("edits", {}).items():
            root = find(path)
            if root:
                for day, count in days.items():
                    edit_days[root][day] += count
                    touched[root] += count
        mentioned = collections.Counter()
        for path, count in session.get("mentions", {}).items():
            root = find(path)
            if root:
                mentioned[root] += count
        # shell commands that name paths are mostly reading; they credit a project only when they point at one
        top = mentioned.most_common(1)
        by_shell = [top[0][0]] if top and top[0][1] >= 3 and top[0][1] * 2 >= sum(mentioned.values()) else []
        targets = sorted(touched) or by_shell or [r for r in [find(cwd)] if r]
        if not targets and cwd.startswith(TEMP_PREFIXES) and os.path.isdir(cwd):
            projects.setdefault(cwd, {"root": cwd, "temp": True, "git": None, "days": collections.defaultdict(dict),
                                      "sessions": [], "worktrees": [], "sub": False, "registered": False})
            targets = [cwd]
        for root in targets:
            record = projects[root]
            record["sessions"].append(session)
            for day, count in (edit_days[root] if edit_days.get(root) else session.get("days", {})).items():
                if iso_day_ts(day) >= since - 86400:
                    record["days"][day]["s"] = record["days"][day].get("s", 0) + 1
                    if edit_days.get(root):
                        record["days"][day]["e"] = record["days"][day].get("e", 0) + count

    for root, data in maps.items():
        if root in projects:
            projects[root]["map"] = data
            for day in data.get("draws", []):
                if iso_day_ts(day) >= since - 86400:
                    projects[root]["days"][day]["m"] = projects[root]["days"][day].get("m", 0) + 1
            for call in data.get("calls", []):
                call["answered"] = call["answered"] or answers.get((root, call["id"]), "")
    for path, pid in registered.items():
        if path not in projects and not (Path(path) / ".project-map" / "map.json").exists():
            name = Path(path).name
            moved = next((r for r in projects if Path(r).name == name), None)
            projects[path] = {"root": path, "missing": True, "movedTo": moved, "git": None, "days": {}, "sessions": [],
                              "worktrees": [], "sub": False, "registered": True, "id": pid}

    out, today = [], dt.date.today()
    for root, p in projects.items():
        g = p.get("git") or {}
        for ts, _ in g.get("commits", []):
            if ts >= since:
                day = local_day(ts)
                p["days"][day]["c"] = p["days"][day].get("c", 0) + 1
        p["sessions"].sort(key=lambda s: -(s.get("end") or 0))
        stamps = [g.get("lastOwn") or 0, (p.get("map") or {}).get("drawn") or 0] + [s.get("end") or 0 for s in p["sessions"]]
        p["lastTs"] = max(stamps) or None
        p["quietDays"] = (today - dt.date.fromtimestamp(p["lastTs"])).days if p["lastTs"] else None
        p["activeDays"] = sum(1 for day in p["days"] if iso_day_ts(day) >= since)
        mark = marks.get(root)
        loops = bool((p.get("map") or {}).get("left") or g.get("dirty") or g.get("untracked") or g.get("unpushed"))
        if mark and mark["kind"] == "done":
            p["state"] = "done"
        elif mark and mark["kind"] == "parked" and (not mark.get("until") or mark["until"] >= today.isoformat()):
            p["state"] = "parked"
        elif p["quietDays"] is not None and p["quietDays"] < quiet_days:
            p["state"] = "moving"
        elif p["activeDays"] and (loops or p["activeDays"] >= 2 or p.get("map")):
            p["state"] = "quiet"  # worked on inside the window, then stopped with something still open
        else:
            p["state"] = "idle"
        p["mark"] = mark
        p["flags"] = flags_for(p, quiet_days, now)
        if not (p["activeDays"] or p.get("map") or p.get("missing") or p.get("temp") or mark
                or any(f["view"] == "risk" and f["sev"] >= 2 for f in p["flags"])):
            continue
        out.append(p)
    return out


def iso_day_ts(day: str) -> float:
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(12)).timestamp()


def finish(projects: list[dict], cfg: dict, links: dict) -> list[dict]:
    """Names, ids, areas and the shape the page and the brief read."""
    registered = {entry.get("path"): pid for pid, entry in links.get("projects", {}).items() if entry.get("path")}
    names = collections.Counter()
    for p in projects:
        m = p.get("map") or {}
        folder = Path(p["root"])
        plain = f"{folder.parent.name}/{folder.name}" if folder.name.lower() in GENERIC_NAMES else folder.name
        p["name"] = m.get("title") or (tilde(p["root"]) if p.get("temp") else plain)
        p["id"] = p.get("id") or registered.get(p["root"]) or slug(Path(p["root"]).name)
        names[p["id"]] += 1
    for p in projects:
        if names[p["id"]] > 1 and p["root"] not in registered:
            p["id"] = f"{p['id'][:18]}-{short_hash(p['root'])}"
        p["area"] = area_of(p["root"], cfg)
        g = p.get("git") or {}
        p["git"] = {k: g.get(k) for k in ("branch", "dirty", "untracked", "remote", "unpushed", "stashes", "stashMessage",
                                          "initial", "sample", "lastOwn", "touchedAt")} if g else None
        if g:
            p["git"]["recent"] = [subject for _, subject in g.get("commits", [])[:6]]
        p["sessions"] = [dict({k: s.get(k) for k in ("id", "cwd", "title", "start", "end", "ended", "lastWords", "branch")},
                              live=time.time() - (s.get("end") or 0) < IDLE_MINUTES * 60) for s in p["sessions"][:5]]
        p["days"] = {day: counts for day, counts in sorted(p["days"].items())}
        state_md = Path(p["root"]) / "STATE.md"
        p["stateMd"] = str(state_md) if state_md.exists() else None
    order = {"needs": 0, "risk": 1, "quiet": 2}
    projects.sort(key=lambda p: (min([order.get(f["view"], 3) for f in p["flags"]] or [9]),
                                 -max([f["sev"] for f in p["flags"]] or [0]), -(p.get("lastTs") or 0)))
    return projects


def short_hash(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode()).hexdigest()[:4]


# ---------------------------------------------------------------- scan

def scan(days: int | None = None, rescan: bool = False) -> dict:
    started = time.time()
    cfg = settings()
    if days:
        cfg["days"] = days
    db = connect()
    now = time.time()
    since = now - cfg["days"] * 86400
    tops, map_roots = discover(db, cfg, rescan)
    links = PM.load_links()
    sessions = scan_sessions(db, since)
    people = identities(tops, cfg)

    top_set = set(tops)
    finder = Finder({r: r for r in tops + map_roots})
    in_repo = Finder({r: r for r in tops})
    warm = set()
    for session in sessions:
        if (session.get("end") or 0) >= now - WARM_DAYS * 86400:
            for path in list(session.get("edits", {})) + [session.get("cwd")]:
                root = finder(path)
                if root:
                    warm.add(root)
    for root, data in db.execute("SELECT root, data FROM repos"):
        if (json.loads(data).get("lastOwn") or 0) >= now - WARM_DAYS * 86400:
            warm.add(root)
    targets = [(root, None) for root in tops]
    for root in map_roots:
        if root not in top_set:
            above = in_repo(os.path.dirname(root))
            if above:
                targets.append((root, os.path.relpath(root, above)))
    states = scan_repos(db, targets, since, people, warm, rescan)

    maps = {root: read_map(root) for root in map_roots}
    marks = {row[0]: {"kind": row[1], "reason": row[2], "until": row[3], "at": row[4]}
             for row in db.execute("SELECT root, kind, reason, until, at FROM marks")}
    answers = {(row[0], row[1]): row[2] for row in db.execute("SELECT root, decision, answer FROM answers")}
    quiet = meta(db, "quiet") or {}
    quiet_days = int(quiet.get("days") or QUIET_DEFAULT)
    projects = build(cfg, tops, map_roots, states, maps, sessions, links, marks, answers, quiet_days, now, since)
    projects = finish(projects, cfg, links)
    model = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
             "window": cfg["days"], "quietDays": quiet_days, "quiet": quiet, "redacted": False,
             "unmappedDays": UNMAPPED_DAYS,
             "roots": [tilde(str(r)) for r in cfg["roots"]],
             "scan": {"repos": len(tops), "maps": len(map_roots), "sessions": len(sessions),
                      "checked": sum(1 for t, _ in targets if t in states), "seconds": 0.0},
             "projects": projects, "pm": pm_command()}
    model["changes"] = snapshot(db, projects)
    for p in projects:
        p["cmd"] = commands(p, model)
    model["scan"]["seconds"] = round(time.time() - started, 2)
    write_outputs(model)
    db.close()
    return model


def snapshot(db, projects: list[dict]) -> dict:
    """Store today's state per project and say what changed since the last day on record."""
    today = dt.date.today().isoformat()
    for p in projects:
        db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)", (today, p["root"], json.dumps(
            {"name": p["name"], "state": p["state"], "flags": sorted({f["code"] for f in p["flags"]}),
             "left": (p.get("map") or {}).get("left")})))
    db.commit()
    row = db.execute("SELECT MAX(day) FROM snapshots WHERE day < ?", (today,)).fetchone()
    if not row or not row[0]:
        return {"since": None, "items": []}
    before = {r[0]: json.loads(r[1]) for r in db.execute("SELECT root, data FROM snapshots WHERE day = ?", (row[0],))}
    words = {"uncommitted": "uncommitted work", "unpushed": "unpushed commits", "no-remote": "no remote",
             "stash": "stashed changes", "calls": "open calls", "stuck-on-you": "stuck on you",
             "session-question": "a session waiting on you", "never-committed": "never committed",
             "missing": "map folder gone", "quiet": "gone quiet"}
    items = []
    for p in projects:
        old = before.get(p["root"])
        now_flags = {f["code"] for f in p["flags"]}
        if not old:
            if now_flags & {"uncommitted", "never-committed", "missing", "session-question"}:
                items.append({"kind": "new", "text": f"{p['name']}: new, " + ", ".join(words.get(c, c) for c in sorted(now_flags))})
            continue
        if old["state"] != p["state"] and p["state"] in ("quiet", "moving"):
            items.append({"kind": "state", "text": f"{p['name']} {'went quiet' if p['state'] == 'quiet' else 'is moving again'}"})
        for code in sorted(now_flags - set(old["flags"]) - {"quiet"}):
            if code in words:
                items.append({"kind": "new", "text": f"{p['name']}: {words[code]}"})
        for code in sorted(set(old["flags"]) - now_flags - {"quiet"}):
            if code in words:
                items.append({"kind": "resolved", "text": f"{p['name']}: no longer {words[code]}"})
    return {"since": row[0], "items": items[:12]}


# ---------------------------------------------------------------- restart

def pm_command() -> str:
    """How to call pm.py from a terminal: through the installed skill link when there is one."""
    installed = HOME / ".claude" / "skills" / "map" / "scripts" / "pm.py"
    script = installed if installed.exists() else Path(PM.__file__).resolve()
    return f"python3 {shlex.quote(tilde(str(script)))}"


def claude_argv() -> list[str]:
    """`claude`, or config super.claude, plus CLAUDE_SESSIONS_CLAUDE_ARGS (the flags claude-sessions reopens with)."""
    base = settings().get("claude")
    if base:
        return shlex.split(base)
    return ["claude"] + shlex.split(os.environ.get("CLAUDE_SESSIONS_CLAUDE_ARGS", ""))


def commands(p: dict, model: dict) -> dict:
    pm, pid = model["pm"], p["id"]
    out = {"restart": f"{pm} super restart {pid} --here", "park": f"{pm} super park {pid} --until 14d",
           "done": f"{pm} super done {pid}", "unpark": f"{pm} super unpark {pid}"}
    last = p["sessions"][0] if p.get("sessions") else None
    if last and last.get("cwd") and last.get("id"):
        out["resume"] = f"cd {shlex.quote(last['cwd'])} && {shlex.join(claude_argv())} --resume {last['id']}"
    return out


def brief(p: dict, model: dict) -> str:
    """What a fresh session needs to pick the project back up, without you re-explaining it."""
    m, g = p.get("map") or {}, p.get("git") or {}
    when = dt.datetime.fromtimestamp(p["lastTs"]).strftime("%a %-d %b") if p.get("lastTs") else "a while ago"
    ago = {None: "", 0: " (today)", 1: " (yesterday)"}.get(p.get("quietDays"), f" ({p.get('quietDays')} days ago)")
    lines = [f"Restart: {p['name']} ({tilde(p['root'])}).",
             f"Last worked on {when}{ago}"
             + ". This brief comes from /supermap; check it against the repo before acting.", ""]
    if m and not m.get("problem"):
        behind = f", {m['behind']} commits behind HEAD" if m.get("behind") else ""
        lines += [f"## Where the map left it (drawn {(m.get('generatedAt') or '')[:10]}{behind})",
                  f"- Next milestone: {m.get('milestone')}, {m.get('left')} left" + (": " + "; ".join(m.get("items", [])[:6]) if m.get("items") else ""),
                  f"- Suggested next step: {m.get('nextStep')} ({m.get('why')})"]
        lines += [f"- Stuck: {part['name']}, waiting on {part['waitingOn']}" for part in m.get("parts", []) if part.get("status") == "stuck"]
        for call in m.get("calls", []):
            state = f"answered: {call['answered']}" if call.get("answered") else f"open, default {call.get('default')}"
            lines.append(f"- Call {call['id']}: {call['question']} ({state})")
        lines.append("")
    if g:
        lines.append("## Git")
        changed = (g.get("dirty") or 0) + (g.get("untracked") or 0)
        if changed:
            lines.append(f"- {changed} uncommitted files, e.g. " + ", ".join(g.get("sample") or []))
        if g.get("unpushed"):
            lines.append(f"- {g['unpushed']} commits not on any remote")
        if not g.get("remote") and not p.get("sub"):
            lines.append("- No remote: this work exists only on this Mac")
        if g.get("stashes"):
            lines.append(f"- {g['stashes']} stash(es), newest: {g.get('stashMessage')}")
        if g.get("recent"):
            lines.append("- Your recent commits: " + "; ".join(g["recent"][:5]))
        lines.append(f"- Branch: {g.get('branch')}")
        lines.append("")
    last = p["sessions"][0] if p.get("sessions") else None
    if last:
        ended = {"question": "ended on a question to the user", "mid-task": "stopped mid-task",
                 "unanswered": "ended with the user's message unanswered", "interrupted": "was interrupted",
                 "error": "ended on an error", "done": "ended normally"}.get(last.get("ended"), "ended")
        lines += ["## Last session", f"- {last.get('title') or 'untitled'} ({dt.datetime.fromtimestamp(last['end']).strftime('%a %-d %b %H:%M') if last.get('end') else '?'}), {ended}."]
        if last.get("lastWords"):
            lines.append(f"- Its last words: {' '.join(last['lastWords'].split())[:280]}")
        lines += [f"- Resume it instead: claude --resume {last['id']} (from {tilde(last.get('cwd') or p['root'])})", ""]
    if p.get("stateMd"):
        try:
            text = Path(p["stateMd"]).read_text(errors="replace").strip().splitlines()[:30]
            lines += ["## STATE.md (first lines)"] + text + [""]
        except OSError:
            pass
    lines += ["## Start by",
              "1. Check the facts above with git status and, if there is one, .project-map/plan.md.",
              "2. Tell me in three lines where this stands and what you'd do next; wait for my go before large changes.",
              "3. If the map is behind or missing, refresh it with /map in the background."]
    return "\n".join(lines)[:8000]


def open_tab(command: str) -> str:
    """Run a command in a new terminal tab: iTerm2 when it's running, else Terminal."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        raise PM.Fail("Opening a tab needs macOS. Run this instead:\n  " + command)
    quoted = command.replace("\\", "\\\\").replace('"', '\\"')
    probe = subprocess.run(["osascript", "-e", 'application "iTerm2" is running'], capture_output=True, text=True)
    if probe.stdout.strip() == "true":
        app = "iTerm2"
        script = ('tell application "iTerm2"\n  if (count of windows) = 0 then\n    create window with default profile\n'
                  '  else\n    tell current window to create tab with default profile\n  end if\n'
                  f'  tell current session of current window to write text "{quoted}"\n  activate\nend tell')
    else:
        app = "Terminal"
        script = f'tell application "Terminal"\n  do script "{quoted}"\n  activate\nend tell'
    result = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise PM.Fail(f"{app} wouldn't open a tab ({result.stderr.strip()[:160]}). Run this instead:\n  {command}")
    return app


def restart(pid: str, resume: bool = False, here: bool = False, show: bool = False, copy: bool = False) -> dict:
    model = latest()
    p = find_project(model, pid)
    if resume:
        last = p["sessions"][0] if p.get("sessions") else None
        if not last or not last.get("id"):
            raise PM.Fail(f"No session of yours on record for {p['name']} in the last {model['window']} days; restart it fresh instead")
        folder, argv = last.get("cwd") or p["root"], claude_argv() + ["--resume", last["id"]]
        command = f"cd {shlex.quote(folder)} && {shlex.join(argv)}"
    else:
        if p.get("missing") or not Path(p["root"]).is_dir():
            raise PM.Fail(f"{p['root']} no longer exists")
        text = brief(p, model)
        path = home_dir() / "briefs" / f"{p['id']}.md"
        private_text(path, text)
        folder, argv = p["root"], claude_argv() + [text]
        command = f"cd {shlex.quote(folder)} && {shlex.join(claude_argv())} \"$(cat {shlex.quote(str(path))})\""
    if copy and shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=command, text=True)
    if here:
        os.chdir(folder)
        os.execvp(argv[0], argv)
    result = {"project": p["name"], "mode": "resume" if resume else "fresh", "command": command}
    if not show and not copy:
        result["opened"] = open_tab(command)
    return result


# ---------------------------------------------------------------- calls, park, done

def latest(max_age: int = 600) -> dict:
    """The last scan if it's recent, else a new one."""
    path = home_dir() / "supermap.json"
    try:
        if time.time() - path.stat().st_mtime < max_age:
            return json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    return scan()


def find_project(model: dict, pid: str) -> dict:
    for p in model["projects"]:
        if pid in (p["id"], p["name"], p["root"], tilde(p["root"])):
            return p
    close = difflib.get_close_matches(pid, [p["id"] for p in model["projects"]], n=3)
    raise PM.Fail(f"No project {pid!r} on the supermap" + (f"; did you mean {', '.join(close)}?" if close else ""))


def open_calls(model: dict) -> list[dict]:
    rows = []
    for p in model["projects"]:
        if p["state"] in ("parked", "done"):
            continue
        for call in (p.get("map") or {}).get("calls", []):
            if not call.get("answered"):
                rows.append({"project": p["id"], "name": p["name"], "decision": call["id"], "question": call["question"],
                             "options": call["options"], "default": call["default"],
                             "since": (p["map"].get("generatedAt") or "")[:10]})
    return sorted(rows, key=lambda r: r["since"])


def answer(pid: str, decision: str, text: str) -> dict:
    """Write your answer into the project's plan.md, where its map agent reads answers on the next draw."""
    model = latest()
    p = find_project(model, pid)
    call = next((c for c in (p.get("map") or {}).get("calls", []) if c["id"] == decision), None)
    if not call:
        raise PM.Fail(f"{p['name']} has no open call {decision!r}; `pm.py super calls` lists them")
    plan = Path(p["root"]) / ".project-map" / "plan.md"
    lines = plan.read_text(errors="replace").splitlines() if plan.exists() else ["# Plan", ""]
    stamp = f"{' '.join(text.split())} (via /supermap, {dt.date.today().strftime('%-d %b')})"
    number = find_call_line(lines, call)
    if number is not None:
        line = lines[number]
        if answer_in(line):
            raise PM.Fail(f"plan.md already answers {decision}: {answer_in(line)}")
        lines[number] = (re.sub(r"Answer:.*$", lambda _: f"Answer: {stamp}", line) if "Answer:" in line
                         else f"{line.rstrip()} Answer: {stamp}")
    else:
        if "## Answers from /supermap" not in lines:
            lines += ["", "## Answers from /supermap", ""]
        lines.append(f"- {decision}: {call['question']} Answer: {stamp}")
    plan.write_text("\n".join(lines) + "\n")
    db = connect()
    db.execute("INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?)", (p["root"], decision, text, dt.datetime.now().isoformat()))
    db.commit()
    forget_latest()
    return {"project": p["name"], "decision": decision, "plan": str(plan), "line": number is not None}


def mark(pid: str, kind: str | None, until: str | None = None, reason: str = "") -> dict:
    model = latest()
    p = find_project(model, pid)
    db = connect()
    if kind is None:
        db.execute("DELETE FROM marks WHERE root = ?", (p["root"],))
    else:
        db.execute("INSERT OR REPLACE INTO marks VALUES (?, ?, ?, ?, ?)",
                   (p["root"], kind, reason, until_date(until), dt.date.today().isoformat()))
    db.commit()
    forget_latest()
    return {"project": p["name"], "mark": kind or "cleared", "until": until_date(until)}


def forget_latest() -> None:
    try:
        (home_dir() / "supermap.json").unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- nudge, learn

def nudge(model: dict) -> dict:
    """At most one item per project: its most pressing flag, for a daily review's "you may have forgotten"."""
    items = []
    for p in model["projects"]:
        if p["state"] in ("parked", "done"):
            continue
        flag = next((f for f in p["flags"] if f["sev"] >= 2 or f["view"] == "quiet"), None)
        if not flag:
            continue
        items.append({"id": f"supermap:{p['id']}:{flag['code']}", "root": p["root"], "project": p["name"],
                      "kind": flag["view"], "severity": flag["sev"], "text": f"{p['name']}: {flag['text']}",
                      "where": f"supermap; pick it up with: pm.py super restart {p['id']}",
                      "age_days": p.get("quietDays") or 0})
    items.sort(key=lambda i: (-i["severity"], -i["age_days"]))
    return {"generatedAt": model["generatedAt"], "items": items[:30]}


def gaps(histories: list[list[str]], end: dt.date) -> list[tuple[int, bool]]:
    """Quiet spells between active days: (length in days, ended by a new commit?), censored at `end`."""
    out = []
    for days in histories:
        dates = [dt.date.fromisoformat(d) for d in days if dt.date.fromisoformat(d) < end]
        for i, day in enumerate(dates):
            out.append(((dates[i + 1] - day).days, True) if i + 1 < len(dates) else ((end - day).days, False))
    return out


def survival(spells: list[tuple[int, bool]], horizon: int = 120) -> list[float]:
    """Kaplan-Meier: S[t] = chance a quiet spell lasts longer than t days, with open spells censored."""
    ended = collections.Counter(n for n, closed in spells if closed)
    censored = collections.Counter(n for n, closed in spells if not closed)
    at_risk, alive, curve = len(spells), 1.0, []
    for t in range(horizon + 1):
        if at_risk:
            alive *= 1 - ended.get(t, 0) / at_risk
        curve.append(alive)
        at_risk -= ended.get(t, 0) + censored.get(t, 0)
    return curve


def resumes(curve: list[float], k: int, within: int = 14) -> float:
    return 1 - curve[k + within] / curve[k] if curve[k] > 0 else 0.0


def threshold(curve: list[float], within: int = 14) -> int:
    """The first quiet day after which a spell more often stays quiet than resumes within two weeks."""
    return next((k for k in range(1, 60) if resumes(curve, k, within) < 0.5), QUIET_DEFAULT)


def observed(histories: list[list[str]], k: int, start: dt.date, stop: dt.date, within: int = 14) -> tuple[int, int]:
    """Of the spells that reached k quiet days between start and stop, how many resumed within `within` days."""
    resumed = total = 0
    for days in histories:
        dates = [dt.date.fromisoformat(d) for d in days]
        for i, day in enumerate(dates):
            mark_day = day + dt.timedelta(days=k)
            following = dates[i + 1] if i + 1 < len(dates) else None
            if not start <= mark_day <= stop or (following and following <= mark_day):
                continue
            total += 1
            resumed += bool(following and (following - mark_day).days <= within)
    return resumed, total


def recent_threshold(histories: list[list[str]], start: dt.date, stop: dt.date, minimum: int = 30) -> tuple:
    """The first quiet day at which, among spells reaching it between start and stop, fewer than half resumed
    within two weeks; needs `minimum` spells to say anything."""
    for k in range(1, 22):
        resumed, total = observed(histories, k, start, stop)
        if total >= minimum and resumed / total < 0.5:
            return k, resumed, total
    return None, 0, 0


def forward_check(db, by_root: dict, today: dt.date, within: int = 14) -> dict:
    """The judge that matters for a drifting number: of the projects the supermap called quiet on a day at least
    two weeks ago, how many really stayed quiet for the next two weeks."""
    stayed = total = 0
    for day, root, data in db.execute("SELECT day, root, data FROM snapshots WHERE day <= ?",
                                      ((today - dt.timedelta(days=within)).isoformat(),)):
        if json.loads(data).get("state") != "quiet" or root not in by_root:
            continue
        start, stop = dt.date.fromisoformat(day), dt.date.fromisoformat(day) + dt.timedelta(days=within)
        total += 1
        stayed += not any(start < dt.date.fromisoformat(d) <= stop for d in by_root[root])
    return {"calledQuiet": total, "stayedQuiet": stayed,
            "share": round(stayed / total, 3) if total else None,
            "note": None if total else f"needs snapshots from {within}+ days ago; the daily scan makes them"}


def learn() -> dict:
    """Set "gone quiet" from your own commits: the first quiet day after which, in the latest complete weeks,
    your projects more often stayed quiet than resumed. Checked by fitting the month before and testing on them."""
    cfg = settings()
    db = connect()
    tops, _ = discover(db, cfg, rescan=False)
    emails, names = identities(tops, cfg)

    def history(root: str) -> list[str]:
        out = git(root, "log", "--all", "--format=%ct%x1f%ae%x1f%an", timeout=60) or ""
        days = set()
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3 and (parts[1].lower() in emails or parts[2] in names):
                days.add(local_day(int(parts[0])))
        return sorted(days)

    repos = [r for r in tops if (git_dirs(r) or (None, None))[0] == (git_dirs(r) or (None, None))[1]]  # no worktrees
    with ThreadPoolExecutor(16) as pool:
        by_root = {root: h for root, h in zip(repos, pool.map(history, repos)) if h}
    histories = list(by_root.values())
    today = dt.date.today()
    spells = gaps(histories, today)
    if len(spells) < 200:
        raise PM.Fail(f"Only {len(spells)} quiet spells in your history; keeping {QUIET_DEFAULT} days")
    curve = survival(spells)
    # spells need two weeks of follow-up, so the latest window ends 14 days ago; behaviour drifts, so it's short
    recent = (today - dt.timedelta(days=46), today - dt.timedelta(days=14))
    earlier = (today - dt.timedelta(days=78), today - dt.timedelta(days=46))
    k_recent, resumed, total = recent_threshold(histories, *recent)
    k_earlier, _, _ = recent_threshold(histories, *earlier)
    check = observed(histories, k_earlier, *recent) if k_earlier else (0, 0)
    k = min(max(k_recent or threshold(curve), 3), 21)
    stats = {"days": k, "method": "latest weeks" if k_recent else "all history (too few recent spells)",
             "spells": len(spells), "repos": len(histories),
             "resumeAtK": round(resumed / total, 3) if k_recent else round(resumes(curve, k), 3),
             "recent": {"from": recent[0].isoformat(), "to": recent[1].isoformat(), "resumed": resumed, "spells": total},
             "history": {"days": threshold(curve), "table": {str(d): round(resumes(curve, d), 3) for d in (1, 2, 3, 5, 7, 10, 14, 30)}},
             "holdout": {"fitOn": [earlier[0].isoformat(), earlier[1].isoformat()], "fitDays": k_earlier,
                         "resumed": check[0], "spells": check[1],
                         "observed": round(check[0] / check[1], 3) if check[1] else None,
                         "holds": bool(check[1]) and check[0] / check[1] < 0.5},
             "forward": forward_check(db, by_root, today),
             "learnedAt": dt.datetime.now().isoformat(timespec="seconds")}
    set_meta(db, "quiet", stats)
    forget_latest()
    return stats


# ---------------------------------------------------------------- the page

def style() -> tuple[str, str]:
    """Theme and accent from the project-map agent's memory, so the supermap matches your maps."""
    text = ""
    for path in (STYLE_MEMORY / "MEMORY.md", STYLE_MEMORY / "style.md"):
        try:
            text += path.read_text(errors="replace")
        except OSError:
            pass
    accent = re.search(r"#[0-9a-fA-F]{6}\b", text)
    theme = "light" if re.search(r"\blight\b", text, re.I) and not re.search(r"\bdark\b", text, re.I) else "dark"
    return theme, accent.group(0) if accent else "#d97757"


def redact(model: dict) -> dict:
    """The copy that can live at a link: no paths, session titles, last words, branch names or commands."""
    safe = json.loads(json.dumps(model))
    safe["redacted"] = True
    safe["roots"] = []
    safe["pm"] = ""
    path_words = re.compile(r"(?:/Users|/home|/private|/tmp|/var|~)/\S*")
    for p in safe["projects"]:
        if p.get("temp"):
            p["name"] = "a temp folder"
        p.pop("cmd", None)
        p["root"] = ""
        p["stateMd"] = None
        if p.get("mark"):
            p["mark"] = dict(p["mark"], reason=path_words.sub("…", p["mark"].get("reason") or ""))
        p["worktrees"] = len(p.get("worktrees") or [])
        p["movedTo"] = None
        if p.get("git"):
            p["git"] = {k: p["git"].get(k) for k in ("dirty", "untracked", "remote", "unpushed", "stashes", "initial")}
        p["sessions"] = [{"end": s.get("end"), "ended": s.get("ended"), "live": s.get("live")} for s in p.get("sessions", [])]
        if p.get("map"):
            # a map's words stay on the map: some were kept local on purpose, so the link carries counts only
            m = p["map"]
            p["map"] = {"url": m.get("url"), "drawn": m.get("drawn"), "generatedAt": m.get("generatedAt"),
                        "left": m.get("left"), "done": m.get("done"), "total": m.get("total"), "behind": m.get("behind"),
                        "problem": "map.json needs fixing" if m.get("problem") else None,
                        "calls": [{"id": "", "question": "", "options": [], "default": "", "answered": c.get("answered") and "yes"}
                                  for c in m.get("calls", [])], "parts": [], "draws": m.get("draws", [])}
            p["name"] = p["name"] if m.get("url") else p["id"]  # a local-only map's title is its words too
        for flag in p["flags"]:
            flag["fix"] = ""
            if flag["code"] == "stuck-on-you":
                flag["text"] = "a part of the map is stuck waiting on you"
            if flag["code"] == "quiet":
                flag["text"] = re.sub(r"left before [^;]*", "left before the next milestone", flag["text"])
            if flag["code"] == "stash":
                flag["text"] = re.sub(r"old: .*$", "old", flag["text"])
            if flag["code"] == "missing":
                flag["text"] = "registered map's folder is gone"
            flag["text"] = path_words.sub("…", flag["text"])
    names = {p["name"]: p for p in model["projects"]}
    for item in safe.get("changes", {}).get("items", []):
        name = item["text"].split(":")[0].split(" went quiet")[0].split(" is moving again")[0]
        original = names.get(name)
        if original and original.get("map") and not original["map"].get("url"):
            item["text"] = item["text"].replace(name, original["id"], 1)
        item["text"] = path_words.sub("…", item["text"])
    return safe


def scrub(value, pattern):
    """Every string in a map's text with file paths cut out (its hosted url stays)."""
    if isinstance(value, dict):
        return {k: v if k == "url" else scrub(v, pattern) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, pattern) for v in value]
    return pattern.sub("…", value) if isinstance(value, str) else value


def page(model: dict) -> str:
    theme, accent = style()
    data = json.dumps(model, separators=(",", ":")).replace("<", "\\u003c")  # no </script> or <!-- inside
    return (PAGE.replace("__THEME__", theme).replace("__ACCENT__", accent).replace("__DATA__", data))


def write_outputs(model: dict) -> None:
    folder = home_dir()
    private_text(folder / "supermap.json", json.dumps(model, indent=1))
    private_text(folder / "supermap.html", page(model))
    private_text(folder / "nudge.json", json.dumps(nudge(model), indent=1))


def publish(rotate: bool = False, dry_run: bool = False) -> dict:
    config = PM.load_config()
    if not config.get("publish", {}).get("command"):
        raise PM.Fail("No hosting configured: the supermap stays local (publish.command in config.json)")
    model = latest()
    links = PM.load_links()
    entry = links.setdefault("super", {})
    old = entry.get("slug")
    if rotate or not entry.get("token"):
        entry["token"] = PM.new_token()
    entry["slug"] = f"{config['publish'].get('prefix', 'map')}-super-{entry['token']}"
    folder = PM.site_dir(entry["slug"], "Supermap", page(redact(model)), {})
    url = PM.deploy(config, folder, entry["slug"], dry_run)
    if rotate and old and old != entry["slug"] and not dry_run:
        if config["publish"].get("remove"):
            subprocess.run(PM.fill(config["publish"]["remove"], slug=old), capture_output=True)
        shutil.rmtree(PM.DATA_DIR / "sites" / old, ignore_errors=True)  # and the local copy of what it served
    if not dry_run:
        PM.private_write(PM.CONFIG_DIR / "links.json", json.dumps(links, indent=2))
    return {"url": url, "local": str(home_dir() / "supermap.html")}


# ---------------------------------------------------------------- summary and CLI

def summary(model: dict) -> str:
    ps = model["projects"]
    count = lambda view: sum(1 for p in ps if any(f["view"] == view for f in p["flags"]))
    risky = [p for p in ps if any(f["view"] == "risk" and f["sev"] >= 2 for f in p["flags"])]
    lines = [f"Supermap: {len(ps)} projects ({model['scan']['repos']} repos, {model['scan']['maps']} maps, "
             f"{model['scan']['sessions']} sessions of yours in {model['window']} days), scanned in {model['scan']['seconds']} s",
             f"  needs you {count('needs')} · at risk {len(risky)} · gone quiet {count('quiet')} (after {model['quietDays']} days) · unmapped {count('unmapped')}"]
    for view, label in (("needs", "needs you"), ("risk", "at risk"), ("quiet", "quiet")):
        rows = []
        for p in ps:
            flag = next((f for f in p["flags"] if f["view"] == view and (view != "risk" or f["sev"] >= 2)), None)
            if flag:
                rows.append((-flag["sev"], p.get("quietDays") or 0, p["name"], p["id"], flag["text"]))
        for _, _, name, pid, text in sorted(rows)[:5]:
            lines.append(f"  {label:<9} {name} ({pid}): {text}")
    for item in model.get("changes", {}).get("items", [])[:5]:
        lines.append(f"  since {model['changes']['since']}: {item['text']}")
    lines.append(f"  page: {home_dir() / 'supermap.html'}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pm.py super", description=__doc__.splitlines()[0])
    parser.add_argument("action", nargs="?", default="scan",
                        choices=["scan", "open", "restart", "brief", "calls", "answer", "park", "unpark", "done",
                                 "publish", "nudge", "learn", "preview"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--days", type=int)
    parser.add_argument("--rescan", action="store_true", help="walk the folders and recheck every repo now")
    parser.add_argument("--learn", action="store_true", help="refit 'gone quiet' first")
    parser.add_argument("--publish", action="store_true", help="also publish the redacted copy")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--here", action="store_true", help="run claude in this terminal")
    parser.add_argument("--print", dest="show", action="store_true", help="print the command, open nothing")
    parser.add_argument("--copy", action="store_true", help="copy the command to the clipboard")
    parser.add_argument("--until")
    parser.add_argument("--reason", default="")
    parser.add_argument("--rotate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    need = {"restart": 1, "brief": 1, "answer": 3, "park": 1, "unpark": 1, "done": 1}.get(args.action, 0)
    if len(args.args) < need:
        raise PM.Fail(f"`super {args.action}` needs {need} argument(s)")
    if args.action == "answer" and len(args.args) > 3:
        args.args = args.args[:2] + [" ".join(args.args[2:])]
    out = None
    if args.action == "scan":
        if args.learn:
            try:
                learn()
            except PM.Fail as problem:
                print(f"learn: {problem}", file=sys.stderr)
        model = scan(args.days, args.rescan)
        if args.publish:
            try:
                model["published"] = publish()["url"]
            except PM.Fail as problem:
                print(f"publish: {problem}", file=sys.stderr)
        if not args.quiet:
            print(json.dumps(model, indent=1) if args.json else summary(model))
        return 0
    if args.action == "open":
        page_path = home_dir() / "supermap.html"
        if not page_path.exists():
            scan()
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(page_path)])
        out = {"opened": str(page_path)}
    elif args.action == "restart":
        out = restart(args.args[0], args.resume, args.here, args.show, args.copy)
    elif args.action == "brief":
        model = latest()
        print(brief(find_project(model, args.args[0]), model))
        return 0
    elif args.action == "calls":
        rows = open_calls(latest())
        if args.json:
            out = rows
        else:
            for r in rows:
                print(f"{r['project']} {r['decision']}: {r['question']} (default: {r['default']}; on the map since {r['since']})")
            print(f"{len(rows)} open calls")
            return 0
    elif args.action == "answer":
        out = answer(*args.args[:3])
    elif args.action in ("park", "done"):
        out = mark(args.args[0], "parked" if args.action == "park" else "done", args.until, args.reason)
    elif args.action == "unpark":
        out = mark(args.args[0], None)
    elif args.action == "publish":
        out = publish(args.rotate, args.dry_run)
    elif args.action == "nudge":
        out = nudge(latest())
    elif args.action == "learn":
        out = learn()
    elif args.action == "preview":
        latest()
        out = PM.preview_page(home_dir() / "supermap.html")
    print(json.dumps(out, indent=1))
    return 0


PAGE = r"""<!doctype html>
<html lang="en" data-theme="__THEME__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">
<title>Supermap</title>
<style>
:root{--bg:#0d1117;--panel:#151b23;--panel2:#1d242d;--line:#2d343d;--text:#e6edf3;--muted:#8d96a0;--accent:__ACCENT__;
--red:#f47067;--amber:#d9a53a;--green:#57ab5a;--blue:#6cb6ff;--cell:#57ab5a}
:root[data-theme=light]{--bg:#fbfbfa;--panel:#ffffff;--panel2:#f1f3f5;--line:#d8dde3;--text:#1f2328;--muted:#5d6670;
--red:#cf222e;--amber:#9a6700;--green:#1a7f37;--blue:#0969da;--cell:#2da44e}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
a{color:var(--blue)}
.wrap{max-width:1180px;margin:0 auto;padding:22px 16px 56px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 14px}
h1{font-size:24px;margin:0;letter-spacing:-.01em}
h1 b{color:var(--accent)}
.sub{color:var(--muted);font-size:13px;margin:4px 0 16px;flex-basis:100%}
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:0 0 10px}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 16px}
.tabs button{font:inherit;font-size:13px;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:999px;padding:5px 12px;cursor:pointer}
.tabs button[aria-selected=true]{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.tabs .c{color:var(--muted);margin-left:6px;font-variant-numeric:tabular-nums}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;text-align:left;color:inherit;font:inherit;cursor:pointer}
.tile[aria-selected=true]{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.tile .n{display:block;font-size:28px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.1}
.tile .l{color:var(--muted);font-size:13px}
.tile.hot .n{color:var(--red)}.tile.warm .n{color:var(--amber)}
.changes{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:10px;padding:10px 14px;margin:0 0 16px;font-size:14px}
.changes ul{margin:4px 0 0;padding-left:18px}.changes li.resolved{color:var(--green)}
h2{font-size:17px;margin:22px 0 4px}
.lede{color:var(--muted);font-size:13px;margin:0 0 12px}
.grid{display:flex;gap:12px;align-items:flex-start}
.grid>.col{flex:1 1 0;min-width:0;display:flex;flex-direction:column;gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 15px;min-width:0}
.card h3{font-size:16px;margin:0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;overflow-wrap:anywhere}
.chip{font-size:12px;font-weight:500;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:0 8px;white-space:nowrap}
.meta{color:var(--muted);font-size:13px;margin:2px 0 8px}
.dot{width:9px;height:9px;border-radius:50%;flex:none;background:var(--muted)}
.s-moving{background:var(--green)}.s-quiet{background:var(--amber)}.s-parked{background:var(--blue)}.s-done,.s-idle{background:var(--muted)}
ul.flags{list-style:none;margin:6px 0;padding:0}
ul.flags li{padding:3px 0 3px 22px;position:relative;font-size:14px;overflow-wrap:anywhere}
ul.flags li:before{content:"";position:absolute;left:4px;top:10px;width:8px;height:8px;border-radius:2px;background:var(--muted)}
ul.flags li.v3:before{background:var(--red)}ul.flags li.v2:before{background:var(--amber)}
.v3 .sev{color:var(--red)}.v2 .sev{color:var(--amber)}
.fix{color:var(--muted)}
.sev{font-size:11px;font-weight:650;text-transform:uppercase;letter-spacing:.04em;margin-right:6px;color:var(--muted)}
.block{border-top:1px solid var(--line);margin-top:10px;padding-top:8px;font-size:14px;overflow-wrap:anywhere}
.block .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em;display:block}
.bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden;margin:6px 0 2px}
.bar i{display:block;height:100%;background:var(--accent)}
.calls{margin:6px 0 0;padding-left:18px}.calls li{margin:3px 0}.calls .d{color:var(--muted)}
.quote{color:var(--muted);font-style:italic}
.actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.actions button,.actions a{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:8px;padding:5px 10px;cursor:pointer;text-decoration:none}
.actions button.primary{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 22%,var(--panel2))}
details.row{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:6px 0}
details.row>summary{list-style:none;cursor:pointer;padding:9px 12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
details.row>summary::-webkit-details-marker{display:none}
details.row[open]>summary{border-bottom:1px solid var(--line)}
details.row .card{border:0;border-radius:0 0 10px 10px;background:transparent}
.rname{font-weight:600;overflow-wrap:anywhere}.rmeta{color:var(--muted);font-size:13px;margin-left:auto}
.icons{display:flex;gap:6px;flex-wrap:wrap}
.daygrid{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
.daygrid table{border-collapse:separate;border-spacing:3px;font-size:12px;padding:6px;width:100%}
.daygrid th{font-weight:500;color:var(--muted);text-align:center;padding:0 0 4px;min-width:22px}
.daygrid th.p{text-align:left;position:sticky;left:0;background:var(--panel);padding-right:10px;max-width:190px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px;color:var(--text)}
.daygrid th.today{color:var(--accent)}
.daygrid th.area{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;padding-top:10px;max-width:none;position:static}
.daygrid td{min-width:18px;height:20px;border-radius:4px;background:var(--panel2);text-align:center;color:#fff;font-size:10px;cursor:pointer}
.daygrid td.l1{background:color-mix(in srgb,var(--cell) 30%,var(--panel2))}.daygrid td.l2{background:color-mix(in srgb,var(--cell) 55%,var(--panel2))}
.daygrid td.l3{background:color-mix(in srgb,var(--cell) 80%,var(--panel2))}.daygrid td.l4{background:var(--cell)}
.daygrid td.sel{outline:2px solid var(--accent)}
.daylist h3{font-size:15px;margin:16px 0 6px}
.daylist .chips{display:flex;flex-wrap:wrap;gap:6px}
.daylist .chips span{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:3px 9px;font-size:13px}
.daylist .chips em{color:var(--muted);font-style:normal}
.empty{color:var(--muted);background:var(--panel);border:1px dashed var(--line);border-radius:12px;padding:18px;text-align:center}
.foot{color:var(--muted);font-size:12px;margin-top:28px;border-top:1px solid var(--line);padding-top:12px}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--text);color:var(--bg);padding:8px 14px;border-radius:8px;font-size:13px;display:none;max-width:calc(100vw - 32px)}
.more{margin-top:12px}.more summary{cursor:pointer;color:var(--muted);font-size:14px}
@media (max-width:640px){.tile{padding:10px}.tile .n{font-size:22px}.tile .l{font-size:12px}.wide-only{display:none}h1{font-size:21px}}
</style></head><body><div class="wrap">
<header><h1><b>Supermap</b></h1><p class="sub" id="sub"></p></header>
<div class="tiles" role="tablist" id="tiles"></div>
<div class="tabs" role="tablist" id="tabs"></div>
<div id="changes"></div>
<main id="view"></main>
<p class="foot" id="foot"></p>
</div><div class="toast" id="toast" role="status"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const P=D.projects, R=D.redacted;
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const now=Date.now();
function ago(ts){if(!ts)return'never';const h=(now-ts*1000)/36e5;if(h<1)return'just now';if(h<48)return Math.round(h)+' h ago';const d=Math.round(h/24);return d<60?d+' days ago':Math.round(d/30)+' months ago'}
function dayName(d){return new Date(d+'T12:00:00').toLocaleDateString(undefined,{weekday:'short',day:'numeric',month:'short'})}
const has=(p,v,min)=>p.flags.some(f=>f.view===v&&(!min||f.sev>=min));
const qsev=p=>Math.max(0,...p.flags.filter(f=>f.view==='quiet').map(f=>f.sev));
const SEV={3:'act now',2:'soon',1:'note'};
const STATE={moving:'moving',quiet:'gone quiet',parked:'parked',done:'done',idle:'idle'};
const VIEWS=[
 {id:'needs',label:'Needs you',list:()=>P.filter(p=>has(p,'needs')),hot:1,lede:'Open calls on your maps, parts stuck waiting on you, and sessions that ended on a question to you.'},
 {id:'risk',label:'At risk',list:()=>P.filter(p=>has(p,'risk',2)),hot:1,lede:'Work that could be lost for good: never committed, uncommitted and untouched for a day or more, on no remote, stashed or in a temp folder. Most urgent first, then most work at stake. Each line says what to do; Restart fresh copies a command that opens Claude in the project with a brief of where it stood.'},
 {id:'quiet',label:'Gone quiet',list:()=>P.filter(p=>p.state==='quiet'),warm:1,lede:''},
 {id:'projects',label:'By project',list:()=>P.filter(p=>p.activeDays||p.map||p.mark)},
 {id:'days',label:'By day',list:()=>P.filter(p=>p.activeDays)},
 {id:'unmapped',label:'Unmapped',list:()=>P.filter(p=>has(p,'unmapped')),lede:`Projects you worked on for ${D.unmappedDays} or more days this window that have no map yet, busiest first. Open a session there and type /map.`},
 {id:'parked',label:'Parked',list:()=>P.filter(p=>p.state==='parked'||p.state==='done'),lede:'Projects you parked or marked done. Parked ones still show in At risk, because a parked laptop-only repo can still be lost.'},
];
VIEWS[2].lede=`Projects you worked on this window, then left for ${D.quietDays}+ days with something still open. `+(D.quiet&&D.quiet.recent?`Lately, only ${Math.round(D.quiet.resumeAtK*100)}% of your projects quiet that long came back within two weeks without a nudge.`:'');
function flagList(p,view,min){const mine=p.flags.filter(f=>(!view||f.view===view)&&(!min||f.sev>=min));
 const fs=mine.concat(p.flags.filter(f=>!mine.includes(f)));  // every flag, this view's first, so a project reads the same everywhere
 return fs.length?'<ul class="flags">'+fs.map(f=>`<li class="v${f.sev}"><span class="sev">${SEV[f.sev]}</span>${esc(f.text)}${f.fix&&!R?`<span class="fix"> → ${esc(f.fix)}</span>`:''}</li>`).join('')+'</ul>':''}
function mapBlock(p){const m=p.map;if(!m)return'';if(m.problem)return`<div class="block"><span class="k">Map</span>${esc(m.problem)}</div>`;
 if(R){const open=(m.calls||[]).filter(c=>!c.answered).length;const pct=m.total?Math.round(100*m.done/m.total):0;
  return `<div class="block"><span class="k">Map · drawn ${ago(m.drawn)} ${m.url?`<a href="${esc(m.url)}">open map</a>`:'(kept local)'}</span><div>${m.left} left before the next milestone${open?` · ${open} open call${open>1?'s':''}`:''}</div><div class="bar"><i style="width:${pct}%"></i></div></div>`}
 const pct=m.total?Math.round(100*m.done/m.total):0;const link=R?(m.url?`<a href="${esc(m.url)}">open map</a>`:''):(m.index?`<a href="file://${encodeURI(m.index)}">open map</a>`:(m.url?`<a href="${esc(m.url)}">open map</a>`:''));
 const calls=(m.calls||[]).map(c=>`<li>${esc(c.question)} <span class="d">${c.answered?'answered: '+esc(c.answered)+' (map not redrawn yet)':'default: '+esc(c.default)}</span></li>`).join('');
 return `<div class="block"><span class="k">Map${m.behind?` · ${m.behind} commits behind`:''} · drawn ${ago(m.drawn)} ${link}</span>
 <div><b>${esc(m.milestone||'')}</b>: ${m.left} left</div><div class="bar" title="${m.done} of ${m.total} items across the parts"><i style="width:${pct}%"></i></div>
 <div>Next: ${esc(m.nextStep||'')}</div>${calls?`<ul class="calls">${calls}</ul>`:''}</div>`}
function sessionBlock(p){const s=(p.sessions||[])[0];if(!s||!s.end)return'';
 if(s.live)return `<div class="block"><span class="k">Session running now</span>${R?'':`<b>${esc(s.title||'untitled')}</b>`}</div>`;
 const how={question:'ended on a question to you',['mid-task']:'stopped mid-task',unanswered:'your last message went unanswered',interrupted:'you interrupted it',error:'ended on an error',done:'ended normally'}[s.ended]||'';
 return `<div class="block"><span class="k">Last session · ${ago(s.end)}</span>${R?'':`<b>${esc(s.title||'untitled')}</b> · `}${how}${!R&&s.lastWords&&s.ended!=='done'?`<div class="quote">“${esc(s.lastWords.slice(-200))}”</div>`:''}</div>`}
function gitBlock(p){const g=p.git;if(!g||R)return'';const bits=[];if(g.branch)bits.push('on '+esc(g.branch));if(g.recent&&g.recent.length)bits.push('last: '+esc(g.recent[0]));
 return bits.length?`<div class="block"><span class="k">Git</span>${bits.join(' · ')}</div>`:''}
function actions(p){if(R||!p.cmd)return'';const c=p.cmd;const b=[];
 if(!p.missing)b.push(`<button class="primary" data-copy="${esc(c.restart)}" title="Copies a command: paste it in a terminal to start a fresh Claude session primed with this project's restart brief">Restart fresh</button>`);
 if(c.resume)b.push(`<button data-copy="${esc(c.resume)}" title="Copies the command that resumes your last session here">Resume last session</button>`);
 if(p.state==='parked'||p.state==='done')b.push(`<button data-copy="${esc(c.unpark)}">Unpark</button>`);else{b.push(`<button data-copy="${esc(c.park)}" title="Park for two weeks: it stops counting as quiet">Park</button>`);b.push(`<button data-copy="${esc(c.done)}">Done</button>`)}
 return `<div class="actions">${b.join('')}</div>`}
function head(p){return `<h3><span class="dot s-${p.state}" title="${STATE[p.state]}"></span>${esc(p.name)} <span class="chip">${esc(p.area)}</span></h3>
 <div class="meta">${STATE[p.state]} · last worked ${ago(p.lastTs)}${p.activeDays?` · ${p.activeDays} active day${p.activeDays>1?'s':''} in ${D.window}`:''}${p.mark&&p.mark.until?` · parked until ${esc(p.mark.until)}`:''}${p.mark&&p.mark.reason?` · ${esc(p.mark.reason)}`:''}</div>`}
function card(p,view,min){return `<article class="card">${head(p)}${flagList(p,view,min)}${mapBlock(p)}${sessionBlock(p)}${gitBlock(p)}${actions(p)}</article>`}
function cards(list,view,min,cap){if(!list.length)return`<p class="empty">Nothing here.</p>`;const n=cap||list.length;
 const grid=l=>`<div class="grid">${l.map(p=>card(p,view,min)).join('')}</div>`;
 return grid(list.slice(0,n))+(list.length>n?`<details class="more"><summary>${list.length-n} more</summary>${grid(list.slice(n))}</details>`:'')}
function row(p){const icons=p.flags.slice(0,3).map(f=>`<span class="chip" style="color:var(${f.sev>2?'--red':f.sev>1?'--amber':'--muted'})">${esc(f.code)}</span>`).join('');
 return `<details class="row"><summary><span class="dot s-${p.state}"></span><span class="rname">${esc(p.name)}</span><span class="icons">${icons}</span><span class="rmeta">${ago(p.lastTs)}</span></summary>${card(p)}</details>`}
const RENDER={
 needs:v=>cards(v.list(),'needs'),
 risk:v=>{const all=P.filter(p=>has(p,'risk'));const top=v.list();const rest=all.filter(p=>!top.includes(p));
  const rsev=p=>Math.max(0,...p.flags.filter(f=>f.view==='risk').map(f=>f.sev));
  const size=p=>{const g=p.git||{};return (g.dirty||0)+(g.untracked||0)+(g.unpushed||0)+3*(g.stashes||0)};
  return cards(top.sort((a,b)=>rsev(b)-rsev(a)||size(b)-size(a)||(b.lastTs||0)-(a.lastTs||0)),'risk',2,24)+(rest.length?`<details class="more"><summary>${rest.length} lower-risk notes (older work, no remote)</summary>${cards(rest,'risk')}</details>`:'')},
 quiet:v=>cards(v.list().sort((a,b)=>qsev(b)-qsev(a)||a.quietDays-b.quietDays),'quiet',0,12),
 projects:v=>{const by={};v.list().forEach(p=>(by[p.area]=by[p.area]||[]).push(p));
  return Object.keys(by).sort((a,b)=>by[b].length-by[a].length).map(a=>`<h2>${esc(a)} <span class="chip">${by[a].length}</span></h2>${by[a].map(row).join('')}`).join('')||'<p class="empty">Nothing here.</p>'},
 days:v=>dayView(v.list()),
 unmapped:v=>cards(v.list().sort((a,b)=>b.activeDays-a.activeDays),'unmapped',0,12),
 parked:v=>cards(v.list()),
};
function dayView(list){const w=Math.min(window.innerWidth,1180)-32;const n=window.innerWidth<640?10:Math.max(14,Math.min(D.window,Math.floor((w-230)/21)));const days=[];for(let i=n-1;i>=0;i--){const d=new Date(now-i*864e5);days.push(d.toLocaleDateString('en-CA'))}
 const score=c=>c?(c.c||0)+(c.e?Math.ceil(c.e/5):0)+(c.s||0)+(c.m||0):0;
 const rows=list.filter(p=>days.some(d=>p.days[d])).sort((a,b)=>a.area.localeCompare(b.area)||b.lastTs-a.lastTs);
 const lvl=s=>s>=12?4:s>=6?3:s>=3?2:s>=1?1:0;
 const grid=`<div class="daygrid wide-only"><table><tr><th class="p">project</th>${days.map((d,i)=>`<th class="${i===n-1?'today':''}" title="${dayName(d)}">${d.slice(8)}</th>`).join('')}</tr>${rows.map((p,i)=>(i===0||rows[i-1].area!==p.area?`<tr><th class="p area" colspan="${n+1}">${esc(p.area)}</th></tr>`:'')+`<tr><th class="p" title="${esc(p.area)}">${esc(p.name)}</th>${days.map(d=>{const c=p.days[d];const s=score(c);
   return `<td class="l${lvl(s)}" data-day="${d}" title="${esc(p.name)}, ${dayName(d)}${c?`: ${c.c||0} commits, ${c.s||0} sessions${c.e?`, ${c.e} edits`:''}${c.m?', map drawn':''}`:': nothing'}">${c&&c.m?'◆':''}</td>`}).join('')}</tr>`).join('')}</table></div>`;
 const list2=days.slice().reverse().map(d=>{const ps=list.filter(p=>p.days[d]).sort((a,b)=>score(b.days[d])-score(a.days[d]));if(!ps.length)return'';
  return `<h3 id="d-${d}">${dayName(d)} <span class="chip">${ps.length} project${ps.length>1?'s':''}</span></h3><div class="chips">${ps.map(p=>{const c=p.days[d];const bits=[];if(c.c)bits.push(c.c+' commit'+(c.c>1?'s':''));if(c.s)bits.push(c.s+' session'+(c.s>1?'s':''));if(c.m)bits.push('map');return `<span>${esc(p.name)} <em>${bits.join(', ')}</em></span>`}).join('')}</div>`}).join('');
 return `<p class="lede">What you touched each day: commits, your Claude sessions and map draws. A row that goes dark is a project you stopped. ◆ = map drawn.</p>${rows.length?grid:''}<div class="daylist">${list2||'<p class="empty">No activity in this window.</p>'}</div>`}
function masonry(){document.querySelectorAll('.grid').forEach(g=>{const cards=[...g.querySelectorAll(':scope>.card, :scope>.col>.card')];
 const n=Math.max(1,Math.floor((g.clientWidth+12)/352));g.innerHTML='';const cols=[...Array(n)].map(()=>{const c=document.createElement('div');c.className='col';g.appendChild(c);return c});
 cards.forEach(card=>{cols.reduce((a,b)=>a.offsetHeight<=b.offsetHeight?a:b).appendChild(card)})})}
let _w=window.innerWidth;addEventListener('resize',()=>{if(window.innerWidth!==_w){_w=window.innerWidth;clearTimeout(window._m);window._m=setTimeout(masonry,120)}});
document.addEventListener('toggle',e=>{if(e.target.matches&&e.target.matches('details.more'))masonry()},true);
function show(id){const v=VIEWS.find(x=>x.id===id)||VIEWS[0];document.querySelectorAll('[data-v]').forEach(t=>t.setAttribute('aria-selected',t.dataset.v===v.id));
 $('#view').innerHTML=`<h2>${v.label}</h2>${v.lede?`<p class="lede">${esc(v.lede)}</p>`:''}`+RENDER[v.id](v);masonry();
 document.querySelectorAll('.daygrid td[data-day]').forEach(td=>td.onclick=()=>{const el=document.getElementById('d-'+td.dataset.day);if(el)el.scrollIntoView({behavior:'smooth'})});
 try{history.replaceState(null,'','#'+v.id)}catch(e){}}
const ALARMS=['needs','risk','quiet'];
$('#tiles').innerHTML=VIEWS.filter(v=>ALARMS.includes(v.id)).map(v=>{const n=v.list().length;const cls=n&&v.hot?'hot':n&&v.warm?'warm':'';return `<button class="tile ${cls}" role="tab" data-v="${v.id}"><span class="n">${n}</span><span class="l">${v.label}</span></button>`}).join('');
$('#tabs').innerHTML=VIEWS.filter(v=>!ALARMS.includes(v.id)).map(v=>`<button role="tab" data-v="${v.id}">${v.label}${v.id==='days'?'':`<span class="c">${v.list().length}</span>`}</button>`).join('');
document.querySelectorAll('[data-v]').forEach(t=>t.onclick=()=>show(t.dataset.v));
const ch=D.changes||{};if(ch.items&&ch.items.length)$('#changes').innerHTML=`<div class="changes"><b>Since ${dayName(ch.since)}</b><ul>${ch.items.map(i=>`<li class="${i.kind}">${esc(i.text)}</li>`).join('')}</ul></div>`;
$('#sub').textContent=`${new Date(D.generatedAt).toLocaleString(undefined,{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})} · ${P.length} projects · ${D.scan.maps} maps · ${D.scan.sessions} of your sessions in ${D.window} days · scanned ${ago(Date.parse(D.generatedAt)/1000)}`+(R?' · redacted copy: no paths, session titles or commands':'');
$('#foot').textContent=`Scanned ${D.scan.repos} repos${D.roots.length?' under '+D.roots.join(', '):''}, ${D.scan.sessions} interactive Claude sessions and ${D.scan.maps} maps in ${D.scan.seconds} s. Gone quiet = ${D.quietDays} days`+(D.quiet&&D.quiet.recent?`: in your latest complete weeks, ${Math.round(D.quiet.resumeAtK*100)}% of your projects that went that long without a commit resumed within two weeks (${D.quiet.recent.spells} spells; ${D.quiet.spells.toLocaleString()} in all history). It drifts month to month, so the daily scan refits it`+(D.quiet.forward&&D.quiet.forward.calledQuiet?`; of ${D.quiet.forward.calledQuiet} projects called quiet two weeks ago, ${Math.round(D.quiet.forward.share*100)}% stayed quiet`:'')+'':' (default; run pm.py super learn)')+'.'+(R?'':' Buttons copy a command; paste it into a terminal.');
document.addEventListener('click',e=>{const b=e.target.closest('[data-copy]');if(!b)return;const text=b.dataset.copy;
 const done=()=>{const t=$('#toast');t.textContent='Copied: '+text.slice(0,90)+(text.length>90?'…':'');t.style.display='block';clearTimeout(window._t);window._t=setTimeout(()=>t.style.display='none',2600)};
 if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(text).then(done,()=>fallback(text,done));else fallback(text,done)});
function fallback(text,done){const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();try{document.execCommand('copy')}catch(e){}ta.remove();done()}
const first=(location.hash||'').slice(1);show(VIEWS.some(v=>v.id===first)?first:(VIEWS.find(v=>v.list().length&&['needs','risk','quiet'].includes(v.id))||VIEWS[3]).id);
</script></body></html>
"""
