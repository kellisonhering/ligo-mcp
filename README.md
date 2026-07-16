# ligo-mcp

A gravitational-wave detection pipeline that pairs lightweight signal-processing (Q-transform + numerical features, no template bank) with an LLM triage layer — and, more importantly, an **evaluation harness** that measures how well that combination actually performs against a pre-registered set of rules.

The pipeline runs continuously as a macOS background service, pulling real LIGO/Virgo detector data from public archives (GWOSC), and hides synthetic gravitational-wave signals inside real noise so the pipeline's hit rate and false-alarm rate can be measured honestly.

---

## Why this exists

The question the project is trying to answer:

> Can a lightweight, agentic triage system — Q-transform image + LLM interpretation — flag real gravitational-wave candidates well enough to be useful, or does effective detection require matched-filter–level physics?

A methods paper is being drafted around the answer; results won't be shared until all 300 campaign runs complete and blinded scoring is done. The interesting result isn't whether the LLM flags GW150914 — that's a known-answer benchmark the loop re-runs periodically. It's the shape of the efficiency curve on the marginal signals — the ones with signal-to-noise ratio between 6 and 16, where the pipeline must distinguish weak signals from real detector noise.

## Pre-registration (the scientific credential)

Before running the injection campaign, the grading rules were **locked in advance** in [`REVIEW_FINDINGS.md`](REVIEW_FINDINGS.md) §7.5 — including what counts as a "catch", what counts as a false alarm, and the pre-committed interpretations for every possible result.

The proof that those rules were fixed *before* results were seen is this repository's git history. The lock commit is [`f335294003eb`](https://github.com/kellisonhering/ligo-mcp/commit/f335294003eb):

```
🔒 Lock injection campaign pre-registration (§7.5)
Committed 2026-07-12T14:48:25Z
```

Anyone can click that hash and see the commit timestamp on GitHub. If the rules change after this point, they appear as a new amendment section — never as an edit to §7.5. This is what turns "we ran an experiment" into "we ran a pre-registered experiment," which is the difference between a demo and a scientific result.

## The injection campaign

The evaluation harness runs 300 experiments interleaved with the normal survey loop:

- **200 injections** — synthetic binary-black-hole waveforms injected into real O3 detector noise. Signal strength stratified: 20% invisible (SNR 4–6), 60% marginal (6–16), 20% obvious (16–24).
- **100 noise-only controls** — no signal injected, so any "detection" here is a false alarm.

The pipeline is blind to which is which. All ground truth (injection parameters, strengths, sky positions) is written to a separate `campaign_truth.jsonl` file that isn't read until scoring, after all 300 runs are complete.

Every run also logs the decision a **physics-only baseline** (no LLM) would have made on the same detector summary, so the same grading rules produce a "does the LLM add anything?" comparison as a free byproduct.

The pre-registration commits to interpretations for every possible result — including the finding that the LLM adds no discrimination beyond the physics-only baseline. From §7.5: *"Every outcome above is publishable. There is no failure outcome."*

## How the LLM fits in

The LLM planner never sees raw detector strain. On each run it receives:

- A **numerical summary** (JSON dict): energy contrast, chirp-like flag, dual-detector coincidence flag with time-of-flight check, catalog cross-match results (Fermi GBM, IceCube, SNEWS, data-quality flags), and — when present — the vision model's classification of the spectrogram.
- The **Q-transform spectrogram image** is analyzed by Claude's vision model in a separate API call *before* the planner runs; the vision result is merged into the summary as `vision_signal_type`, `vision_shape`, `vision_confidence`, `vision_reasoning`, `vision_score_modifier`.

The planner returns a structured decision from an enforced whitelist — `archive`, `benchmark_validated`, `glitch_candidate`, `rerun`, `follow_up`, `candidate_for_human_review` — plus a numeric `interesting_score` clamped to `[0, 1]`. Any decision outside the whitelist is rewritten to `archive`. Parse failures and API exceptions default to `archive` with `interesting_score = 0.0`.

The planner never sees: raw strain data, injection ground truth, or any field named `injection`. Blinding is enforced at the code level by `_assert_blind()` in [`loop.py`](loop.py), which checks the summary against an allowlist of ~30 permitted key prefixes and raises `RuntimeError` before the LLM call if anything unexpected appears. Injection ground truth (masses, sky position, achieved SNR) lives only in `campaign_truth.jsonl`, which is written by [`injection_pool_generator.py`](injection_pool_generator.py) and read by no other file in the repository.

## Repository layout

**Start here:** [`REVIEW_FINDINGS.md`](REVIEW_FINDINGS.md) contains the design review and the locked pre-registration (§7.5); [`loop.py`](loop.py) is the main survey-loop entrypoint.

