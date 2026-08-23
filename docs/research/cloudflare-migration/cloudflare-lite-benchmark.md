# Cloudflare `lite` runtime benchmark

Status: in progress
Date: 2026-08-23
Issue: https://github.com/pandacover/tomo_v2/issues/12

## Scope

This benchmark tests the packaged Tomo Python runtime without SuperGrok or another live model. A deterministic provider reproduces delayed and streamed output while the real runtime performs protocol parsing, SQLite/FTS5 persistence, alternating checkpoints, and image normalization.

The Cloudflare harness starts up to three fresh owner-keyed Sandbox instances concurrently on the `lite` shape and destroys them after each run. It records cold-start time, output-chunk timing, process RSS, cgroup memory, disk use, SQLite integrity, FTS5 results, and protocol completion.

## Local smoke results

These runs validate the harness only. They used Python 3.12 in the local Codex environment, not the Cloudflare `lite` container's Python 3.11 and 256 MiB cgroup.

| Scenario | Result | Wall time | Process high-water RSS | Notes |
| --- | --- | ---: | ---: | --- |
| Text | Pass | 743 ms | 73,776 KiB | Real runtime, two protocol events, SQLite integrity and FTS5 pass, two checkpoints |
| Typical images | Pass | 1,677 ms | 144,412 KiB | Eight sequential 2560×1920 images; eight vision observations |
| Maximum images | Pass locally | 4,228 ms | 309,196 KiB | Eight sequential 5000×4000 images at the 20 MP decoded-input limit |
| Delayed provider | Pass | 1,735 ms at a 1 s delay | 73,916 KiB | Confirms the fake wait and streamed-output path |

The maximum-image path exceeds the `lite` memory limit in this local process measurement. This is a warning, not the final Cloudflare result: the target image uses Python 3.11 and cgroup accounting includes the Sandbox runtime as well as Tomo. The Cloudflare run must determine whether `lite` OOMs and whether Tomo's 20 MP limit needs a later memory optimization or a larger container.

## Cloudflare runs still required

1. Three concurrent cold text turns.
2. Three concurrent typical-image turns.
3. Three concurrent 20 MP maximum-image turns.
4. Three concurrent 120-second waits.
5. A 720-second wait after the shorter cases succeed.
6. A sleep-and-wake run using the same owner identifiers (`sleepCycle=true`).

The live-model and SuperGrok authentication check remains separate. It is not needed to decide basic runtime fit.

## Current blocker

Deploying a Sandbox custom image requires Docker. The current execution environment has neither Docker nor a Cloudflare account connector, so it can prepare and validate the harness but cannot perform the real `lite` deployment. The remaining commands are documented in `tomo_core/cloudflare-benchmark/README.md` for an environment with Docker and Wrangler authentication.
