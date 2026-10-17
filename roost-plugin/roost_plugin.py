#!/usr/bin/env python3
"""Roost menu for project-map: the supermap's counts, every running Claude window by project, and
each project's map, each with its actions in a submenu.

A refresh only reads supermap.json and asks ps and lsof which claude processes run where, so it
stays cheap. When the scan is over an hour old it starts one in the background for next time.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
PM = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "pm.py"
DATA = Path(os.environ.get("PROJECT_MAP_DATA_DIR", HOME / ".local" / "share" / "project-map")) / "super"
MODEL = DATA / "supermap.json"
LOCK = DATA / ".roost-scan.lock"
STALE = 3600  # seconds before a refresh asks for a background rescan
SEPARATOR = {"separator": True}
TITLE = "Maps"
SEV = {3: "act now", 2: "soon", 1: "note"}


def pm(*args):
    return [sys.executable, str(PM), *args]


def me(*args):
    return [sys.executable, "./roost_plugin.py", *args]


def run(argv, timeout=10):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def ago(ts):
    minutes = max(0, int((time.time() - (ts or 0)) / 60))
    if minutes < 60:
        return f"{minutes}m ago"
    return f"{minutes // 60}h ago" if minutes < 1440 else f"{minutes // 1440}d ago"


def load_model():
    try:
        return json.loads(MODEL.read_text()), MODEL.stat().st_mtime
    except (OSError, ValueError):
        return None, 0


def rescan_in_background(scanned_at):
    """One background scan at a time, and only when the last one is old."""
    if time.time() - scanned_at < STALE:
        return False
    try:
        if time.time() - LOCK.stat().st_mtime < 600:
            return False
    except OSError:
        pass
    try:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        LOCK.touch()
        subprocess.Popen(pm("super", "--quiet"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError:
        return False


def running_windows():
    """[(tty, cwd)] for each interactive claude in a terminal: ps for the ttys, one lsof for the folders."""
    ttys = {}
    for line in run(["ps", "-axo", "pid=,tty=,args="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, tty, args = parts
        argv = args.split()
        if tty in ("??", "") or os.path.basename(argv[0]) != "claude" or "-p" in argv or "--print" in argv:
            continue
        ttys[pid] = "/dev/" + tty
    if not ttys:
        return []
    cwd, pid = {}, None
    for line in run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(ttys)]).splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n") and pid:
            cwd[pid] = line[1:]
    return sorted({(ttys[p], cwd.get(p, "")) for p in ttys})


def project_for(path, projects):
    """The deepest project whose root holds path."""
    best = None
    for p in projects:
        root = p.get("root") or ""
        if root and (path == root or path.startswith(root + os.sep)) and (not best or len(root) > len(best["root"])):
            best = p
    return best


def actions(p, model):
    """A project's submenu: what's wrong, then what you can do about it."""
    items = [{"title": f"{SEV.get(f['sev'], '')}: {f['text']}"[:110]} for f in p.get("flags", [])[:4]]
    m = p.get("map") or {}
    if m and not m.get("problem"):
        items.append({"title": f"Map: {m.get('left')} left before {m.get('milestone')}"[:110]})
    if items:
        items.append(SEPARATOR)
    if m.get("index"):
        items.append({"title": "Open its map", "open": m["index"]})
    elif m.get("url"):
        items.append({"title": "Open its map", "open": m["url"]})
    if not p.get("missing") and p.get("root"):
        items.append({"title": "Restart fresh in a new tab", "run": pm("super", "restart", p["id"]),
                      "tooltip": "A new terminal tab running claude, primed with where this project stood"})
        if p.get("sessions"):
            items.append({"title": "Resume its last session in a new tab", "run": pm("super", "restart", p["id"], "--resume")})
        items.append({"title": "Open the folder", "open": p["root"]})
    if p.get("state") in ("parked", "done"):
        items.append({"title": "Unpark", "run": pm("super", "unpark", p["id"])})
    else:
        items.append({"title": "Park for two weeks", "run": pm("super", "park", p["id"], "--until", "14d"),
                      "confirm": f"Park {p['name']} for two weeks? It leaves Gone quiet and Needs you."})
        items.append({"title": "Mark done", "run": pm("super", "done", p["id"]),
                      "confirm": f"Mark {p['name']} done? Only never-committed work will still show."})
    return items


def label(p):
    flags = p.get("flags") or []
    worst = max((f["sev"] for f in flags), default=0)
    mark = {3: "● ", 2: "◐ "}.get(worst, "")
    return f"{mark}{p['name']}"[:60]


def section(title, projects, model, limit=15):
    if not projects:
        return {"title": f"{title}: none", "disabled": True}
    items = [{"title": label(p), "items": actions(p, model)} for p in projects[:limit]]
    if len(projects) > limit:
        items.append({"title": f"…and {len(projects) - limit} more on the page", "open": str(DATA / "supermap.html")})
    return {"title": f"{title} ({len(projects)})", "items": items}


def running_section(model):
    windows = running_windows()
    if not windows:
        return {"title": "Running now: no Claude windows", "disabled": True}
    projects = (model or {}).get("projects", [])
    items = []
    for tty, cwd in windows:
        p = project_for(cwd, projects)
        name = p["name"] if p else (cwd.replace(str(HOME), "~") or tty)
        sub = [{"title": "Bring to front", "run": me("focus", tty)}]
        if p:
            sub += [SEPARATOR] + actions(p, model)
        items.append({"title": (label(p) if p else name) + f"  ({tty.rsplit('/', 1)[-1]})", "items": sub})
    items.sort(key=lambda i: i["title"].lstrip("●◐ "))
    return {"title": f"Running now ({len(windows)})", "items": items}


def menu():
    model, scanned_at = load_model()
    if not PM.exists():
        return {"title": TITLE, "items": [{"title": f"pm.py not found at {PM}"}]}
    if not model:
        rescan_in_background(0)
        return {"title": TITLE, "items": [{"title": "No supermap yet: scanning in the background…"},
                                          {"title": "Scan now", "run": pm("super", "--quiet")}]}
    started = rescan_in_background(scanned_at)
    ps = model["projects"]
    needs = [p for p in ps if any(f["view"] == "needs" for f in p["flags"])]
    risk = [p for p in ps if any(f["view"] == "risk" and f["sev"] >= 2 for f in p["flags"])]
    risk.sort(key=lambda p: -max(f["sev"] for f in p["flags"] if f["view"] == "risk"))
    quiet = [p for p in ps if p.get("state") == "quiet"]
    maps = sorted((p for p in ps if p.get("map")), key=lambda p: p["name"].lower())
    # amber for what needs you; red only for risks that are new since the last day on record, so it stays worth a look
    fresh = [i for i in (model.get("changes") or {}).get("items", []) if i.get("kind") == "new"]
    badge = {"text": str(len(fresh)), "level": "alert"} if fresh else (
        {"text": str(len(needs)), "level": "warn"} if needs else None)
    links = {}
    try:
        links = json.loads((HOME / ".config" / "project-map" / "links.json").read_text())
    except (OSError, ValueError):
        pass
    items = [
        {"title": f"{len(needs)} need you · {len(risk)} at risk · {len(quiet)} gone quiet",
         "open": str(DATA / "supermap.html"), "tooltip": "Open the supermap"},
        {"title": f"Scanned {ago(scanned_at)}" + (" · rescanning…" if started else ""), "disabled": True},
        SEPARATOR,
        running_section(model),
        section("Needs you", needs, model),
        section("At risk", risk, model),
        section("Gone quiet", quiet, model),
        {"title": f"All maps ({len(maps)})", "items": [{"title": p["name"][:60], "items": actions(p, model)} for p in maps]
         or [{"title": "No maps yet: type /map in a project"}]},
        SEPARATOR,
        {"title": "Open the supermap", "open": str(DATA / "supermap.html")},
        {"title": "Answer the open calls in Claude…", "run": me("calls"),
         "tooltip": "A new terminal tab running /supermap calls"},
        {"title": "Rescan now", "run": pm("super", "--quiet")},
    ]
    slug = (links.get("super") or {}).get("slug")
    config = {}
    try:
        config = json.loads((HOME / ".config" / "project-map" / "config.json").read_text())
    except (OSError, ValueError):
        pass
    template = (config.get("publish") or {}).get("url")
    if slug and template:
        items.append({"title": "Open the redacted link (phone-safe)", "open": template.format(slug=slug)})
    out = {"title": TITLE, "items": items}
    if badge:
        out["badge"] = badge
    return out


FOCUS = '''tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "%s" then
          select w
          tell t to select
          tell s to select
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "%s" then
        set selected of t to true
        set index of w to 1
        activate
        return "ok"
      end if
    end repeat
  end repeat
end tell
return "missing"'''


def main(argv):
    if argv[:1] == ["focus"] and len(argv) == 2 and argv[1].startswith("/dev/tty"):
        tty = argv[1].replace('"', "")
        print(run(["osascript", "-e", FOCUS % (tty, tty)]).strip())
        return
    if argv[:1] == ["calls"]:
        import importlib.util
        spec = importlib.util.spec_from_file_location("pm", PM)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.load_supermap().open_tab(f"cd {HOME / 'dev'} && claude \"/supermap calls\"")
        return
    print(json.dumps(menu()))


if __name__ == "__main__":
    main(sys.argv[1:])
