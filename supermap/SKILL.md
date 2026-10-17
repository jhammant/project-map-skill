---
name: supermap
description: One map of all your project maps, and of the work that has no map. Shows what needs you (open calls, stuck parts, sessions that ended on a question to you), what could be lost (never committed, uncommitted and untouched, unpushed, on no remote, stashed, in a temp folder), what has gone quiet, by project and by day, with a one-step restart for any project. Use for "/supermap", "map of my maps", "have I lost anything", "what have I dropped", "what did I work on on Tuesday / this week", "restart X", "pick X back up", "what's waiting on me across projects", "answer my open calls", "park X", "I'm done with X".
---

# /supermap

`PM` means `python3 ~/.claude/skills/map/scripts/pm.py`. The supermap lives beside /map's helper (`supermap.py`).

## /supermap
Run `PM super`. It scans every repo under your project folders, every map (registered or not) and your interactive Claude Code sessions, in about 2 s (about 10 s the first time). It writes `~/.local/share/project-map/super/supermap.html` and prints a summary. Then `PM super open`.

Answer in this order, briefly:
1. What needs you.
2. The worst at-risk work, never committed or uncommitted on no remote first.
3. The top 3 projects that went quiet, each with what's still open.
4. What changed since the last day on record.

End with the next moves in one line: restart one, answer the open calls, or park what's paused on purpose. The scan is the answer; don't re-derive it from code.

For a day or a week ("what did I do on Tuesday?", "what did I drop last week?"), read `~/.local/share/project-map/super/supermap.json` and answer from each project's `days`, where `c` is commits, `s` sessions, `e` edits and `m` map draws.

## Restart a project
For "restart X" or "pick X back up", run `PM super restart X`. This is the default. It opens a new iTerm2 tab (Terminal if iTerm2 isn't running) in the project, running a fresh claude primed with a restart brief: the map's next step, stuck parts and open calls, the git state, how the last session ended, and STATE.md.

For "resume X" or "the old session", run `PM super restart X --resume`. `PM super brief X` shows the brief without opening anything. If the user didn't say which, restart fresh and mention that resume exists.

## /supermap calls
`PM super calls --json` lists every unanswered "Your call" across the maps, oldest first. Ask them with AskUserQuestion, up to 4 per round:
- header: the project id, 12 characters at most;
- question: the call's question;
- options: the call's options, at most 4, with the default first and "(default)" added to its label. "Other" lets the user type an answer.

For each answer, run `PM super answer <project> <decision> "<answer>"`. It fills the `Answer:` slot on that call's line in the project's plan.md, or adds a line there; the project's map agent reads it on the next draw. Calls left unanswered keep their defaults. Afterwards, name the maps that now have answers and offer to redraw them in the background with the project-map agent (mode `update`).

## Park and done
- "Park X [until …] [because …]": `PM super park X --until 14d --reason "..."`. `--until` takes a date or a length (14d, 2w). A parked project leaves Gone quiet and Needs you but stays in At risk, because a parked laptop-only repo can still be lost.
- "Unpark X": `PM super unpark X`.
- "I'm done with X": `PM super done X`. After that only never-committed work still shows.

## Publish
`PM super publish` puts a redacted copy at an unguessable link, using the publish command in `~/.config/project-map/config.json`. The copy has no map text (questions, next steps, part names), file paths, session titles, session last words, git branch names, commit subjects or commands: counts, what's at risk and each hosted map's own link only. `--rotate` replaces the link. When you share the link, say the copy is redacted and that it still opens every hosted map, so it should be treated like a password.

## Daily
A daily job runs `PM super --learn --publish --quiet` before the day review. Each run:
- refits "gone quiet" from the latest complete weeks of your own commits;
- snapshots every project; the "since …" changes and the forward check come from these snapshots;
- refreshes the redacted link;
- writes `nudge.json`, which dayreview turns into "You may have forgotten" items. `PM super nudge` shows them.

## Settings
The `super` section of config.json:

| Key | What it does |
|---|---|
| `roots` | Folders to scan. Default: whichever of ~/dev, ~/code, ~/src, ~/projects, ~/Developer, ~/work, ~/repos and ~/git exist. |
| `depth` | How deep to look for repos. Default 4. |
| `days` | The window, in days. Default 30. |
| `ignore` | Globs to skip. |
| `emails` | Extra commit emails that are yours. |
| `areas` | Folder prefix → area name. |
| `claude` | The command a restart runs, e.g. `claude --dangerously-skip-permissions`. Otherwise `claude` plus `CLAUDE_SESSIONS_CLAUDE_ARGS`. |
