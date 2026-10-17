"""The supermap against planted lost work: every planted case must be found, and nothing clean flagged."""
import datetime as dt
import importlib.util
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ME, OTHER = "me@example.com", "other@example.com"
NOW = time.time()


def load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "skill" / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cwd, *args, days_ago=0, email=ME):
    when = f"@{int(NOW - days_ago * 86400)} +0000"
    env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when, GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_EMAIL=email, GIT_AUTHOR_NAME="Me" if email == ME else "Other",
               GIT_COMMITTER_NAME="Me" if email == ME else "Other")
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout


def repo(path, commits=(), remote=None):
    """A repo with commits made `days_ago`, each (days_ago, email); pushed to a bare remote when given."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for i, (days_ago, email) in enumerate(commits):
        (path / f"file{i}.txt").write_text(f"{i}\n")
        git(path, "add", ".")
        git(path, "commit", "-q", "-m", f"change {i}", days_ago=days_ago, email=email)
    if remote:
        git(remote.parent, "init", "-q", "--bare", remote.name)
        git(path, "remote", "add", "origin", str(remote))
        git(path, "push", "-q", "-u", "origin", "main")
    return path


def stamp(days_ago):
    return dt.datetime.fromtimestamp(NOW - days_ago * 86400, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def session(folder, name, cwd, records, title=None, entry="cli", source="typed"):
    """A Claude Code transcript in the compact JSON lines Claude Code writes."""
    lines = []
    for kind, days_ago, content in records:
        record = {"type": kind, "entrypoint": entry, "cwd": str(cwd), "sessionId": name, "gitBranch": "main",
                  "timestamp": stamp(days_ago), "message": {"role": kind, "content": content}}
        if kind == "user" and isinstance(content, str):
            record["promptSource"] = source
        lines.append(json.dumps(record, separators=(",", ":")))
    if title:
        lines.append(json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": name}, separators=(",", ":")))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.jsonl").write_text("\n".join(lines) + "\n")


def edit(path):
    return [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": str(path), "old_string": "a", "new_string": "b"}}]


def project_map(root, decisions, plan=None, stuck_on=None):
    folder = root / ".project-map"
    folder.mkdir()
    parts = [{"id": "core", "name": "Core", "status": "in-progress", "done": 2, "total": 5, "summary": "half way",
              "evidence": ["tests"], "waitingOn": None, "changed": False}]
    if stuck_on:
        parts.append({"id": "billing", "name": "Billing", "status": "stuck", "done": 0, "total": 3, "summary": "blocked",
                      "evidence": ["issue"], "waitingOn": stuck_on, "changed": False})
    data = {"project": root.name.title(), "generatedAt": stamp(3), "commit": "HEAD", "branch": "main", "mode": "update",
            "nextMilestone": {"name": "Beta", "left": 3, "items": ["a", "b", "c"]},
            "nextStep": {"text": "Finish the core", "why": "it blocks the beta"},
            "parts": parts, "decisions": decisions, "milestones": [], "panels": []}
    (folder / "map.json").write_text(json.dumps(data))
    (folder / "index.html").write_text("<!doctype html><title>m</title>")
    (folder / "log.md").write_text(f"# log\n- {stamp(3)[:16]}Z first: drew it\n")
    if plan is not None:
        (folder / "plan.md").write_text(plan)


def build_world(tmp_path, monkeypatch):
    """Eleven projects with planted lost work, five sessions and a registry, all under tmp_path."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f"[user]\n\temail = {ME}\n\tname = Me\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("CLAUDE_SESSIONS_CLAUDE_ARGS", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude" / "projects"))  # for copies pm.main loads
    dev, remotes, temp = tmp_path / "dev", tmp_path / "remotes", tmp_path / "tmp"
    remotes.mkdir()

    clean = repo(dev / "tools" / "clean", [(4, ME), (2, ME)], remotes / "clean.git")
    quiet = repo(dev / "tools" / "localdirty", [(14, ME), (12, ME), (10, ME)])
    (quiet / "file0.txt").write_text("edited, never committed\n")
    (quiet / "notes.md").write_text("new file, never added\n")
    stashy = repo(dev / "tools" / "stashy", [(5, ME)], remotes / "stashy.git")
    (stashy / "file0.txt").write_text("half-done idea\n")
    git(stashy, "stash", "push", "-q", "-m", "half-done idea", days_ago=9)
    unpushed = repo(dev / "tools" / "unpushed", [(6, ME)], remotes / "unpushed.git")
    (unpushed / "more.txt").write_text("x\n")
    git(unpushed, "add", ".")
    git(unpushed, "commit", "-q", "-m", "not pushed", days_ago=3)
    git(unpushed, "worktree", "add", "-q", "-b", "side", str(dev / "tools" / "unpushed-wt"))
    (dev / "tools" / "unpushed-wt" / "wt.txt").write_text("dirty in the worktree\n")
    clone = repo(dev / "tools" / "clone", [(1, OTHER)])
    (clone / "file0.txt").write_text("local tweak to someone else's code\n")
    fresh = dev / "lab" / "fresh"
    fresh.mkdir(parents=True)
    git(fresh, "init", "-q", "-b", "main")
    (fresh / "idea.py").write_text("print('never committed')\n")

    mapped = repo(dev / "lab" / "mapped", [(8, ME), (3, ME)], remotes / "mapped.git")
    project_map(mapped, [
        {"id": "plans", "question": "Monthly or annual plans first?", "options": ["Monthly", "Annual"],
         "defaultChoice": "Monthly", "status": "open", "answer": None},
        {"id": "host", "question": "Which host should the beta run on?", "options": ["Fly", "Render"],
         "defaultChoice": "Fly", "status": "open", "answer": None},
        {"id": "brand", "question": "Keep the working name for launch? (notes in ~/private/brand.md)", "options": ["Keep", "Rename"],
         "defaultChoice": "Keep", "status": "open", "answer": None},
    ], plan="# plan\n\n## Open questions\n\n- plans: Monthly or annual plans first? Default: Monthly. Answer: Annual\n"
            "- host: Which host should the beta run on? Default: Fly. Answer:\n",
        stuck_on="Your decision on pricing")
    orphan = repo(dev / "lab" / "orphan", [(2, ME)], remotes / "orphan.git")
    project_map(orphan, [])
    asker = repo(dev / "lab" / "asker", [(20, ME)], remotes / "asker.git")
    moved = repo(dev / "attic" / "gone", [(2, ME)], remotes / "gone.git")

    pm = load("pm")
    monkeypatch.setattr(pm, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(pm, "DATA_DIR", tmp_path / "data")
    sm = load("supermap")
    sm.bind(pm)
    monkeypatch.setattr(sm, "CLAUDE_PROJECTS", tmp_path / "claude" / "projects")
    monkeypatch.setattr(sm, "STYLE_MEMORY", tmp_path / "memory")
    monkeypatch.setattr(sm, "TEMP_PREFIXES", (str(temp) + os.sep,))
    pm.CONFIG_DIR.mkdir(parents=True)
    (pm.CONFIG_DIR / "config.json").write_text(json.dumps({"super": {"roots": [str(dev)], "depth": 4}}))
    (pm.CONFIG_DIR / "links.json").write_text(json.dumps({"projects": {
        "mapped": {"path": str(mapped)}, "gone": {"path": str(dev / "lab" / "gone")}}}))

    sessions = sm.CLAUDE_PROJECTS
    session(sessions / "-dev", "parent-folder", dev, [
        ("user", 2.5, "tidy the mapped project"), ("assistant", 2.5, edit(mapped / "file0.txt")),
        ("user", 2.5, [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        ("assistant", 2.4, [{"type": "text", "text": "Done."}])], title="Tidy mapped")
    session(sessions / "-asker", "asker-session", asker, [
        ("user", 2.2, "plan the migration"),
        ("assistant", 2.0, [{"type": "text", "text": "I have two plans. Should I go with the safe one?"}])],
        title="Migration plan")
    session(sessions / "-unpushed", "stopped-session", unpushed, [
        ("user", 3.1, "run the release"),
        ("assistant", 3.0, [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "make release"}}])])
    session(sessions / "-clean", "headless", clean, [
        ("user", 1, "You are a learning-signal extractor."), ("assistant", 1, edit(clean / "file0.txt"))],
        entry="sdk-cli", source="sdk")
    session(sessions / "-dev", "survey", dev, [  # reads across two repos and edits nothing
        ("user", 1.2, "which of my tools still build?"),
        ("assistant", 1.2, [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command":
            f"ls {clean}/a {clean}/b {clean}/c && ls {stashy}/a {stashy}/b {stashy}/c"}}]),
        ("assistant", 1.1, [{"type": "text", "text": "Both look fine."}])], title="Survey")
    scratch = temp / "scratch"
    scratch.mkdir(parents=True)
    session(sessions / "-tmp", "temp-work", scratch, [
        ("user", 1.5, "prototype it here"), ("assistant", 1.5, edit(scratch / "proto.py"))], title="Prototype")
    return {"sm": sm, "pm": pm, "dev": dev, "clean": clean, "quiet": quiet, "stashy": stashy, "unpushed": unpushed,
            "clone": clone, "fresh": fresh, "mapped": mapped, "orphan": orphan, "asker": asker, "moved": moved,
            "scratch": scratch, "tmp": tmp_path}


