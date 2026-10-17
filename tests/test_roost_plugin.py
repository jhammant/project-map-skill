"""The Roost menu, built from a supermap.json: sections, per-project actions and the badge."""
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load(tmp_path, monkeypatch, model):
    spec = importlib.util.spec_from_file_location("roost_plugin", REPO / "roost-plugin" / "roost_plugin.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    (tmp_path / "supermap.json").write_text(json.dumps(model))
    monkeypatch.setattr(plugin, "DATA", tmp_path)
    monkeypatch.setattr(plugin, "MODEL", tmp_path / "supermap.json")
    monkeypatch.setattr(plugin, "HOME", tmp_path)
    monkeypatch.setattr(plugin, "running_windows", lambda: [("/dev/ttys004", "/w/app/src"), ("/dev/ttys009", "/w")])
    monkeypatch.setattr(plugin, "rescan_in_background", lambda scanned_at: False)
    return plugin


def project(pid, root, flags, state="moving", **extra):
    return dict({"id": pid, "name": pid.title(), "root": root, "state": state, "flags": flags, "sessions": [],
                 "map": None}, **extra)


MODEL = {"projects": [
    project("app", "/w/app", [{"view": "needs", "code": "calls", "sev": 2, "text": "2 open calls"}],
            map={"index": "/w/app/.project-map/index.html", "left": 3, "milestone": "Beta"}),
    project("lab", "/w/lab", [{"view": "risk", "code": "never-committed", "sev": 3, "text": "never committed"}],
            state="quiet"),
], "changes": {"items": [{"kind": "new", "text": "Lab: never committed"}]}}


def titles(items):
    return [i.get("title", "") for i in items]


def test_menu_has_every_section_and_a_red_badge_only_for_new_risks(tmp_path, monkeypatch):
    plugin = load(tmp_path, monkeypatch, MODEL)
    menu = plugin.menu()
    assert menu["badge"] == {"text": "1", "level": "alert"}
    top = titles(menu["items"])
    for name in ("Running now (2)", "Needs you (1)", "At risk (1)", "Gone quiet (1)", "All maps (1)"):
        assert name in top
    calm = dict(MODEL, changes={"items": []})
    assert load(tmp_path, monkeypatch, calm).menu()["badge"] == {"text": "1", "level": "warn"}


def test_running_windows_are_named_by_their_project_with_its_actions(tmp_path, monkeypatch):
    plugin = load(tmp_path, monkeypatch, MODEL)
    running = next(i for i in plugin.menu()["items"] if i.get("title", "").startswith("Running now"))
    app = next(i for i in running["items"] if "App" in i["title"])
    actions = titles(app["items"])
    assert actions[0] == "Bring to front" and "Open its map" in actions and "Restart fresh in a new tab" in actions
    assert app["items"][0]["run"][-2:] == ["focus", "/dev/ttys004"]
    park = next(i for i in app["items"] if i.get("title") == "Park for two weeks")
    assert park["confirm"] and park["run"][-4:] == ["park", "app", "--until", "14d"]
    loose = next(i for i in running["items"] if "ttys009" in i["title"])
    assert titles(loose["items"]) == ["Bring to front"], "a window outside any project can still be found"
