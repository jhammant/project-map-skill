#!/usr/bin/env python3
"""project-map helper for the /map skill and the project-map subagent.

  pm.py root [PATH]              the project root and its map id, as JSON
  pm.py guard                    PreToolUse hook: Write/Edit only inside .project-map/ (stdin: hook JSON)
  pm.py check ROOT               validate ROOT/.project-map/map.json against the map contract
  pm.py publish ROOT [--rotate] [--dry-run] [--local]
                                 add the map to the local registry and, if configured (and not
                                 --local), publish it at an unguessable URL
  pm.py preview ROOT             render the map in headless Chrome at phone (390 px) and laptop width:
                                 screenshots to look at, and any sideways overflow measured
  pm.py link [ROOT]              the project's hosted map URL and the all-projects index URL
  pm.py where [--all]            answer "where are we?" from the map (or every map), Orbital first
  pm.py github OWNER/REPO        open issues and recent CI runs, through Orbital when it can, else gh
  pm.py orbital QUERY [--given NAME=TYPE:VALUE ...]
                                 run a TaxiQL query (a file or a name in taxi/queries) against Orbital
  pm.py serve [--port 9733]      serve the registry's maps.json so a local Orbital can read it
  pm.py super ...                the supermap: every project's map, lost work and restarts (supermap.py)

Standard library only. Config: ~/.config/project-map/config.json. Registry: ~/.local/share/project-map/.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from functools import partial
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("PROJECT_MAP_CONFIG_DIR", HOME / ".config" / "project-map"))
DATA_DIR = Path(os.environ.get("PROJECT_MAP_DATA_DIR", HOME / ".local" / "share" / "project-map"))
MEMORY_DIR = HOME / ".claude" / "agent-memory" / "project-map"
REPO_DIR = Path(__file__).resolve().parents[2]  # follows the install symlink back to the repo
QUERIES_DIR = REPO_DIR / "taxi" / "queries"

STATUSES = {"done", "in-progress", "not-started", "stuck"}
DECISION_STATUSES = {"open", "decided"}
PRIVATE_META = '<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">'
TOKEN_LENGTH = 20  # base32 characters: 100 bits, far beyond guessing


def new_token() -> str:
    return base64.b32encode(secrets.token_bytes(15)).decode().lower()[:TOKEN_LENGTH]


class Fail(Exception):
    """A problem to report on one line, with a non-zero exit."""


# ---------------------------------------------------------------- config and paths

def load_config() -> dict:
    path = CONFIG_DIR / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise Fail(f"{path} is not valid JSON: {exc}") from None


def run(argv: list[str], cwd: Path | None = None) -> str | None:
    try:
        out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def project_root(path: str = ".") -> tuple[Path, str]:
    """The git top level (or the folder itself) and a short, URL-safe name for it."""
    start = Path(path).expanduser().resolve()
    top = run(["git", "-C", str(start), "rev-parse", "--show-toplevel"])
    root = Path(top) if top else start
    name = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-")[:20].strip("-") or "project"
    return root, name


def map_id(root: Path, name: str, links: dict) -> str:
    """The name, unless another folder already uses it; then the name plus a short path hash."""
    for key, entry in links.get("projects", {}).items():
        if entry.get("path") == str(root):
            return key
    taken = links.get("projects", {}).get(name)
    if taken and taken.get("path") != str(root):
        return f"{name[:15]}-{hashlib.sha1(str(root).encode()).hexdigest()[:4]}"
    return name


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def load_links() -> dict:
    path = CONFIG_DIR / "links.json"
    return json.loads(path.read_text()) if path.exists() else {"projects": {}}


# ---------------------------------------------------------------- guard

def guard(stdin=sys.stdin) -> int:
    """PreToolUse hook. Exit 2 blocks the Write or Edit and tells the agent why."""
    payload = json.load(stdin)
    target = payload.get("tool_input", {}).get("file_path", "")
    target = os.path.normpath(os.path.expanduser(target))  # folds any ../ before checking
    memory = str(MEMORY_DIR)
    if (".project-map" in target.split(os.sep)
            or os.path.basename(target) == ".gitignore"
            or target == memory or target.startswith(memory + os.sep)):
        return 0
    print("project-map only writes inside .project-map/, the project's .gitignore or its own "
          f"memory. Blocked: {target}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------- the map contract

def check_map(data: object) -> list[str]:
    """Problems with a map.json, empty when it meets the contract project-map.taxi reads."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["map.json must be a JSON object"]

    def need(obj: dict, key: str, kind: type | tuple, where: str) -> object:
        value = obj.get(key)
        if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
            problems.append(f"{where}.{key} must be {getattr(kind, '__name__', kind)}")
            return None
        return value

    need(data, "project", str, "map")
    when = need(data, "generatedAt", str, "map")
    if when:
        try:
            datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            problems.append("map.generatedAt must be an ISO 8601 time, e.g. 2026-10-07T07:30:00Z")
    need(data, "commit", str, "map")
    milestone = need(data, "nextMilestone", dict, "map")
    if milestone:
        need(milestone, "name", str, "nextMilestone")
        need(milestone, "left", int, "nextMilestone")
    step = need(data, "nextStep", dict, "map")
    if step:
        need(step, "text", str, "nextStep")
        need(step, "why", str, "nextStep")
    parts = need(data, "parts", list, "map") or []
    if isinstance(data.get("parts"), list) and not parts:
        problems.append("map.parts must name at least one part")
    for i, part in enumerate(parts):
        where = f"parts[{i}]"
        if not isinstance(part, dict):
            problems.append(f"{where} must be an object")
            continue
        for key in ("id", "name", "summary"):
            need(part, key, str, where)
        status = part.get("status")
        if status not in STATUSES:
            problems.append(f"{where}.status must be one of {', '.join(sorted(STATUSES))}")
        done, total = need(part, "done", int, where), need(part, "total", int, where)
        if isinstance(done, int) and isinstance(total, int) and not 0 <= done <= total:
            problems.append(f"{where}: done must be between 0 and total")
        if status == "stuck" and not part.get("waitingOn"):
            problems.append(f"{where} is stuck, so waitingOn must say what it waits for")
    for i, decision in enumerate(need(data, "decisions", list, "map") or []):
        where = f"decisions[{i}]"
        if not isinstance(decision, dict):
            problems.append(f"{where} must be an object")
            continue
        need(decision, "question", str, where)
        need(decision, "defaultChoice", str, where)
        if decision.get("status") not in DECISION_STATUSES:
            problems.append(f"{where}.status must be open or decided")
    return problems