@pytest.fixture
def world(tmp_path, monkeypatch):
    return build_world(tmp_path, monkeypatch)


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    """One world, scanned once, for the tests that only read."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        built = build_world(tmp_path_factory.mktemp("world"), monkeypatch)
        built["model"] = built["sm"].scan()
        yield built


def by_root(model):
    return {p["root"]: p for p in model["projects"]}


def codes(project):
    return {f["code"] for f in project["flags"]}


def test_every_planted_case_is_found(scanned):
    world, model = scanned, scanned["model"]
    found = by_root(model)
    expect = {
        "quiet": {"uncommitted", "quiet"},          # local-only, edited, then left for ten days
        "stashy": {"stash"},                        # a nine-day-old stash
        "unpushed": {"unpushed", "session-stopped"},  # commits on no remote; its last session died mid-tool
        "fresh": {"never-committed"},
        "mapped": {"calls", "stuck-on-you"},
        "orphan": {"unregistered-map"},
        "asker": {"session-question"},
    }
    for name, wanted in expect.items():
        project = found[str(world[name])]
        assert wanted <= codes(project), (name, project["flags"])
    gone = next(p for p in model["projects"] if p.get("missing"))
    assert "missing" in codes(gone) and gone["movedTo"] == str(world["moved"])
    temp = found[str(world["scratch"])]
    assert "temp-folder" in codes(temp)


def test_severities_put_unrecoverable_work_first(scanned):
    world, found = scanned, by_root(scanned["model"])
    sev = lambda name, code: next(f["sev"] for f in found[str(world[name])]["flags"] if f["code"] == code)
    assert sev("fresh", "never-committed") == 3
    assert sev("quiet", "uncommitted") == 3, "recent, uncommitted and on no remote"
    assert sev("stashy", "stash") == 2, "stashes older than a week matter"


def test_nothing_clean_is_flagged_and_noise_is_ignored(scanned):
    world, model = scanned, scanned["model"]
    found = by_root(model)
    clean = found[str(world["clean"])]
    assert clean["flags"] == [] and clean["state"] == "moving"
    assert clean["sessions"] == [], "a headless claude -p run is not your work"
    assert str(world["clone"]) not in found, "a clone with only other people's commits is not your project"
    assert str(world["dev"] / "tools" / "unpushed-wt") not in found, "a worktree belongs to its repo"
    unpushed = found[str(world["unpushed"])]
    assert unpushed["git"]["untracked"] >= 1, "the worktree's changes count for its repo"
    assert sum(d.get("c", 0) for d in unpushed["days"].values()) == 2, "its commits aren't counted again via the worktree"
    assert not found[str(world["stashy"])]["sessions"], "a session that only listed files across repos isn't theirs"


def test_sessions_are_credited_to_the_project_they_edited(scanned):
    world, found = scanned, by_root(scanned["model"])
    mapped = found[str(world["mapped"])]
    assert [s["title"] for s in mapped["sessions"]] == ["Tidy mapped"], "started in the parent folder, edited mapped"
    assert any(day.get("e") for day in mapped["days"].values())
    asker = found[str(world["asker"])]["sessions"][0]
    assert asker["ended"] == "question" and asker["lastWords"].endswith("safe one?")


def test_answers_already_in_plan_md_close_the_call(scanned):
    calls = {c["id"]: c for c in by_root(scanned["model"])[str(scanned["mapped"])]["map"]["calls"]}
    assert calls["plans"]["answered"] == "Annual"
    assert calls["host"]["answered"] == "" and calls["brand"]["answered"] == ""


def test_answer_writes_into_plan_md_where_the_map_agent_reads_it(world):
    sm = world["sm"]
    sm.scan()
    sm.answer("mapped", "host", "Render")
    sm.answer("mapped", "brand", "Rename it")
    plan = (world["mapped"] / ".project-map" / "plan.md").read_text()
    assert re.search(r"^- host: .*Answer: Render \(via /supermap", plan, re.M), "filled the empty Answer: slot"
    assert "## Answers from /supermap" in plan and "- brand: Keep the working name for launch? (notes in ~/private/brand.md) Answer: Rename it" in plan
    assert "Answer: Annual" in plan, "your own lines stay as they were"
    with pytest.raises(world["pm"].Fail, match="already answers"):
        sm.answer("mapped", "plans", "Monthly")
    assert sm.open_calls(sm.scan()) == []


def test_park_silences_quiet_but_not_risk_and_done_silences_both(world):
    sm = world["sm"]
    sm.scan()
    sm.mark("localdirty", "parked", until="14d", reason="waiting for the API")
    parked = by_root(sm.scan())[str(world["quiet"])]
    assert parked["state"] == "parked" and "quiet" not in codes(parked)
    assert "uncommitted" in codes(parked), "a parked laptop-only repo can still be lost"
    sm.mark("stashy", "done")
    assert codes(by_root(sm.scan())[str(world["stashy"])]) == set()
    sm.mark("localdirty", None)
    assert by_root(sm.scan())[str(world["quiet"])]["state"] == "quiet"


def test_restart_brief_and_commands(scanned):
    world, sm, model = scanned, scanned["sm"], scanned["model"]
    quiet = by_root(model)[str(world["quiet"])]
    text = sm.brief(quiet, model)
    assert "localdirty" in text and "2 uncommitted files" in text and "No remote" in text
    fresh = sm.restart("localdirty", show=True)
    assert fresh["mode"] == "fresh" and fresh["command"].startswith(f"cd {world['quiet']} && claude ")
    brief_file = sm.home_dir() / "briefs" / "localdirty.md"
    assert brief_file.exists() and stat.S_IMODE(brief_file.stat().st_mode) == 0o600
    resume = sm.restart("asker", resume=True, show=True)
    assert resume["command"] == f"cd {world['asker']} && claude --resume asker-session"
    with pytest.raises(world["pm"].Fail, match="No session"):
        sm.restart("stashy", resume=True, show=True)


def test_page_is_self_contained_and_carries_every_view(scanned):
    sm = scanned["sm"]
    page = (sm.home_dir() / "supermap.html").read_text()
    for view in ("needs", "risk", "quiet", "projects", "days", "unmapped", "parked"):
        assert f"id:'{view}'" in page
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=", page), "no network requests"
    assert '<meta name="robots" content="noindex">' in page
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S).group(1))
    assert data["projects"], "the data parses back"
    assert stat.S_IMODE((sm.home_dir() / "supermap.json").stat().st_mode) == 0o600


def test_published_copy_is_redacted(world, tmp_path):
    sm, pm = world["sm"], world["pm"]
    config = json.loads((pm.CONFIG_DIR / "config.json").read_text())
    config["publish"] = {"command": "true", "url": "https://maps.example.com/{slug}/", "prefix": "map"}
    (pm.CONFIG_DIR / "config.json").write_text(json.dumps(config))
    result = sm.publish()
    slug = result["url"].split("/")[-2]
    assert re.fullmatch(r"map-super-[a-z2-7]{20}", slug)
    page = (pm.DATA_DIR / "sites" / slug / "public" / "index.html").read_text()
    for secret in (str(tmp_path), "Tidy mapped", "Migration plan", "safe one?", "--resume", "half-done idea",
                   "notes.md", "make release", "super restart", "not pushed", "change 1", "~/private/brand.md"):
        assert secret not in page, secret
    for words in ("Monthly or annual plans first?", "Finish the core", "Your decision on pricing", "Beta",
                  "it blocks the beta", "Which host should the beta run on?", "Orphan"):
        assert words not in page, f"map text on the link: {words}"  # some maps are kept local on purpose
    assert "never committed" in page and '"left":3' in page, "what's at risk and the counts stay"


def test_nudge_gives_one_item_per_slipping_project(world):
    sm = world["sm"]
    sm.scan()
    sm.mark("stashy", "parked")
    items = sm.nudge(sm.scan())["items"]
    roots = [i["root"] for i in items]
    assert len(roots) == len(set(roots)) and str(world["stashy"]) not in roots
    quiet = next(i for i in items if i["root"] == str(world["quiet"]))
    assert quiet["age_days"] == 10 and "restart localdirty" in quiet["where"]
    assert json.loads((sm.home_dir() / "nudge.json").read_text())["items"]


def test_changes_since_the_last_day_on_record(world):
    sm = world["sm"]
    sm.scan()
    db = sm.connect()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    db.execute("INSERT INTO snapshots VALUES (?, ?, ?)", (yesterday, str(world["clean"]), json.dumps(
        {"name": "clean", "state": "moving", "flags": ["unpushed"], "left": None})))
    db.execute("INSERT INTO snapshots VALUES (?, ?, ?)", (yesterday, str(world["stashy"]), json.dumps(
        {"name": "stashy", "state": "moving", "flags": [], "left": None})))
    db.commit()
    changes = sm.scan()["changes"]
    assert changes["since"] == yesterday
    texts = [i["text"] for i in changes["items"]]
    assert "clean: no longer unpushed commits" in texts and "stashy: stashed changes" in texts


def test_second_scan_reuses_the_cache(world, monkeypatch):
    sm = world["sm"]
    calls = []
    real = sm.git

    def counting(root, *args, **kwargs):
        calls.append(args[0])
        return real(root, *args, **kwargs)

    monkeypatch.setattr(sm, "git", counting)
    sm.scan()
    first = len(calls)
    calls.clear()
    sm.scan()
    assert len(calls) < first / 2, (first, len(calls))
    assert "log" not in calls, "git history is only reread when git changed the repo"


def test_survival_threshold_on_known_histories():
    sm = load("supermap")
    always_two = [[(dt.date(2026, 1, 1) + dt.timedelta(days=2 * i)).isoformat() for i in range(50)]]
    curve = sm.survival(sm.gaps(always_two, dt.date(2026, 6, 1)))
    assert curve[1] == 1.0 and curve[2] < 0.05, "every spell ends on day 2, bar the open one"
    assert sm.threshold(curve) == 2
    # each history: one spell that resumes on day 3, then one still open; past day 3 nothing resumes
    mixed = [["2026-01-01", "2026-01-04"]] * 20
    curve = sm.survival(sm.gaps(mixed, dt.date(2026, 6, 1)))
    assert sm.resumes(curve, 1) == pytest.approx(0.5) and sm.threshold(curve) == 3


def test_cli_routes_super_options_through_pm(world, capsys):
    pm = world["pm"]
    assert pm.main(["super", "--json", "--quiet"]) == 0
    assert pm.main(["super", "calls"]) == 0
    assert "open calls" in capsys.readouterr().out
    assert pm.main(["super", "restart", "nope"]) == 1
