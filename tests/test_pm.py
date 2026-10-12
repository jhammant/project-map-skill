"""Offline tests for pm.py: the write guard, the map contract, publishing and Orbital calls."""
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SAMPLE = json.loads((REPO / "examples" / "sample-map.json").read_text())


def load_pm():
    spec = importlib.util.spec_from_file_location("pm", REPO / "skill" / "scripts" / "pm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pm(tmp_path, monkeypatch):
    module = load_pm()
    monkeypatch.setattr(module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.delenv("ORBITAL_URL", raising=False)
    return module


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "demo-api"
    (root / ".project-map").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=T",
                    "commit", "-q", "--allow-empty", "-m", "first"], check=True)
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    (root / ".project-map" / "map.json").write_text(json.dumps(dict(SAMPLE, commit=sha)))
    (root / ".project-map" / "index.html").write_text("<!doctype html><html><head><title>m</title></head><body>map</body></html>")
    return root


def guarded(pm, path):
    payload = io.StringIO(json.dumps({"tool_name": "Write", "tool_input": {"file_path": path}}))
    return pm.guard(payload)


def test_guard_allows_only_the_map_folder_gitignore_and_memory(pm, tmp_path):
    assert guarded(pm, "/work/app/.project-map/index.html") == 0
    assert guarded(pm, "/work/app/.gitignore") == 0
    assert guarded(pm, str(tmp_path / "memory" / "MEMORY.md")) == 0
    assert guarded(pm, "/work/app/src/main.py") == 2
    assert guarded(pm, "/work/app/.project-map/../src/main.py") == 2, "../ must not escape"
    assert guarded(pm, "/work/app/.project-mapper/x") == 2, "look-alike folder"


def test_sample_meets_the_contract(pm):
    assert pm.check_map(SAMPLE) == []


@pytest.mark.parametrize("change, problem", [
    (lambda m: m["parts"][2].update(waitingOn=None), "waitingOn must say what it waits for"),
    (lambda m: m["parts"][0].update(status="finished"), "status must be one of"),
    (lambda m: m["parts"][0].update(done=7), "done must be between 0 and total"),
    (lambda m: m.update(generatedAt="yesterday"), "ISO 8601"),
    (lambda m: m["decisions"][0].update(status="maybe"), "open or decided"),
    (lambda m: m.update(parts=[]), "at least one part"),
    (lambda m: m["nextMilestone"].update(left="3"), "nextMilestone.left must be int"),
])
def test_contract_explains_each_problem(pm, change, problem):
    broken = json.loads(json.dumps(SAMPLE))
    change(broken)
    assert any(problem in p for p in pm.check_map(broken)), pm.check_map(broken)


def test_taxi_schema_and_map_contract_agree():
    """Every field project-map.taxi reads must exist in a valid map, so Orbital never binds nulls."""
    taxi = (REPO / "taxi" / "src" / "project-map.taxi").read_text()
    models = dict(re.findall(r"model (\w+) \{([^}]*)\}", taxi))
    fields = {name: re.findall(r"^\s*(\w+)\s*:", body, re.M) for name, body in models.items()}
    optional = set(re.findall(r"^\s*(\w+)\s*:\s*[\w.]+\?", taxi, re.M))
    assert set(fields["ProjectMap"]) - optional <= set(SAMPLE)
    assert set(fields["NextMilestone"]) <= set(SAMPLE["nextMilestone"])
    assert set(fields["NextStep"]) <= set(SAMPLE["nextStep"])
    assert set(fields["MapPart"]) <= set(SAMPLE["parts"][0])
    assert set(fields["MapDecision"]) <= set(SAMPLE["decisions"][0])


def test_local_publish_builds_the_registry_without_hosting(pm, project):
    result = pm.publish(str(project))
    assert "url" not in result
    registry = json.loads((pm.DATA_DIR / "maps.json").read_text())
    assert [m["project"] for m in registry] == ["demo-api"]
    assert registry[0]["folder"] == project.name, "maps say which folder they are, for tools that know it by that"
    assert "Public beta" in (pm.DATA_DIR / "index.html").read_text()


def write_config(pm, tmp_path):
    recorder = tmp_path / "record.py"
    recorder.write_text("import json, sys\nopen(sys.argv[1], 'a').write(json.dumps(sys.argv[2:]) + '\\n')\n")
    log = tmp_path / "deploys.log"
    pm.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (pm.CONFIG_DIR / "config.json").write_text(json.dumps({"publish": {
        "command": f"{sys.executable} {recorder} {log} deploy {{dir}} {{slug}}",
        "remove": f"{sys.executable} {recorder} {log} remove {{slug}}",
        "url": "https://maps.example.com/{slug}/", "prefix": "map"}}))
    return log