def read_map(root: Path) -> dict:
    path = root / ".project-map" / "map.json"
    if not path.exists():
        raise Fail(f"No map yet: {path} does not exist")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise Fail(f"{path} is not valid JSON: {exc}") from None
    problems = check_map(data)
    if problems:
        raise Fail(f"{path} does not meet the map contract:\n  " + "\n  ".join(problems))
    return data


# ---------------------------------------------------------------- publishing

def with_private_meta(page: str) -> str:
    """Tell search engines not to index the page and browsers not to pass its URL on."""
    if 'name="referrer"' in page:
        return page
    match = re.search(r"<head[^>]*>", page, re.I)
    return page[:match.end()] + PRIVATE_META + page[match.end():] if match else PRIVATE_META + page


def site_dir(slug: str, title: str, page: str, extra: dict[str, str]) -> Path:
    """A folder ready for any static host: public/ holds the files; hl.json is title metadata some hosts read."""
    folder = DATA_DIR / "sites" / slug
    if folder.exists():
        shutil.rmtree(folder)
    public = folder / "public"
    public.mkdir(parents=True)
    (public / "index.html").write_text(with_private_meta(page))
    for name, text in extra.items():
        (public / name).write_text(text)
    (folder / "hl.json").write_text(json.dumps(
        {"title": title, "description": "Project map", "static": "public"}, indent=2))
    return folder


def fill(template: str, **values: str) -> list[str]:
    return [part.format(**values) for part in shlex.split(template)]


