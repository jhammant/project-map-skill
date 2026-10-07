---
name: map
description: Draw, update or read a project map (.project-map/index.html) for a big build, using the project-map subagent. Use for "/map", "draw a project map", "update the map", "where are we?", "what's left?", "where are we across everything?", and before letting Claude run on its own for a long stretch (overnight, "keep going", "work through the backlog").
---

# /map

`PM` means `python3 SKILL_DIR/scripts/pm.py`, where SKILL_DIR is this skill's folder.

## 1. Find the project
`PM root` prints the root (the git top level, else the current folder) and the map's id. The map lives in `<root>/.project-map/`.

## 2. Style (once, ever)
Look for a saved style: `grep -rl '## Style' ~/.claude/agent-memory/project-map/`. If any file has one, skip this step.
If a saved dashboard style exists (`~/.claude/agent-memory/dashboard-builder/MEMORY.md`), reuse its theme and accent, say so in one line, and don't ask.
Otherwise ask with AskUserQuestion. Subagents can't ask questions, so the asking happens here:
- "Dark or light?": Dark, Light.
- "One accent colour?": Orange #d97757, Blue #4493f8, Green #3fb950, Purple #a371f7. "Other" takes any colour.
Pass the answer in the brief; project-map saves it to its memory.

## 3. Brief and launch
Start the `project-map` subagent in the background with a brief:
- the project root;
- mode: `first` (no map yet), `update`, or `milestone: <name>`;
- what you're about to do or just did, with test results and their numbers;
- the style answer, if step 2 asked;
- decisions you want the user's call on, each with the default you'll take.
Keep working; don't wait. When it finishes, relay its summary in one or two lines with the local path and the URL if it printed one.

## 4. "Where are we?" / "What's left?"
- One project: `PM where <root>`. It reads the map, says if the map is behind HEAD, and summarises it. If it's behind, refresh the map in the background as well. No map: say so and offer to draw one (draw it straight away if a long run is underway).
- Every project ("across everything"): `PM where --all`. It asks Orbital (project-map.taxi) when one is configured, else reads the local registry.
Answer in this order: the next milestone and how many things are left; each part on one line, status first; what's stuck and what it's waiting on; open decisions with their defaults; the suggested next step. Link the map. The map is the answer; don't re-derive it from the code.

## 5. Other requests
- `/map open`: `open <root>/.project-map/index.html` (or `xdg-open`).
- `/map link`: `PM link <root>` prints the project's hosted URL and the all-projects index URL.
- `/map rotate`: `PM publish <root> --rotate` gives the project a new secret link and removes the old site when a remove command is configured. Use it when a link was shared too widely.

## During a long run
After each map comes back, do its suggested next step unless the user said otherwise. A "Your call" question with no answer: take the default, keep going, and say in the next brief that you did.
