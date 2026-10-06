# Case study: Weekly Music Digest

**Status: built and tested end to end** — genre-based discovery,
artist-similarity discovery, deduplication, and a weekly automated
email are all running together as one scheduled pipeline.

---

## Overview

A fully autonomous, serverless system that solves a personal problem:
new music overwhelm. Rather than scrolling endlessly to find new sounds
worth listening to, this pipeline runs once a week on its own, pulls
genre-matched and taste-matched music picks, filters out anything
already seen, and delivers a short, curated list straight to an inbox
— no manual searching required.

---

## The problem

New music is released constantly, and sifting through it to find
anything actually worth listening to takes real mental energy —
energy that's often the first thing to go when things get overwhelming.
The goal wasn't just "more music recommendations" — it was the
opposite: a deliberately small, curated weekly list that respects
limited attention rather than adding to the noise.

---

## The solution

A scheduled AWS pipeline with two discovery sources feeding into one
deduplicated, capped weekly digest:

### How it works
1. **Scheduled trigger** — an EventBridge schedule fires automatically
   once a week (no manual action needed to kick it off).
2. **Genre-based discovery** — for each genre tag configured, the
   system pulls that genre's top albums from Last.fm, paging deeper
   into the list over time as earlier picks get used up.
3. **Taste-based discovery** — seeded with a few artists already known
   and liked, the system finds similar artists and pulls their top
   albums too — a second, more personalized discovery path alongside
   genre tags.
4. **Deduplication** — every pick that's ever been sent is logged in
   DynamoDB. Both discovery sources check against the same table, so
   nothing repeats across weeks, regardless of which path surfaced it
   first.
5. **Interleaving** — picks from every genre and every seed artist are
   round-robined together, so no single source dominates the week's
   list, then capped at a fixed, deliberately small number.
6. **Delivery** — the finished list is emailed automatically via SES,
   formatted as a short, scannable list with artist, album, source
   (genre or "similar to X"), and a listen link.

---

## Tech stack

- **AWS Lambda** (Python) — all discovery, filtering, interleaving, and
  email-building logic
- **Amazon EventBridge Scheduler** — the weekly, fully autonomous
  trigger — no human action starts a run
- **DynamoDB** — tracks every pick ever sent, keyed by a stable
  dedupe identifier
- **Amazon SES** — delivers the weekly email
- **Last.fm API** — free, no-subscription-required source for genre-tag
  data and artist-similarity data

---

## My role

Designed and built the entire pipeline solo, including pivoting the
original data-source choice mid-build after discovering a platform
policy change, and later extending the system with a second discovery
method without breaking or rewriting the original working version.

---

## Challenges and what I learned

- **A platform policy changed mid-project.** The original plan used
  Spotify's API, but Spotify now requires a paid Premium subscription
  just to create a developer app — a free-tier blocker discovered only
  after starting to build. Rather than pay for access, pivoted to
  Last.fm, a fully free alternative, and reframed the project's goal
  slightly in the process: not strictly "new releases" (which Last.fm
  can't reliably provide — it has no release-date data), but "genre
  and taste-matched picks not seen yet." The reframed version arguably
  fit the original goal (reducing overwhelm, creating space for
  discovery) just as well.
- **Default Lambda timeout is 3 seconds — too short for multiple
  sequential API calls.** The very first real run timed out
  immediately, since a function making several sequential HTTP calls
  to an external API easily exceeds AWS's default. Fixed by explicitly
  raising the function's configured timeout — a setting separate from
  the code itself, easy to overlook since nothing in the code "looks"
  wrong when this happens.
- **A blank environment variable isn't the same as a missing one.**
  `os.environ.get("X", "3")` only falls back to the default when the
  variable doesn't exist at all — if it exists but is empty, the empty
  string is returned instead, breaking any code that tries to convert
  it to a number. A subtle distinction that caused a confusing crash
  traced back to one blank field in the Lambda console.
- **Extending a working system without breaking it required precision,
  not just more code.** Adding a second discovery method (artist
  similarity) to an already-working genre-based system meant finding
  the exact existing functions and variables to reuse (the same dedupe
  table, the same round-robin logic) rather than writing a parallel,
  disconnected feature — a different skill than building the first
  version from scratch.

---

## Results

The pipeline has been tested end to end multiple times, confirming:
- Scheduled runs execute automatically via EventBridge with no manual
  trigger
- Genre-based and artist-similarity-based picks both return real,
  relevant results
- Deduplication correctly prevents repeat picks across multiple runs
- The weekly email arrives formatted and readable, with working listen
  links

*[Once this has run for a few real weeks: note how the genre/seed-artist
tuning evolved, and whether the picks are holding up as genuinely
useful discoveries over time.]*

---

## What I'd improve next

- Cross-reference picks against MusicBrainz to add real release-date
  filtering, since Last.fm has no native concept of "new"
- Add a lightweight feedback loop (a thumbs up/down reply) so future
  weeks could weight picks based on what was actually liked
- Expand seed artists and genres gradually, rather than all at once, to
  keep tuning manageable
- Consider scoping IAM permissions down from broad managed policies to
  least-privilege, resource-specific ones, same lesson carried over
  from the sneaker pipeline project

---