def deploy(config: dict, folder: Path, slug: str, dry_run: bool) -> str:
    publish = config["publish"]
    argv = fill(publish["command"], dir=str(folder), public=str(folder / "public"), slug=slug)
    url = publish.get("url", "{slug}").format(slug=slug)
    if dry_run:
        print("would run: " + shlex.join(argv), file=sys.stderr)
        return url
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no output"]
        raise Fail(f"publish command failed ({result.returncode}): {tail[0]}")
    return url


def index_page(maps: list[dict]) -> str:
    rows = []
    for item in maps:
        stuck = [p["name"] for p in item.get("parts", []) if p.get("status") == "stuck"]
        open_calls = [d for d in item.get("decisions", []) if d.get("status") == "open"]
        link = item.get("url") or ""
        name = html.escape(item.get("project", "?"))
        rows.append(
            "<tr>"
            f"<td>{f'<a href={html.escape(link, quote=True)!r}>{name}</a>' if link else name}</td>"
            f"<td>{html.escape(item['nextMilestone']['name'])}: {item['nextMilestone']['left']} left</td>"
            f"<td>{html.escape(', '.join(stuck)) or '-'}</td><td>{len(open_calls)}</td>"
            f"<td>{html.escape(item['nextStep']['text'])}</td>"
            f"<td>{html.escape(item.get('generatedAt', ''))}</td></tr>")
    return ("<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><title>Project maps</title>"
            "<style>body{font:15px/1.45 system-ui,sans-serif;margin:24px;background:#0d1117;color:#e6edf3}"
            "a{color:#79c0ff}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:8px;"
            "border-bottom:1px solid #30363d;vertical-align:top}th{color:#8b949e;font-weight:600}</style>"
            "</head><body><h1>Project maps</h1><table><tr><th>Project</th><th>Next milestone</th>"
            "<th>Stuck</th><th>Your calls</th><th>Next step</th><th>Drawn</th></tr>"
            + "".join(rows) + "</table><p>Machine-readable: <a href='maps.json'>maps.json</a> "
            "(the shape project-map.taxi describes).</p></body></html>")


def write_registry(links: dict) -> list[dict]:
    """Every project's latest map in one maps.json, the file project-map.taxi reads.

    Each map also gets its folder's name, which other tools (Claude Code, Retrace) know the project by.
    """
    folders = {pid: Path(e["path"]).name for pid, e in links.get("projects", {}).items() if e.get("path")}
    everything = []
    for path in (DATA_DIR / "maps").glob("*.json"):
        m = json.loads(path.read_text())
        if folders.get(path.stem):
            m["folder"] = folders[path.stem]
        everything.append(m)
    everything.sort(key=lambda m: m.get("project", ""))
    (DATA_DIR / "maps.json").write_text(json.dumps(everything, indent=1))
    (DATA_DIR / "index.html").write_text(index_page(everything))
    return everything


def publish(root_arg: str, rotate: bool = False, dry_run: bool = False, local: bool = False) -> dict:
    config = load_config()
    root, name = project_root(root_arg)
    data = read_map(root)
    links = load_links()
    pid = map_id(root, name, links)
    entry = links.setdefault("projects", {}).setdefault(pid, {"path": str(root)})
    hosted = bool(config.get("publish", {}).get("command")) and not local
    result = {"id": pid, "local": str(root / ".project-map" / "index.html")}

    if hosted:
        prefix = config["publish"].get("prefix", "map")
        old_slug = entry.get("slug")
        if rotate or not entry.get("token"):
            entry["token"] = new_token()
        entry["slug"] = f"{prefix}-{pid}-{entry['token']}"[:50]
        page = (root / ".project-map" / "index.html").read_text()
        folder = site_dir(entry["slug"], f"{data['project']} map", page, {"map.json": json.dumps(data, indent=1)})
        data["url"] = deploy(config, folder, entry["slug"], dry_run)
        result["url"] = data["url"]
        if rotate and old_slug and old_slug != entry["slug"] and config["publish"].get("remove"):
            if dry_run:
                print("would run: " + shlex.join(fill(config["publish"]["remove"], slug=old_slug)), file=sys.stderr)
            else:
                subprocess.run(fill(config["publish"]["remove"], slug=old_slug), capture_output=True)

    maps_dir = DATA_DIR / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    (maps_dir / f"{pid}.json").write_text(json.dumps(data, indent=1))
    everything = write_registry(links)
    result["registry"] = str(DATA_DIR / "maps.json")

    if hosted:
        index = links.setdefault("index", {})
        if not index.get("token"):
            index["token"] = new_token()
        index["slug"] = f"{config['publish'].get('prefix', 'map')}s-{index['token']}"
        folder = site_dir(index["slug"], "Project maps", index_page(everything),
                          {"maps.json": json.dumps(everything, indent=1)})
        result["index_url"] = deploy(config, folder, index["slug"], dry_run)
        result["orbital_url"] = result["index_url"].rstrip("/")  # services.conf: projectMaps.url

    if not dry_run:
        private_write(CONFIG_DIR / "links.json", json.dumps(links, indent=2))
    return result


