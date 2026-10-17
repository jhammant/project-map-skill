# project-map-skill — state as of 17 Oct 2026

## What this is
/map draws one project's map; /supermap (new, `skill/scripts/supermap.py`, `pm.py super ...`) maps every
project on the Mac. It scans every .project-map (registered or not), every repo under ~/dev and Jon's
interactive Claude sessions. It shows what needs him, what could be lost and what went quiet, by project
and by day, and restarts any project in one step.

## Where it stands
- Done on `feat/supermap` (28e92dd, 89fabe4), with 37 tests passing:
  - scanner and page: Needs you, At risk, Gone quiet, By project, By day, Unmapped, Parked;
  - restart fresh (default) or resume; park and done;
  - `/supermap calls` writes answers into each plan.md;
  - redacted publish (no map text, paths, titles or commands);
  - nudge.json and the "gone quiet" refit;
  - detection of deleted repos;
  - the live server for the phone;
  - Roost "Maps" plugin (installed).
- Live, Mac only:
  - loom `supermap-daily` at 05:15: learn, scan, publish;
  - loom daemon `supermap-serve` on 127.0.0.1:8794, behind `tailscale serve` (https://ghost.tail22c501.ts.net:8794/);
  - the redacted link is in links.json under "super".
- dayreview `feat/supermap-nudge` (1c939fe, local only): "You may have forgotten" reads nudge.json. 75 tests pass.
- claude-sessions smart tiling is PR #2 (stacked on #1; squash-merge it). That repo is now owned by the other session.
- "Gone quiet" is 3 days, fit on the latest weeks. A walk-forward fit on August failed, so the number drifts;
  the forward check (from snapshots) can first score on 31 Oct.

## Next steps
1. Push and open a PR for feat/supermap (base main); watch CI on Python 3.9, 3.12 and 3.14.
2. Push dayreview feat/supermap-nudge and merge it, so the 05:30 review keeps the nudge.
3. After 31 Oct, read `pm.py super learn` → forward.share: did "quiet" calls stay quiet?
4. Answer the 44 open calls: `/supermap calls`, or on the phone page.

## Open questions
- Whether to register orbital-os (`pm.py publish --local`). Recommended no: the supermap already tracks it.
- orbital-home: keep the local Orbital demo (default).

## Ideas worth keeping
- Credit sessions by the git root of the files they edited, not by cwd; shell mentions only when they point
  at one project. Skip headless runs (promptSource sdk).
- Count a deleted repo as lost only if no repo still here has its commits (worktrees and copies are safe).
- Fit a drifting threshold walk-forward on the latest complete weeks, and score it forward from snapshots.
- An owner-only tailnet page with action buttons: tailscale serve login header, a Host check against DNS
  rebinding, and a token minted per server start in the page.

---
_Updated by `forkcode close` on 2026-10-17T10:56+01:00_
