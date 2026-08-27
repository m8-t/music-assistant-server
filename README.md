# Music Assistant Server - WiiM fork

Fork of [music-assistant/server](https://github.com/music-assistant/server) carrying a small
patch set on top of the upstream release tag (currently **2.10.0**). It exists because the
WiiM physical remote cannot skip tracks with stock Music Assistant, and because track
changes in the stock queue controller are slower and flakier than they need to be.

This branch (`feat/wiim-remote-transport`) is the fork's default branch and the only one
that matters. Everything else is untouched upstream history.

## What the patch set does

The functional changes live in the first commits on top of the tag, one concern per commit:

1. **`feat: add on_stream_requested hook to Player base class`**
   (`models/player.py`, `controllers/streams/controller.py`)
   A ~10 line generic hook: the stream server notifies the player object when a GET
   request for a queue item arrives. No behavior change for any other player.

2. **`feat: rewrite WiiM remote button detection using stream request signals`**
   (`providers/wiim/player.py`)
   Makes Next/Previous on the WiiM remote drive the MA queue. Both buttons produce an
   identical TRANSITIONING event with unchanged URI, so the button is identified by a
   deterministic side effect instead of position heuristics: on Previous the device
   re-requests the current item's stream URL within 0.5s, on Next it requests nothing.
   Rapid presses are coalesced via burst counters. Previous follows a press-count rule:
   first press restarts the track (device-native), second press within 10s jumps back.

3. **`fix: dispatch queue skips immediately on first press`**
   (`controllers/player_queues/controller.py`)
   Upstream debounces every next/previous with a flat 1s `call_later`, so every skip
   pays one second even for a single press; changed to leading-edge dispatch with
   trailing debounce only for presses inside the 1s window.

4. **`fix: skip scheduling metadata refresh tasks when online metadata is disabled`**
   (`controllers/metadata/controller.py`)
   When "Enable online metadata" is off, `schedule_update_metadata` still queued
   no-op refresh tasks, wasting the 2-slot background task queue. Prevents queue
   starvation during post-migration metadata floods (e.g. MusicBrainz lookup spikes).

The remaining `ci:` commits contain the build and automation described below.

## How the image is built

`.github/workflows/wiim-derived-image.yml` builds a derived image on top of the official
`ghcr.io/music-assistant/server:<base_version>` image instead of rebuilding from source:

- The patch is derived fresh on every run: `git diff <BRANCH_BASE tag>..HEAD -- music_assistant/`.
- Guards: the tag must be an ancestor of HEAD, the commit count is capped, and the patch
  may only touch an explicit file allowlist. Any upstream drift fails the build loudly
  instead of silently shipping a reverted upstream fix.
- `docker/wiim-derived.Dockerfile` applies the patch with `patch --fuzz=0`, rejects any
  `.rej`/`.orig` leftovers, recompiles bytecode, and greps content markers per patched file.
- Result: `ghcr.io/m8-t/music-assistant-server:<base>-wiim-remote.<run>` plus a moving
  `<base>-wiim-remote` tag. Deployment pins the moving tag by digest so Renovate surfaces
  every rebuild as a normal update MR.

## Staying current with upstream

`.github/workflows/upstream-bump.yml` runs daily: it checks upstream for a newer release
tag, updates the base version in the Dockerfile and workflow defaults, pushes the bump and
triggers a rebuild. Patch-level upstream updates therefore flow through automatically; the
build only goes red when a patch no longer applies cleanly, which is the signal that a
real rebase is due.

Inherited upstream workflows (release automation, tests, dependabot helpers) are disabled
on this fork; only the two workflows above plus the dependency graph are active.