CHROME_PATHS = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"]
# Headless Chrome won't make a window narrower than 500 px, so a phone is a 390 px iframe.
# The framed copy reports its own sideways overflow to the wrapper with postMessage.
REPORT = ("<script>addEventListener('load',()=>setTimeout(()=>{const d=document.documentElement;"
          "parent.postMessage({pmOverflow:Math.max(0,d.scrollWidth-innerWidth)},'*');},300))</script>")
WRAPPER = ("<!doctype html><html><body style='margin:0;background:#000'>"
           "<iframe src='{src}' style='width:{width}px;height:2400px;border:0;display:block'></iframe>"
           "<script>addEventListener('message',e=>{{if(e.data&&'pmOverflow' in e.data)"
           "document.documentElement.setAttribute('data-pm-overflow',String(e.data.pmOverflow));}});"
           "</script></body></html>")


def chrome() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        if shutil.which(name):
            return shutil.which(name)
    return next((p for p in CHROME_PATHS if os.path.exists(p)), None)


def preview(root_arg: str) -> dict:
    """Screenshots at phone (390 px) and laptop (1280 px) width, and any sideways overflow in px."""
    root, _ = project_root(root_arg)
    page = root / ".project-map" / "index.html"
    if not page.exists():
        raise Fail(f"No map yet: {page} does not exist")
    return preview_page(page)


def preview_page(page: Path) -> dict:
    """preview() for any page: the screenshots land next to it."""
    browser = chrome()
    if not browser:
        raise Fail("No Chrome or Chromium found, so the page can't be previewed here")
    folder = page.parent
    text = page.read_text()
    probe = folder / ".preview-probe.html"
    probe.write_text(text.replace("</body>", REPORT + "</body>") if "</body>" in text else text + REPORT)
    result = {}
    try:
        for label, width in (("phone", 390), ("laptop", 1280)):
            wrapper = folder / f".preview-{label}.html"
            wrapper.write_text(WRAPPER.format(src=probe.name, width=width))
            common = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                      "--allow-file-access-from-files", f"--window-size={max(width, 500)},2400",
                      "--virtual-time-budget=5000"]
            shot = folder / f"preview-{label}.png" if page.name == "index.html" else folder / f"{page.stem}-{label}.png"
            # Framed pages paint oddly in a screenshot, so shoot the page itself at the narrowest
            # width Chrome allows (500 px, still phone layout below the usual 600 px breakpoints).
            shoot = common[:-2] + [f"--window-size={max(width, 500)},2400", "--virtual-time-budget=5000"]
            subprocess.run(shoot + [f"--screenshot={shot}", page.as_uri()], capture_output=True, timeout=90)
            dom = subprocess.run(common + ["--dump-dom", wrapper.as_uri()], capture_output=True, text=True, timeout=90)
            found = re.search(r'data-pm-overflow="(\d+)"', dom.stdout)
            result[label] = {"width": width, "screenshot": str(shot), "screenshot_width": max(width, 500),
                             "overflow_px": int(found.group(1)) if found else None}
    finally:
        for temp in folder.glob(".preview-*.html"):
            temp.unlink()
    return result