| File | What it does |
|---|---|
| [`loop.py`](loop.py) | The continuous survey loop — picks a target, runs the pipeline, calls the LLM planner, writes a record |
| [`pipeline.py`](pipeline.py) | Signal-processing: fetch strain data, Q-transform, energy contrast, chirp detection, coincidence |
| [`planner.py`](planner.py) | LLM-facing prompts (Anthropic Claude) — planner, vision, target selector |
| [`crossmatch.py`](crossmatch.py) | External catalog checks: Fermi GBM, IceCube, data quality, SNEWS |
| [`catalog.py`](catalog.py) | GWTC event lookup + subsolar candidate lists |
| [`canary.py`](canary.py) | Startup sanity checks — verifies every check works on a known-answer input |
| [`injection_pool_generator.py`](injection_pool_generator.py) | Pre-generates the 300 injection/noise specs used by the campaign |
| [`injections.py`](injections.py) | Applies an injection to a real strain window at runtime |
| [`server.py`](server.py) | FastMCP server exposing the pipeline as tools (integrates with the OpenClaw gateway) |
| [`reporter.py`](reporter.py) | Turns raw records into human-readable summaries and daily reports |
| [`REVIEW_FINDINGS.md`](REVIEW_FINDINGS.md) | The design review (July 8, 2026), the pre-registration (§7.5), and status notes |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | Fix batches (F1–F12) approved in phases, with completion status |

## Running it

**Dependencies:**

```bash
pip install -r requirements.txt
```

Uses `gwpy` and `gwosc` for LIGO data access, `anthropic` for the LLM planner, `fastmcp` for the MCP interface, and `numpy`/`scipy` for signal-processing.

**API key:** the planner reads `ANTHROPIC_API_KEY` from `~/.openclaw/.env`. If you're not running through OpenClaw, set it in your own environment.

**Data location:** all experiment output goes to `~/experiment-data/ligo/` (outside iCloud-synced folders to avoid mid-write file locks).

**Injection venv:** the injection pool generator uses `pycbc`, which is a heavy dependency. It lives in its own venv at `~/venvs/ligo-injections/` to keep the live loop's environment small:

```bash
python -m venv ~/venvs/ligo-injections
source ~/venvs/ligo-injections/bin/activate
pip install pycbc numpy
```

**As a background service (macOS):** the survey loop runs as a standalone launchd agent (`com.kellison.ligo-loop`) so it survives shell exits, sleep, and reboots, and auto-restarts on crash. The plist template lives in `~/Library/LaunchAgents/`; the pipeline's own MCP tools (`start_loop_service`, `stop_loop_service`, `loop_service_status`) manage it via `launchctl`.

## Engineering and evaluation design

This repo is the engineering side of a broader project on **evaluation harnesses for AI scientific judgment** — the recurring pattern of asking "can an LLM be trusted to make this call, and how would we know?" The design choices that make that measurable here:

- **Test infrastructure for a system that can't have a normal test suite** — you can't unit-test "is this a gravitational wave" because there's no ground truth. So we create controlled ground truth through synthetic injections and score against it.
- **Pre-registration as a QA discipline** — locking the grading rules before seeing results, using git as the tamper-evident timestamp.
- **Blinding boundaries in a codebase** — injection truth is architecturally isolated from the planner so the LLM can't accidentally see the answer key.
- **Silent-failure hunting** — the July 2026 review turned up four checks that were failing silently and reporting fake pass results; the canary suite in `canary.py` exists so that class of bug can't come back.
- **Version stamping** — every record carries `schema_version`, `PIPELINE_VERSION`, `DETECTION_ALGORITHM_VERSION`, and prompt version hashes, so records from before and after a change can be re-scored coherently.

## Status (July 2026)

- **Live loop:** running as `com.kellison.ligo-loop`, one experiment per hour.
- **Injection campaign:** live (interleaved with the survey), pool generation in progress toward 300 specs.
- **Pre-registration:** locked, pushed publicly (see commit `f335294003eb`).
- **Results:** will not be reported until all 300 campaign runs complete. Blinding is enforced.

## Data attribution

This project uses public strain data from the [Gravitational Wave Open Science Center](https://gwosc.org) (GWOSC), a service of the LIGO Scientific Collaboration, the Virgo Collaboration, and KAGRA. LIGO is funded by the U.S. National Science Foundation and operated by Caltech and MIT. Virgo is funded by CNRS, INFN, and other European agencies and operated by the European Gravitational Observatory. KAGRA is hosted by the Institute for Cosmic Ray Research at the University of Tokyo.

## License

No license has been granted yet. The source is publicly viewable, but reuse, modification, and redistribution are not permitted until a LICENSE file is added.