def test_hosted_publish_uses_unguessable_slugs_and_private_pages(pm, project, tmp_path):
    log = write_config(pm, tmp_path)
    first = pm.publish(str(project))
    slug = first["url"].split("/")[-2]
    assert re.fullmatch(r"map-demo-api-[a-z2-7]{20}", slug) and len(slug) <= 50
    assert re.fullmatch(r"https://maps\.example\.com/maps-[a-z2-7]{20}/", first["index_url"])
    page = (pm.DATA_DIR / "sites" / slug / "public" / "index.html").read_text()
    assert '<meta name="referrer" content="no-referrer">' in page and 'content="noindex"' in page
    hosted_maps = json.loads((pm.DATA_DIR / "sites" / first["index_url"].split("/")[-2] / "public" / "maps.json").read_text())
    assert hosted_maps[0]["url"] == first["url"], "Orbital gets each map's link too"
    links = pm.CONFIG_DIR / "links.json"
    assert stat.S_IMODE(links.stat().st_mode) == 0o600, "the secret links are private"
    assert pm.publish(str(project))["url"] == first["url"], "the link is stable between updates"
    assert pm.link(str(project)) == {"url": first["url"], "index_url": first["index_url"]}

    rotated = pm.publish(str(project), rotate=True)
    assert rotated["url"] != first["url"]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["remove", slug] in calls, "rotating takes the old page down"


def test_orbital_queries_get_given_facts_separated_by_commas(pm, monkeypatch):
    sent = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        sent["url"], sent["body"] = request.full_url, request.data.decode()
        return Response(b'[{"number": 1}]')

    monkeypatch.setenv("ORBITAL_URL", "http://orbital.test:9022/")
    monkeypatch.setattr(pm.urllib.request, "urlopen", fake_urlopen)
    result = pm.taxiql("github-open-issues", ["owner=github.RepoOwner:octo", "repo=github.RepoName:hello"])
    assert result == [{"number": 1}]
    assert sent["url"] == "http://orbital.test:9022/api/taxiql"
    assert sent["body"].startswith('given {\n  owner : github.RepoOwner = "octo",\n  repo : github.RepoName = "hello"\n}\n')
    assert "find { github.Issue[] }" in sent["body"]


def test_github_falls_back_when_neither_orbital_nor_gh_is_there(pm, monkeypatch):
    monkeypatch.setattr(pm.shutil, "which", lambda name: None)
    answer = pm.github("octo/hello")
    assert answer["source"] == "none" and "No Orbital configured" in answer["note"]


def test_where_says_when_the_map_is_behind(pm, project):
    assert "3 left before \"Public beta\"" in pm.where(str(project), False)
    subprocess.run(["git", "-C", str(project), "-c", "user.email=t@example.com", "-c", "user.name=T",
                    "commit", "-q", "--allow-empty", "-m", "second"], check=True)
    text = pm.where(str(project), False)
    assert "stuck" in text and "waiting on Your call" in text
    assert "1 commit(s) behind" in text


def test_same_folder_name_in_two_places_gets_two_ids(pm, tmp_path):
    links = {"projects": {"app": {"path": "/work/one/app"}}}
    assert pm.map_id(Path("/work/one/app"), "app", links) == "app"
    other = pm.map_id(Path("/work/two/app"), "app", links)
    assert other.startswith("app-") and other != "app"


@pytest.mark.skipif(load_pm().chrome() is None, reason="needs Chrome or Chromium")
def test_preview_measures_overflow_at_phone_width(pm, project):
    page = project / ".project-map" / "index.html"
    page.write_text("<!doctype html><html><head><meta name=viewport content='width=device-width'></head>"
                    "<body style='margin:0'><div style='width:700px;height:20px;background:red'></div></body></html>")
    result = pm.preview(str(project))
    assert result["phone"]["overflow_px"] == 310, "a 700 px block overflows a 390 px phone by 310 px"
    assert result["laptop"]["overflow_px"] == 0
    assert Path(result["phone"]["screenshot"]).exists()
    assert not list((project / ".project-map").glob(".preview-*.html")), "temporary files are cleaned up"