def link(root_arg: str) -> dict:
    config = load_config()
    template = config.get("publish", {}).get("url")
    if not template:
        raise Fail("No hosting configured: maps stay local (publish.url in config.json)")
    root, name = project_root(root_arg)
    links = load_links()
    entry = links.get("projects", {}).get(map_id(root, name, links), {})
    index = links.get("index", {})
    if not entry.get("slug"):
        raise Fail("This project's map hasn't been published yet")
    result = {"url": template.format(slug=entry["slug"])}
    if index.get("slug"):
        result["index_url"] = template.format(slug=index["slug"])
    return result


# ---------------------------------------------------------------- Orbital

def orbital_url(config: dict) -> str | None:
    return (os.environ.get("ORBITAL_URL") or config.get("orbital", {}).get("url") or "").rstrip("/") or None


def taxiql(query: str, givens: list[str] | None = None) -> object:
    """Run TaxiQL against the configured Orbital. givens: NAME=TYPE:VALUE, added as a given block."""
    base = orbital_url(load_config())
    if not base:
        raise Fail("No Orbital configured (set orbital.url in config.json or ORBITAL_URL)")
    path = Path(query)
    if not path.exists():
        path = QUERIES_DIR / (query if query.endswith(".taxiql") else query + ".taxiql")
    text = path.read_text() if path.exists() else query
    if givens:
        facts = []
        for given in givens:
            name, _, typed = given.partition("=")
            kind, _, value = typed.partition(":")
            facts.append(f"  {name} : {kind} = {json.dumps(value)}")
        text = "given {\n" + ",\n".join(facts) + "\n}\n" + text
    request = urllib.request.Request(base + "/api/taxiql", data=text.encode(), method="POST",
                                     headers={"Content-Type": "application/taxiql", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise Fail(f"Orbital rejected the query (HTTP {exc.code}): {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Fail(f"Orbital at {base} is not reachable: {exc}") from None


def github(owner_repo: str) -> dict:
    """Open issues and recent CI runs: Orbital (github.taxi) first, the gh CLI second."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", owner_repo):
        raise Fail("Give the repository as OWNER/REPO")
    owner, repo = owner_repo.split("/")
    givens = [f"owner=github.RepoOwner:{owner}", f"repo=github.RepoName:{repo}"]
    try:
        return {"source": "orbital", "issues": taxiql("github-open-issues", givens),
                "runs": taxiql("github-ci-runs", givens)}
    except Fail as orbital_problem:
        note = str(orbital_problem)
    if shutil.which("gh"):
        issues = run(["gh", "api", f"repos/{owner}/{repo}/issues?state=open&per_page=50"])
        runs = run(["gh", "run", "list", "-R", owner_repo, "--limit", "10",
                    "--json", "name,status,conclusion,headSha,createdAt,url"])
        if issues is not None:
            return {"source": "gh", "note": note,
                    "issues": [{"number": i["number"], "title": i["title"], "state": i["state"],
                                "url": i["html_url"], "pullRequest": "pull_request" in i}
                               for i in json.loads(issues)],
                    "runs": json.loads(runs) if runs else []}
    return {"source": "none", "note": note + "; gh is not available either", "issues": [], "runs": []}


# ---------------------------------------------------------------- where are we

def summarise(data: dict) -> str:
    lines = [f"{data['project']}: {data['nextMilestone']['left']} left before "
             f"\"{data['nextMilestone']['name']}\" (drawn {data['generatedAt']} at {data['commit']})"]
    for part in data["parts"]:
        line = f"  {part['status']:<11} {part['name']} ({part['done']} of {part['total']}): {part['summary']}"
        if part["status"] == "stuck":
            line += f" [waiting on {part['waitingOn']}]"
        lines.append(line)
    for decision in data.get("decisions", []):
        if decision.get("status") == "open":
            lines.append(f"  your call: {decision['question']} (going with {decision['defaultChoice']})")
    lines.append(f"  next: {data['nextStep']['text']}")
    if data.get("url"):
        lines.append(f"  map: {data['url']}")
    return "\n".join(lines)


def where(root_arg: str, everything: bool) -> str:
    if everything:
        try:
            rows = taxiql("where-are-we")
            return "\n".join(f"{r['project']}: {r['left']} left before \"{r['milestone']}\"; next: {r['nextStep']}"
                             for r in rows) + "\n(answered by Orbital from project-map.taxi)"
        except Fail as problem:
            registry = DATA_DIR / "maps.json"
            if not registry.exists():
                raise Fail(f"No maps registered yet ({problem})") from None
            return "\n\n".join(summarise(m) for m in json.loads(registry.read_text())) + \
                f"\n(from {registry}; Orbital: {problem})"
    root, _ = project_root(root_arg)
    data = read_map(root)
    head = run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"])
    text = summarise(data)
    if head and not head.startswith(data["commit"][:7]) and not data["commit"].startswith(head):
        behind = run(["git", "-C", str(root), "rev-list", "--count", f"{data['commit']}..HEAD"])
        text += f"\n  (the map is {behind or 'some'} commit(s) behind HEAD {head}; refresh it)"
    return text


# ---------------------------------------------------------------- cli

class _Live:
    """This module's globals as attributes, read when used, for a copy loaded outside sys.modules."""

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None


def load_supermap():
    """supermap.py, next to this file, bound to this module's config, registry and hosting."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("supermap", Path(__file__).resolve().with_name("supermap.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    this = sys.modules.get(__name__)
    module.bind(this if getattr(this, "load_supermap", None) is load_supermap else _Live())
    return module


def serve(port: int, host: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(DATA_DIR))
    print(f"serving {DATA_DIR} at http://{host}:{port}/maps.json (Ctrl+C stops)")
    http.server.ThreadingHTTPServer((host, port), handler).serve_forever()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["super"]:  # its own options, which argparse.REMAINDER would drop when they come first
        try:
            return load_supermap().main(argv[1:])
        except Fail as problem:
            print(f"pm.py: {problem}", file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(prog="pm.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("root").add_argument("path", nargs="?", default=".")
    sub.add_parser("guard")
    sub.add_parser("check").add_argument("root")
    p = sub.add_parser("publish")
    p.add_argument("root")
    p.add_argument("--rotate", action="store_true", help="replace the project's secret link")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--local", action="store_true", help="register it for where --all and Orbital, but don't host it")
    sub.add_parser("preview").add_argument("root")
    sub.add_parser("link").add_argument("root", nargs="?", default=".")
    p = sub.add_parser("where")
    p.add_argument("root", nargs="?", default=".")
    p.add_argument("--all", action="store_true", help="every registered project")
    sub.add_parser("github").add_argument("repo")
    p = sub.add_parser("orbital")
    p.add_argument("query")
    p.add_argument("--given", action="append", default=[], help="NAME=TYPE:VALUE")
    p = sub.add_parser("super", add_help=False, help="the supermap (pm.py super --help)")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=9733)
    p.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    try:
        if args.command == "guard":
            return guard()
        if args.command == "root":
            root, name = project_root(args.path)
            print(json.dumps({"root": str(root), "name": name, "id": map_id(root, name, load_links())}))
        elif args.command == "check":
            root, _ = project_root(args.root)
            read_map(root)
            print("map.json meets the contract")
        elif args.command == "publish":
            print(json.dumps(publish(args.root, args.rotate, args.dry_run, args.local), indent=1))
        elif args.command == "preview":
            print(json.dumps(preview(args.root), indent=1))
        elif args.command == "link":
            print(json.dumps(link(args.root), indent=1))
        elif args.command == "where":
            print(where(args.root, args.all))
        elif args.command == "github":
            print(json.dumps(github(args.repo), indent=1))
        elif args.command == "orbital":
            print(json.dumps(taxiql(args.query, args.given), indent=1))
        elif args.command == "serve":
            serve(args.port, args.host)
        return 0
    except Fail as problem:
        print(f"pm.py: {problem}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
