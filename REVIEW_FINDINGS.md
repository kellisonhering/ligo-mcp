# LIGO Pipeline — Full Review Findings & Action Plan (July 8, 2026)

> **STATUS UPDATE — July 10, 2026 (second fix batch, all validated end-to-end):**
> - **W1 FIXED** — `snr` renamed to `energy_contrast` everywhere (schema, prompts, reports, plots); `confidence_score` rebuilt from discriminators (chirp, coincidence, DQ) with loudness deliberately excluded. The scattered-light glitch at GPS 1258332306.3 that scored confidence 1.0 under the old code now scores **0.15**; GW150914 scores 0.80. DETECTION_ALGORITHM_VERSION 2.0.0, PLANNER_PROMPT_VERSION 1.2.0, **schema_version 3** — confidence is NOT comparable across the v2→v3 boundary.
> - **W2 FIXED** — coincidence now requires peak arrival times within light travel between sites (15 ms H1↔L1, 40 ms to V1/K1, incl. tile slack). Validation: GW150914 H1/L1 Δt = 4 ms → passes; the 1258332306.3 glitch pair Δt = 220 ms → correctly rejected. New per-pair fields: `dt_peak_s`, `dt_agreement`.
> - **W5 FIXED** — catalog.py now fetches all confident GWTC events from the GWOSC allevents API at runtime (393 events vs the hardcoded 11), cached monthly at `~/experiment-data/ligo/gwtc_catalog_cache.json`, hardcoded list kept as offline fallback. GW190425 (the review's example miss) now matches as a known BNS.
> - **IceCube catalog POPULATED** — 217 alerts from the GCN/AMON notice archives (EHE+HESE 2016-2019, Gold/Bronze 2019-present, highest revision per event). 'No alert' within coverage is now an authoritative realtime-program negative. Caveats live in the catalog's `completeness` field.
> - **§6 reporter mean→median also fixed** in passing (one extreme value no longer poisons the daily summary).
> - Earlier batch (July 8-9) had already fixed §1 (three dead checks + three-state status + canaries), B2 (loop self-stop), and the §4 provenance gaps.
> - **Still open:** O4 cutoff raise (§5 — awaiting Kellison's explicit go-ahead), B3 PBH-mode fixes (blocked on O4c anyway), W3 (32s noise window), W4 (chirp detector granularity), §7 injections, remaining §6 minor items (run_batch duplication, analyze_target timeout risk, report coincidence section, vision score_modifier advisory-only).

**What this is:** the complete record of a deep panel-style review (Fable 5, high thinking) of the LIGO MCP pipeline, run at Kellison's request against ChatGPT's review prompt. Every claim marked **[VERIFIED]** was empirically tested on July 8, 2026 — not inferred. This file is the build source for the next round of LIGO work: a future session should read this file top to bottom, re-run the canary tests in §2 to confirm current state, then execute §9 in order.

**Review ground rules that produced this:** read-only inspection of all 8 source files + the 39-record dataset at `~/experiment-data/ligo/experiments.jsonl` + live canary tests. Nothing was modified. No fixes have been applied as of this writing — every bug below is still live.

**Dataset state at review time:** 39 records (July 6–8), decisions: 30 glitch_candidate, 6 benchmark_validated, 3 archive. Loop healthy: launchd service `com.kellison.ligo-loop` running (PID 80042, never exited), records landing hourly, zero iCloud deadlock errors since the July 7 move to `~/experiment-data/ligo/`.

---

## §1 — THE HEADLINE FINDING: 3 of 4 external checks are silent no-ops [VERIFIED]

The crossmatch layer (SNEWS, IceCube, data-quality, Fermi) writes `checked: true` into every record — but **only Fermi actually works**. The other three fail silently on every single experiment and have never once performed a real check. All 39 records' SNEWS/IceCube/DQ columns are fiction dressed as negative results.

**Canary test results (July 8, 2026):**

| Check | Canary (known-positive replay) | Result | Root cause |
|---|---|---|---|
| **Fermi** | GW170817 (GPS 1187008882.4) must find GRB bn170817529 | ✅ **PASS** — found bn170817529, offset 2.07 s, t90 2.05 s | works correctly |
| **SNEWS** | raw endpoint probe | ❌ **HTTP 404** (HTML page — endpoint doesn't exist) | `crossmatch.py:260` queries `gcn.nasa.gov/api/v0/notices` which does not exist; the `status_code != 200` branch (`crossmatch.py:272-281`) returns `checked=True` with the canned note "No SNEWS alerts exist for the LIGO observing era" — this exact note appears in every record, proving the failure path fires 100% of the time |
| **IceCube** | IC-170922A (2017-09-22 20:54:30 UTC — most famous neutrino alert ever) must be found | ❌ **found=False, no error** | same dead endpoint + wrong schema names (`crossmatch.py:358`: "nu.icecube.HESE"/"nu.icecube.EHE") — can structurally never fire |
| **Data quality** | GW150914 (GPS 1126259462.4) — flags must at least query successfully | ❌ **all queries HTTP 400**, swallowed by `except: continue` (`crossmatch.py:207`) | **precise root cause found:** `check_data_quality` passes FLOAT bounds — `get_segments(flag, gps_time - 2.0, gps_time + 2.0)` (`crossmatch.py:190-191`). Even `H1_DATA` (a definitely-valid timeline) 400s with float bounds, while `loop.py`'s working call passes int constants over the full O3 range and succeeds. Every record's `dq_data_usable: true` is untested boilerplate |

**Canary script** (was in session scratchpad — recreate from this):
```python
import sys; sys.path.insert(0, "/Users/kellisonjames/Desktop/ligo-mcp")
from crossmatch import check_fermi_gbm, check_data_quality, check_icecube_gcn
from astropy.time import Time
r = check_fermi_gbm(1187008882.4)   # expect bn170817529 at ~2s
print("FERMI:", r.trigger_found, r.trigger_name, r.trigger_time_offset_s)
ic = float(Time("2017-09-22 20:54:30", format="iso", scale="utc").gps)
r = check_icecube_gcn(ic)           # expect found=True; currently False = broken
print("ICECUBE:", r.alert_found, r.error)
r = check_data_quality(1126259462.4, "H1")  # flags=[] with no error = silently broken
print("DQ:", r.flags_found, r.error)
from gwosc.timeline import get_segments
try: print("H1_DATA float bounds:", get_segments("H1_DATA", 1126259460.4, 1126259464.4))
except Exception as e: print("H1_DATA float bounds FAILS:", e)   # ← the DQ root cause
```

**Fix directions (not yet applied):**
- DQ: pass **int** bounds to `get_segments` (widen to e.g. `int(gps)-16 … int(gps)+16`); verify flag-name semantics against GWOSC docs (CAT segments may mark data *passing* vetoes, not flags *active* — verify direction before trusting cat1_active logic); record per-flag query errors instead of `continue`.
- SNEWS + IceCube: find the current GCN notices API (endpoint moved/changed; check gcn.nasa.gov docs + `gcn-kafka` archive access); correct schema names.
- **Schema change (applies to all four):** replace boolean `checked` with three states: `checked_ok` / `check_failed` (+ HTTP status/error) / `not_checked`. "The check is broken" must be distinguishable from "checked and quiet" — this is the whole point of an honest negative-results dataset.
- **Permanent canaries:** wire §1's replay tests into a weekly (or per-restart) health check + a `check_health` MCP tool. Pattern name: *canary tests for scientific senses* — every external integration must be provably able to fire against a historical known-positive. Portable to the exoplanet pipeline too.

---

## §2 — SECOND SEVERITY TIER (both still live)

### B2 — The survey loop can silently, permanently stop itself
`loop.py:347-359`: when the target queue empties (~every 50 experiments ≈ 2 days), the loop calls `select_next_target()` (LLM). That function (`planner.py:261-288`) has **no JSON error handling** (unlike the other two LLM calls). Any exception — rate limit, API overload, malformed JSON — propagates to `run_loop`'s handler which prints "Failed to get next target from LLM: … Stopping." and **`break`s → process exits 0 → launchd `KeepAlive SuccessfulExit=false` sees a clean exit and does NOT restart.** The service dies quietly; the only symptom is no new records.
- **How to notice:** `loop.out.log` ends with "Stopping."; `loop_status` shows stale `last_experiment_created_at`.
- **Fix direction:** on select failure, fall back to `_fetch_random_o3_windows()` instead of breaking; and/or make the plist KeepAlive unconditional; add JSON/backoff handling to `select_next_target`.

### B3 — Sub-solar (PBH) mode latent bugs — MUST fix before O4c ever starts it
1. `FRANGE_SUBSOLAR=(20, 4096)` (`pipeline.py:28`) but `fetch_open_data()` (`pipeline.py:96`) defaults to 4 kHz data → Nyquist 2048 → gwpy silently clamps to ~1291 Hz. **PBH mode cannot see its own target band (500–2000+ Hz).** Fix: pass `sample_rate=16384` when `subsolar_mode=True`.
2. `_fetch_subsolar_candidates` (`loop.py:463-542`) **lacks the `PUBLIC_DATA_GPS_CUTOFF` filter** that `_fetch_gracedb_triggers` has (`loop.py:584`) — it would burn cycles/API money on non-public O4 events.
3. S251112cm GPS is approximate ±12 h (`catalog.py:32-44`, `SUBSOLAR_MATCH_WINDOW_S=43200`): the "benchmark" analyzes noise half a day from the real event, and ANY window within 12 h of Nov 12 2025 noon gets `is_pbh_candidate=true`, which the planner prompt treats as "highest-priority" → score inflation on random noise. Fix: get the real GPS from GWOSC when O4c drops; shrink the window; until then treat S251112cm records as unusable.
4. `run_pbh_loop`'s exception handler lacks the `traceback.print_exc()` its sibling has.
5. The PBH loop runs as a **thread inside the MCP server** (`server.py:264-284`) — the exact fragility the survey loop was moved to launchd to escape. If ever started for real, give it the same launchd treatment.
(Existing memory `project-ligo-pbh-waiting-o4` already says don't start before O4c ~Dec 2026; these are the additional preconditions.)

---

## §3 — Scientific weaknesses (W1–W5, with observed evidence)

- **W1 — "SNR" is not SNR.** `pipeline.py:116`: `snr = max(Q energy)/median(Q energy)` — a peak-to-median tile-energy contrast, NOT matched-filter SNR. Observed absurdities in the dataset: L1 at GW170817 logged `second_snr: 216103.87` (it's the famous L1 glitch), `virgo_snr: 311.06` (real Virgo SNR there was ~2). Downstream: `confidence_score=(snr-3)/15` capped at 1.0 (`pipeline.py:124`) **saturates for anything loud** — a scattered-light glitch (record be47fc: snr 27.05) and GW150914 both get confidence 1.0. *Direction:* rename to `energy_contrast`/`qpeak_ratio` everywhere (schema, prompts, reports); rebuild confidence from discriminators (coincidence, chirp behavior, DQ), not loudness. Loudness ≠ realness.
- **W2 — Coincidence lacks the time-of-flight test.** Real GWs hit H1/L1 within ~10 ms. Current test = "loud tile somewhere in the same 4 s window at ±30% frequency" (`pipeline.py:21, 228-230`). `peak_time_offset_s` is already computed per detector — just never compared. Add `|Δt_peak| ≤ ~15 ms` (tile-resolution tolerance). Highest-value cheap physics upgrade.
- **W3 — 4 s window is both data and noise estimate.** `fetch_open_data(start,end)` fetches exactly the analyzed 4 s → whitening/PSD from 4 s of data → unstable normalization (part of why contrast swings 8→216k). Standard practice: fetch ~32 s, `outseg` the central 4 s. Related: `FRANGE=(20,2048)` exceeds 4 kHz data's usable band → gwpy warns "resetting to 1291 Hz" EVERY run, flooding `loop.err.log` and burying real errors. Cap frange or fetch 16 kHz.
- **W4 — Chirp detector is two points.** `pipeline.py:139-144` compares argmax frequency of window halves. GW151226 (long quiet chirp) fails it. Direction: ≥8 time slices + monotonic-rise score.
- **W5 — Catalog knows 10 of ~90 GWTC events** (`catalog.py:6-17`). A random O3 window landing on e.g. GW190425 (second BNS ever) would be filed as an unknown coincident candidate. Direction: fetch the full event list at runtime via the `gwosc` package (`gwosc.datasets.find_datasets` / event API) with the hardcoded 10 as offline fallback.

---

## §4 — Provenance & reproducibility gaps (B4) [dataset-value blockers]

- **Model ID not recorded**: planner + vision hardcode `claude-sonnet-4-6` (`planner.py:154, 219, 266`) but the record's `versions` block (`loop.py:307-312`) tracks only prompt versions. Add `model_id` (and temperature) per LLM call to versions.
- **Temperature neither pinned nor logged** — same input can yield different verdicts; either set temperature=0 for triage or log it.
- **GraceDB provenance dropped at the door**: targets carry `gracedb_id`, `gracedb_labels`, `gracedb_far` (`loop.py:588-594`) but `run_experiment()` never writes them into the record — only `mode` survives. When O4 unlock makes GraceDB targeting real, you can't say WHY a GPS was analyzed. Pass the target dict through into the record.
- `interesting_score` unclamped (`planner.py:235`) — LLM could return 1.5; clamp to [0,1].
- Raw LLM text discarded on JSON parse failure (`planner.py:241-248`) — store it for post-mortems.
- Missing per-record: wall-clock duration, cost estimate, gwpy/python versions, schema_version.

## §5 — Confirmed-inert feature: GraceDB candidate mode [VERIFIED]

`loop.out.log` shows at every startup: "Fetching public GraceDB superevents..." followed immediately by "Fetched 50 survey windows from O3 science segments" with NO "GraceDB: X retracted..." line → the 200 most recent superevents are all post-cutoff O4 events, all skipped (`PUBLIC_DATA_GPS_CUTOFF = O3_END`, `loop.py:72`). **The project's most novel targeting idea — re-analyzing RETRACTED candidates — has never actually run.** Raising the cutoff to end of O4b (data now public on GWOSC as of today — O4a since Aug 2025, O4b covering Apr 2024–Jan 2025; O4c expected ~Dec 2026) un-inerts it AND opens ~1.5 years of new data. **One-constant change with the highest value-per-line in the codebase — pending Kellison's go-ahead.** Do the B3 fixes in the same pass.

## §6 — Minor bugs (B6)

- `server.py:230` `run_batch`: `queue[i % len(queue)]` never pops → duplicate targets when count > queue.
- `server.py:65-71` `analyze_target`: synchronous multi-minute pipeline inside an MCP request → gateway timeout risk for Eve.
- `reporter.py:107-113`: daily summary uses MEAN of the energy ratio → one 216k value poisons it; use median.
- Candidate report (`reporter.py`) omits coincidence + crossmatch sections and doesn't embed the plot image.
- Vision `score_modifier` (`planner.py:106-111`) is advisory prose to the LLM, not numerically applied — apply it or rename it.

---

## §7 — Benchmarking upgrades (recommendations)

- **Matrix, not list**: loud BBH (GW150914 ✓), quiet BBH (GW151226 ✓), BNS+glitch (GW170817 ✓), NSBH (GW190814 — in catalog.py, unused), known loud glitch (Gravity Spy times), certified quiet data, detector-off time, + graded synthetic injections.
- **Mechanical pass/fail** the LLM doesn't control: `catalog_matched AND signal_detected AND (Δt-coincident where applicable) AND decision==benchmark_validated`, logged as boolean.
- **Drift automation is free**: GW170817 already ran twice → 0.82 both times (good stability). Automate: same GPS + same version stamps ⇒ metrics must match within tolerance; alert on drift. The versions block exists for exactly this and nothing consumes it yet.
- **INJECTIONS = the single strongest upgrade (panel unanimous):** add scaled CBC waveforms into real noise (gwpy/pycbc, in-memory) → unlimited parameterized ground truth → detection-efficiency vs distance curves, LLM-confidence calibration against known truth, blind challenges (don't tell the planner which windows carry injections). This is the LIGO twin of the exoplanet project's synthetic-ground-truth spine.
- **Already-measured psychometric curve (SURPRISE FINDING #1):** interesting_score on known-real events spans 0.35–1.0 — GW150914 1.0, GW170817 0.82, GW190521 0.55, GW151226 0.55, GW170814 0.35. That spread IS the pipeline's sensitivity fingerprint measured on ground truth, produced accidentally by routine operation. Plot score vs event loudness/type — it's the most honest figure the project owns.

### §7.1 — INJECTION ENGINE: detailed build plan (drafted July 10, 2026 — APPROVED IN PRINCIPLE, not yet built)

**Search keywords for future sessions:** injection, injections, synthetic signal, fake chirp, ground truth, detection efficiency, blind challenge, contamination, data leakage, pycbc, CBC waveform, calibration curve, false alarm rate.

**Why this exists (the motivating question):** Kellison correctly identified the contamination problem — the planner is HANDED the event name + GPS time for known events (verified July 10: `experiment_summary` in loop.py passes `known_event_match`, `event_name`, `event_type`, `gps_time`; the system prompt says "if known_event_match is true, treat as a benchmark"). So every high score on a named benchmark is answer-key leakage and proves nothing about detection skill. The ONE non-contaminated signal already in the data: the LLM scoring a *known-real* event LOW (GW170814 = 0.35) because the spectrogram showed a scattered-light glitch — i.e. it contradicted its own memory based on the data. Injections remove the contamination problem entirely: the ground truth is invented at runtime, exists in no catalog, and no model was trained on it. This is the LIGO twin of the exoplanet synthetic-spectrum spine — both become "when the AI says 0.8, is it right 80% of the time?" calibration studies.

**Core mechanism:** fetch quiet REAL detector noise → generate a CBC chirp from physics → add signal into noise (both detectors, correct light-travel Δt + antenna response) → run the normal pipeline on the sum → the planner is NOT told it's injected → because we built the signal we know the truth exactly → measure whether the pipeline caught it.

**Generator ≠ analyzer (no circularity):** pycbc generates the waveform via full relativity; the pipeline detects with a generic Q-transform that knows nothing about the specific waveform. The injection also genuinely exercises the W2 time-of-flight coincidence test built July 10.

**Five phases (each independently testable — stop and verify after each):**

- **Phase A — generate one waveform (standalone, `injections.py`, no pipeline changes).** Use `pycbc.waveform.get_td_waveform` (approximant e.g. `IMRPhenomD` or `SEOBNRv4_opt`), parameterized by mass1/mass2 (+ optional spins). Test: plot one, confirm upward chirp. Compute optimal/expected SNR against a noise PSD via `pycbc.filter.sigma` (this is the honest "true detectability" label). *Friction: pycbc is the one heavy new dependency in the whole plan — everything else is light.*
- **Phase B — inject into real noise (standalone).** Fetch a quiet window; project the waveform onto H1 and L1 with correct time delay + antenna pattern (`pycbc.detector.Detector.project_wave` or `time_delay_from_earth_center`); add to the strain; make spectrograms at a LOUD injection (chirp clearly visible) and a FAINT one (buried). gwpy↔pycbc conversion via `.to_pycbc()` / `TimeSeries.from_pycbc`; hand the summed strain back to the existing `data.q_transform`. Deliverable: contaminated data stream with the signal visibly hiding.
- **Phase C — wire into pipeline, BLIND (the careful part).** Add an injection path to `run_pipeline`/`run_experiment`. **HARD RULE: injection metadata (masses, distance, expected SNR, geocent merger time, Δt applied, seed) goes into the RECORD for later analysis but is NEVER placed in the `experiment_summary` dict the planner sees.** The AI gets spectrogram + metrics only — identical to a normal run. New record field `injection: {...}`; bump to schema_version 4.
- **Phase D — injection campaign + blinding.** New loop mode mixing injections into survey runs at a set rate (~20-30%), randomized parameters spanning obvious→invisible SNR. Store truth in a SEPARATE file not read until scoring is done (anti-self-deception). KEEP matched pure-noise runs (no injection) as the false-alarm control. Deliberate negative control: single-detector injections that the W2 test SHOULD reject.
- **Phase E — analysis (`analyze_injections.py`).** Three payoff figures: (1) detection-efficiency curve = caught-fraction vs injected SNR (standard GW sensitivity plot; smooth ramp with a crossover in the marginal zone — the money region); (2) false-alarm rate from noise-only runs; (3) calibration curve = LLM confidence vs actual correctness.

**Decisions to lock before running (pre-registration):** SNR span (must cover clearly-visible ~20+ down to invisible ~4 so the efficiency curve has slope; the marginal zone is the science); waveform approximant; injection fraction; fresh-noise-per-run vs reuse (fresh = cleaner, reuse = measures run-to-run variance — maybe some of both); random seed policy for reproducibility.

**Schema additions (Phase C):** per record `injection: {injected: bool, approximant, mass1, mass2, distance_mpc, geocent_end_time, network_optimal_snr, per_detector_expected_snr: {H1,L1}, time_delay_applied_s, seed}`. Mirror the exoplanet JSONL discipline. `injected: false` on the pure-noise controls (so the control set is queryable, not just absent).

**Caveats:** pycbc install is the real cost; this is a genuine subproject (~5 phases) not a one-day fix; but it is THE upgrade that moves the project from "recognizes famous events it may have memorized" to "detects gravitational waves" — defensible against the contamination critique that otherwise sinks the whole benchmark.

### §7.2 — PREWORK DONE (Opus 4.8, July 10, 2026) + OPEN QUESTIONS FOR FABLE

Kellison wants Fable to do the actual build in a future usage window AND to give its opinion on the whole design (not just execute). This prework clears mechanical friction so Fable spends its window on judgment. **No injection code was written** — deliberately, so Fable's approach isn't pre-biased.

**Environment findings (verified July 10, 2026):**
- Python is **3.14.2** at `/Library/Frameworks/Python.framework/Versions/3.14`. Installed: gwpy 4.0.1, numpy 2.4.4, scipy 1.17.1, astropy 8.0.1, matplotlib 3.11.0. pycbc/lalsuite NOT installed.
- **pycbc INSTALLS on 3.14** — `pip install --dry-run pycbc` resolves cleanly to PyCBC 2.11.0 + lalsuite 7.26.15 + Cython/Mako/etc. (Dry run only — the actual lalsuite compile was NOT performed; confirm with a real install as step zero.)
- **⚠️ pycbc wants to DOWNGRADE numpy 2.4.4→2.3.5 and scipy 1.17.1→1.16.3.** The running launchd loop uses the system numpy/scipy — a system-wide pycbc install could perturb or break the live pipeline on its next hourly run. **Install pycbc in a dedicated venv, NOT system-wide.** Decide whether injection generation runs as a separate venv step feeding strain arrays to the main pipeline, or whether the whole pipeline moves into that venv.
- **gwpy↔pycbc bridge confirmed present:** `TimeSeries.to_pycbc`, `.from_pycbc`, and `.inject` all exist on gwpy 4.0.1 — the injection add-in has native support, no manual array alignment needed.

**Verified wiring points (line numbers as of July 10, 2026 — reconfirm, code may have moved):**
- `pipeline.py:100` `def run_pipeline(...)` — add an `injection` parameter here.
- `pipeline.py:116` primary-detector `fetch_open_data` — inject the signal into the returned strain right after this.
- `pipeline.py:217` **second `fetch_open_data`, inside the coincidence check** — ⚠️ **THE non-obvious gotcha:** strain is fetched TWICE (primary detector + coincidence detector). A proper injection must go into BOTH fetches with the correct per-detector time delay + antenna projection, or the signal lands only in H1, fails the W2 coincidence/Δt test, and looks like a glitch. Cleanest design: compute an "injection set" once (waveform projected to each detector with its delay), then apply the matching projection wherever a detector's data is fetched. Single-detector injection = a deliberate negative control, not the default.
- `loop.py:107` `def run_experiment(...)` — thread an injection spec through here.
- `loop.py:165` `experiment_summary = {...}` — **THE BLINDING BOUNDARY. Injection truth must NEVER enter this dict** (it's what the planner LLM sees). Injection facts go only into the record's own `injection` field for later analysis.
- `loop.py:281` record `"detection"` block / `loop.py:343` `schema_version` — add the `injection` field near here, bump schema_version 3→4.

**OPEN QUESTIONS FOR FABLE (the opinion Kellison wants — critique freely, don't just build):**
1. **Is the experiment sound overall?** Critique the contamination fix, the blinding, the controls. What's missing or weak? What would a GW-detection reviewer attack?
2. **Detection-statistic mismatch — possibly the deepest issue:** the pipeline detects via Q-transform peak/median **energy_contrast**, NOT matched filtering. Real GW pipelines recover faint signals with matched filters that this contrast statistic will miss. Does that make faint-injection recovery meaningless — or is "cheap Q-transform triage misses what matched filter catches, here's exactly where the cliff is" actually a MORE interesting result? Should Phase E add a pycbc matched-filter recovery stat as a second yardstick to quantify that gap?
3. **Waveform source for the first pass:** full pycbc/lalsuite (defensible, citable, but the venv + numpy-downgrade cost) vs a numpy-only analytic post-Newtonian chirp (zero new deps, faster to stand up, less rigorous). Start analytic and upgrade, or go straight to pycbc?
4. **SNR span + sampling** for the detection-efficiency curve — what range (invisible ~4 → obvious ~20+?) and spacing gives a clean curve without burning API budget on redundant loud injections? Overweight the marginal zone like the exoplanet plan?
5. **Injection fraction + blinding mechanics** — is ~20-30% right? Truth stored in a separate reveal-after file — overkill or correct?
6. **Anything better we haven't thought of** — Kellison explicitly wants Fable to suggest improvements to the whole direction, not just answer 1-5.

### §7.3 — FABLE 5 DESIGN REVIEW (July 10, 2026 — review only, nothing built)

**Verdict: sound, build it — with the amendments below.** Strong structural point in the plan's favor: the planner is STATELESS (fresh context, temperature 0, no cross-run memory), so blinding is enforced by architecture, not discipline — it cannot learn the injection fraction even in principle.

**A1 — MOST IMPORTANT AMENDMENT (gap in §7.1): pre-register the detection claim.** The trigger (`signal_detected`) fires on nearly every window (all 73 records show loud junk clearing CONTRAST_THRESHOLD), so it is NOT a usable detection statistic. Before any scored campaign, freeze the rule for "injection caught" — candidates: `confidence_score ≥ 0.45`, `coincident AND dt_agreement`, or `LLM decision ∈ {follow_up, candidate_for_human_review}` — and report curves at several frozen thresholds. Choosing the rule after seeing results = the exact self-deception this project exists to prevent.

**A2 — answers to the six questions:**
1. Sound. Reviewer attack surface: (a) noise-selection bias — injection windows MUST be drawn by the same random process as survey windows (glitchy ones included), never curated-quiet; (b) the 4 s window (see A3); (c) the named-benchmark fingerprint table stays contaminated forever — demote it to "answer-key-given consistency check" in any writeup; injections carry ALL detection claims.
2. The mismatch is the PAPER, not a flaw. Reframe the curve as "sensitivity of cheap Q-transform+LLM triage," never "GW sensitivity." **DO add the matched-filter yardstick**: per injection, recover matched-filter SNR using the injected waveform as its own template (nearly free once pycbc generates it; label it an optimal-template upper bound). Money figure = cheap-stack efficiency curve vs the MF-optimal curve; the gap quantifies what cheap triage costs.
3. **Go straight to pycbc in the venv** (needed for the MF yardstick anyway; an analytic inspiral-only chirp has no merger blob, which unfair-tests the vision layer since morphology is part of the stack under test). numpy PN chirp = fallback only if the lalsuite install fails in practice.
4. **Window/mass constraint §7.1 missed: WINDOW_SECONDS=4 caps accessible masses.** A BNS (1.4+1.4) chirps ~30+ s above 20 Hz — cannot fit; would be truncated garbage. First campaign = **BBH only, total mass ~20–80 M☉** (whole chirp ≤ ~2 s). SNR span ~[4, 24], overweight 6–16 (the marginal zone), N ≥ 200 injections + ≥ 100 noise-only controls. Also plot injected SNR vs observed energy_contrast — the mapping is unknown and is itself an early deliverable.
5. Trickle-into-hourly-loop is the slow path (~7/day → ~6 weeks). **Prefer a dedicated batch campaign** (hundreds of back-to-back runs, ~2 cheap LLM calls each) alongside/instead of the ambient trickle. Separate truth file: keep — it protects against the human, the LLM can't peek by construction.
6. Additions adopted below.

**A3 — sequencing prerequisite:** W3 (fetch ~32 s, analyze central 4 s) must land BEFORE or WITH the campaign — expected-SNR truth labels need a real PSD estimate; 4 s of data can't provide one. Freeze all versions + pre-register bins/claim before the scored campaign (same preregistration discipline as the exoplanet protocol).

**A4 — three additions to the plan:**
- **Name-ablation A/B (do FIRST — needs no pycbc, no new deps, ~30 cheap LLM calls):** re-run the 5 famous benchmarks with `event_name`/`known_event_match`/catalog fields STRIPPED from the planner summary; compare scores with-name vs without-name. Directly quantifies the answer-key inflation that motivated the whole build. Perfect pre-window appetizer; can run anytime on approval.
- **Synthetic GW170817s:** inject loud signals into windows carrying a large single-detector glitch — recreates the famous near-veto on demand; tests whether the W2 dt test recovers a real signal through a glitch. Showcase case study (pairs with SURPRISE FINDING #2).
- **Physics-only baseline per injection:** log the deterministic-rules decision alongside the LLM's on every injection — answers "does the LLM add anything over rules?" on ground truth (mirrors the exoplanet shallow-baseline arm; §8 item 5 gets its data for free).

**Build order when the window opens:** name-ablation A/B → venv + pycbc install (real install, not dry-run) → Phase A/B → W3 → Phase C (+A1 claim freeze) → dedicated batch campaign (Phase D) → Phase E with both yardsticks.

### §7.4 — BUILD PROGRESS (July 10, 2026, Fable 5) — ablation DONE, Phases A/B DONE + VALIDATED

**Name-ablation A/B/C — RESULTS (script: `name_ablation.py`; data: `~/experiment-data/ligo/analysis/name_ablation_20260710.json`).** Design: rebuild each famous benchmark's planner summary from its stored record, run the CURRENT planner 3 ways at temperature 0 — A = full (name+GPS), B = catalog fields neutralized, C = B + GPS shifted +13,000,000 s + Fermi trigger name masked. Only the identifiers vary.

| Event | A (named) | B (no name) | C (no identifiers) | Verdict |
|---|---|---|---|---|
| GW150914 | 0.97 | 0.97 | **0.97 → candidate_for_human_review** | Zero inflation — data carries it; blind, it correctly escalates an "unknown" loud chirp to a human |
| GW170817 | 0.93 | 0.88 | **0.72 → follow_up** | 0.21 inflation, mostly via the famous Fermi trigger name; still detected blind |
| GW190521 | 0.55 | 0.35 | **0.35 → glitch_candidate** | 0.20 inflation — blind, the pipeline MISSES it (calls it a glitch) |
| GW151226 | 0.18 | 0.18 | 0.18 | No inflation because never detected, even named (long quiet chirp = W4 weakness) |
| GW170814 | 0.18 | 0.18 | 0.18 | Same — the scattered-light-contaminated case |

**Headline: blind detection scoreboard on the famous five = 2 detected (GW150914 strongly, GW170817 as follow-up), 3 missed.** Answer-key inflation is real but event-dependent (0.0–0.21). GW150914's zero-inflation + blind escalation is the pipeline's best honest result to date. NOTE: arm-A scores differ from stored historical scores (e.g. GW151226 0.55→0.18) because the prompt + confidence formula changed July 10 (v1.2.0/2.0.0) — version-stamped, expected.

**Phase A — DONE + VALIDATED** (`injections.py`, runs under `~/venvs/ligo-injections/bin/python`). pycbc 2.11.0 + lalsuite 7.26.15 installed in the venv cleanly; system numpy/scipy untouched (live loop unaffected). 30+30 M☉ IMRPhenomD waveform: real signal duration 1.35 s (fits the 4 s window — confirms the BBH-only mass rule), frequency thirds 19→22→46 Hz (accelerating upward chirp), textbook morphology confirmed visually (`~/experiment-data/ligo/injections/phase_a_waveform.png`). Gotcha logged: FD approximants pad the buffer to 16 s of mostly zeros — measure signal duration above 1% peak amplitude, not `.duration`.

**Phase B — DONE + VALIDATED** (same file, `--phase b`). Injected into real O3 noise (GPS 1243000000, 32 s window, W3-style): H1/L1 arrival-time difference **6.3 ms** (physical, passes the W2 tolerance — the projection includes real light-travel delays); measured-PSD SNR scaling exact (target 25 → achieved 25.0; target 6 → 6.0); **loud injection = clearly visible textbook chirp in the spectrogram; SNR-6 injection = invisible, and the brightest tile in that window is an unrelated noise blob** — direct preview of where the energy-contrast statistic will hit its efficiency cliff (the A2.2 matched-filter-gap thesis, visible in one image). Plots: `~/experiment-data/ligo/injections/phase_b_{H1,L1}_snr{25,6}.png`. GPS 1240000000 had no open data — the campaign's window picker needs a data-availability retry loop (already prototyped in `phase_b`).

**NEXT (needs go-ahead — touches the live pipeline):** W3 (32 s fetch, central 4 s analysis) → Phase C (injection param through `run_pipeline`/`run_experiment`, `injection` record field, schema_version 4, blinding boundary enforced) → A1 claim freeze + pre-registration (SNR bins, masses 20–80 M☉ total, N≥200+100, detection-claim rule) → Phase D batch campaign → Phase E analysis with both yardsticks.

### §7.5 — PRE-REGISTRATION: injection campaign grading rules
**STATUS: 🔒 LOCKED by Kellison on 2026-07-11. These rules do not change from this point forward; results are reported under them no matter how they look. Git commit of this file at lock time (`ligo-mcp` repo, commit `f335294003eb`, 2026-07-12T09:48:25-05:00) is the credential — the timestamp on that commit is what makes this pre-registration real. Any change to §7.5 after lock must appear as a NEW section (e.g. §7.5b — amendment), never as an in-place edit, and must be committed with a clear reason before any further scored runs.**

**PACING (Kellison's choice at lock time):** the campaign runs **interleaved with the regular survey loop at ~1 experiment per hour**, NOT as a dedicated batch. Rationale: spreads cost gradually (matches Kellison's budget pacing policy, [[project-pace-spending]]); no scientific downside (300 experiments is 300 experiments regardless of wall-clock rate); requires no new scheduler infrastructure. Approximate wall-clock horizon at 1/hour with ~50% injection fraction: ~25 days for 300 runs total.

**THE TEST:** hide synthetic gravitational-wave signals in real detector noise; measure how many the pipeline finds, how often it cries wolf on nothing, and whether its confidence numbers mean what they say.

**1. The roster (what runs):**
- 300 runs total, mixed into the ongoing hourly survey loop at ~50% injection rate (see PACING above).
- **200 injections** + **100 pure-noise controls** (no signal — the lie-detector set), shuffled randomly across the schedule.
- Campaign records go to a SEPARATE file (`~/experiment-data/ligo/campaign_2026_07.jsonl`), keeping the survey dataset clean and avoiding concurrent-write collisions with the live loop.
- Raw 32 s strain windows (H1+L1) saved per run (~600 MB total) — enables the matched-filter yardstick and any future reanalysis.

**2. The signals:**
- Binary black holes only: each mass drawn uniform 10–40 M☉ (total 20–80 — heavier signals don't fit the 4 s window; documented limitation).
- Approximant IMRPhenomD, zero spins (v1 simplification, stated openly). Random sky position, orientation, polarization.
- Strength = network optimal SNR against the MEASURED noise of the actual window (frozen x-axis definition): 20% drawn in 4–6 (invisible), 60% in 6–16 (the "maybe" zone — the science), 20% in 16–24 (obvious).
- Injected into BOTH detectors with correct light-travel delay + antenna response (validated Phase B: 6.3 ms).

**3. The noise windows:** drawn randomly from O3 science time by the same process the survey uses — NOT curated for quietness; glitchy windows stay in (realism). Only exclusion: ±64 s around any GWTC catalog event. No-data windows are redrawn.

**4. The grading rules (frozen):**
- **PRIMARY ("caught"):** `chirp_like == true` OR `coincident == true` — the pipeline saw the chirp shape, or both detectors agreed in frequency AND arrival time. Mechanical, computed by code, no AI judgment involved.
- **SECONDARY ("escalated"):** the LLM decision is `follow_up` or `candidate_for_human_review` — did the system actually wake a human? This grades the end-to-end triage behavior.
- Both rules applied identically to injections (→ efficiency) and noise-only controls (→ false-alarm rate).
- Supplementary: efficiency curves also shown at confidence thresholds 0.3 / 0.45 / 0.6 (labeled as supplementary, not primary).

**5. The report (produced no matter what it shows):**
- Detection-efficiency curve: fraction caught per SNR bin (2-wide bins, 4–24), with binomial CIs; **SNR50** = interpolated strength at 50% caught.
- False-alarm rate on the 100 controls (both rules), with CI.
- Calibration: LLM interesting_score on injected vs empty runs — does higher score actually mean more likely real?
- Matched-filter gap: post-hoc MF-recovered SNR per injection (injected waveform as its own template, labeled optimal-template upper bound) vs what the cheap stack caught.

**6. Pre-committed interpretations (so results can't be spun):**
- SNR50 ≤ 10 → cheap Q-transform+LLM triage is surprisingly competitive with matched filtering.
- SNR50 ≥ 15 → cheap triage misses most of the marginal zone; the measured gap IS the finding.
- False-alarm rate > 10% on the primary rule → the rule is too loose; report it and say so.
- Every outcome above is publishable. There is no failure outcome.

**7. Prerequisites before the first campaign run (all validated before freeze takes effect):**
- W3 lands (32 s fetch / central 4 s analysis) → DETECTION_ALGORITHM_VERSION 2.1.0, since PSD-accurate SNR labels depend on it.
- Phase C lands (injection pathway, `injection` record field carrying `{injected, spec_id}` only — full truth parameters live ONLY in `campaign_truth.jsonl`, not read until scoring; schema_version 4; the planner summary NEVER carries any injection field).
- Both validated by: one GW150914 benchmark run + one loud test injection + one noise control, checked end-to-end.
- Versions + prompts frozen at campaign start and stamped into every record (existing versions block).

**8. Physics-only baseline logged per run (locked into scope — panel-flagged as highest-value addition):** every campaign record ALSO carries the decision a deterministic-rule system would have made on the same summary (`baseline_decision`, `baseline_caught`) — no LLM in the loop. Grading rules 4 and 5 are applied identically to this arm. Yields the "does the LLM add anything over rules?" comparison as a free byproduct — the highest information-per-dollar experiment in the campaign per the July 2026 strategy review. Kellison confirmed 2026-07-11.

**Blinding statement:** the planner LLM is structurally blind (stateless, sees only the summary, which carries no injection information). The humans (Kellison + the AI assistant) agree not to compare outcomes against `campaign_truth.jsonl` until all 300 runs are complete.

### §7.5b — AMENDMENT (pacing only), 2026-07-16
**Change:** `SLEEP_BETWEEN_EXPERIMENTS` reduced from 3600 s (1/hour) to 1800 s (2/hour) in `loop.py`.
**Reason:** observed real cadence was ~1 run every 2.9 h (wall-clock time for multi-detector data fetch + two LLM calls stacks on top of the sleep), projecting ~4 months to complete 300 runs. Halving the sleep shortens the wall-clock horizon to roughly 2 months. Kellison requested the change on 2026-07-16.
**Scope of this amendment:** wall-clock pacing ONLY. This changes nothing about the grading rules (§7.5 rules 4–6), the signal population (§7.5 rule 2), the noise-window selection (§7.5 rule 3), blinding, or the physics-only baseline. Pacing was pre-declared scientifically neutral in the §7.5 PACING note ("no scientific downside; 300 experiments is 300 experiments regardless of wall-clock rate"), so this amendment does not affect any scored outcome. No campaign records already written are invalidated; runs before and after this change are graded identically.

### §7.5c — ERRATUM (implementation bugs, not rule changes), 2026-07-16
This section documents two places where the **code diverged from, or under-described, the locked §7.5 rules**. §7.5's rules themselves are unchanged and remain correct; the code and docs are being brought into conformance with them. Raised in an external code review on 2026-07-16.

**Erratum 1 — physics-only baseline used the wrong `caught` rule.**
`physics_only_decision()` in `loop.py` computed the baseline `caught` as
`chirp_like AND coincident AND dt_agreement AND !dq_cat1_active`, but §7.5 rule 4
locks the PRIMARY rule as `chirp_like == true OR coincident == true`. This was an
implementation bug (the function's own docstring wrongly claimed it matched rule 4).
- **Scope:** affects ONLY the stored convenience boolean `physics_baseline.caught`
  in campaign records written before this fix. It does **not** affect the primary
  injection-efficiency grading, which is computed at scoring time from raw fields,
  nor any raw measurement.
- **Remedy:** the raw discriminators (`chirp_like`, `coincident`) are stored in
  every record, so **final scoring recomputes `caught` uniformly for all 300
  records under the locked OR rule.** Earlier records therefore stay valid and the
  dataset stays append-only — nothing is edited or discarded.
- **Fix + versioning:** `physics_only_decision()` now computes
  `bool(chirp_like) or bool(coincident)`, verbatim §7.5 rule 4. A new
  `baseline_rule_version` field (1 = old buggy rule, 2 = locked OR rule) is stamped
  into each record's `physics_baseline` block so the write-time rule is
  self-documenting. `DETECTION_ALGORITHM_VERSION` is deliberately **not** bumped:
  signal processing is unchanged; only the deterministic baseline rule changed.

**Erratum 2 — the `injection.injected` field is misleadingly named.**
`injected` is set to `injection_spec_path is not None`, which is True for **every**
campaign run — including noise-only controls, whose pool spec carries a zero
injection array. So `injected` means "a campaign pool spec was applied," NOT "a
synthetic signal is present."
- **Not a blinding leak:** because it is uniformly True across both injections and
  controls, it does not distinguish them. It separates campaign runs from ordinary
  survey runs, nothing more.
- **Handling:** the field is **kept as-is, not renamed**, to avoid a schema change
  mid-campaign; old records are not rewritten. Its true meaning is documented here
  and in a `loop.py` comment. Whether a run truly contains a signal is determined
  ONLY by the sealed `campaign_truth.jsonl` at scoring time.

**Artifact sealing (related hardening, same date).** The truth set (`campaign_truth.jsonl`), `pool_index.json`, `pool_gen.log`, and all 300 `pool/*.npz` were set filesystem read-only and hashed into `campaign_seal/SHA256SUMS.txt` (committed, off-repo data itself not committed). Sealing occurred after 7 campaign records had been collected; disclosed in `campaign_seal/SEAL_NOTE.md`. This makes the §7.5 blinding verifiable rather than honor-system.

## §8 — Expansion ideas (ranked) + what's NOT worth it

1. Injection engine + blind challenges (§7).
2. **O4a/O4b cutoff raise** (§5) — one constant, 1.5 years of data, un-inerts retracted-candidate mode.
3. **Gravity Spy × Claude-vision confusion matrix** — run the vision layer on Gravity Spy-labeled glitch times, publish agreement matrix ("how well does a frontier VLM classify detector glitches zero-shot?"). Self-contained short-note material; LIGO twin of the exoplanet degeneracy matrix.
4. Retracted-candidate reanalysis as flagship question ("what does an independent cheap AI-triaged pipeline see at times LIGO retracted?").
5. **Physics-only baseline logged per experiment** — deterministic rule decision alongside the LLM's; publish disagreement rate + who was right vs benchmarks/injections. (Panel split on whether the LLM adds value over the rules already embedded in its prompt — this measures it. Same lesson as the exoplanet shallow-learner baseline.)
6. Multi-model disagreement triage (Haiku/Sonnet/Opus same summaries).
7. Matched-filter second pass (pycbc) on rare high-scorers only.
- **NOT worth it:** real-time O4 alert racing; training custom ML detectors; quad-coincidence marketing (KAGRA public data too sparse); building the SNEWS Kafka listener before the archive check even works (also: GCN credentials were never registered — confirmed July 8).

**SURPRISE FINDING #2 — the GW170817 case file:** the pipeline independently re-encountered the most famous DQ crisis in GW history — found the monstrous L1 artifact (contrast 216k) and declined coincidence, which is what nearly auto-vetoed GW170817 in the real 2017 control room. Written up as a case file, this is the project's best demo. Treat as showcase, not bug.

**SURPRISE FINDING #3 — the canary pattern is itself a contribution:** "every external sense a science pipeline has must be provably able to fire against a historical known-positive, on a schedule." It found the biggest bug in this codebase within an hour of being invented. Portable to every pipeline (exoplanet next). QA-engineering-applied-to-science — squarely on portfolio thesis.

## §9 — THE ACTION PLAN (execute in this order when work resumes)

1. **Fix §1's three dead checks + the three-state `checked` schema + permanent canaries.** Nothing else about the crossmatch columns can be trusted or released before this. (DQ = int bounds + verify flag semantics; SNEWS/IceCube = find current GCN API + correct schemas.)
2. **Fix B2 (loop self-stop)** — fallback targets instead of `break`; reconsider KeepAlive. It's a time bomb on an unattended service.
3. **W1 rename** (energy_contrast) + rebuild confidence from discriminators. Touches schema, prompts, reports — bump DETECTION_ALGORITHM_VERSION and PLANNER_PROMPT_VERSION.
4. **Decide + apply the O4 cutoff raise** (Kellison's call) together with all of B3's PBH fixes.
5. **W2 (Δt ≤ 15 ms coincidence) + W5 (full catalog at runtime).** Small, high-yield physics.
6. **Provenance pass (§4):** model_id + temperature in versions, GraceDB fields into records, clamp score, keep raw LLM text on failure.
7. Then and only then: **injections** (§7) → Gravity Spy study → physics-baseline logging → dataset-release prep.

**What NOT to build yet:** PBH loop (blocked: O4c ~Dec 2026 + B3 + real S251112cm GPS); SNEWS Kafka monitor (archive check first + GCN registration); HF/dataset release (blocked on step 1 + §4); matched filtering as default stage; dashboards before `get_plot`/`query_experiments` MCP tools exist.

**MCP tool wishlist (for the Agentics side):** `get_plot` (Eve currently cannot see any image), `query_experiments(decision=, min_score=, since=)`, `benchmark_report` (pass/fail + drift), `check_health` (canary results), `cost_report`. Eve notify rules: candidate_for_human_review, benchmark FAIL, loop stalled >2h, canary newly failing. Stay quiet on: glitches, archives, routine benchmark passes.

## §10 — Preprint framing (when mature)

Strongest artifact: labeled dataset + short methods note, NOT a discovery paper. Cleanest question: *"Can an LLM-triaged pipeline on open GW data match deterministic triage, measured against benchmarks and blind injections?"* Smallest credible study: benchmark matrix + ~200 injections + physics-baseline comparison + calibration curve. Honest publishable negative: "LLM triage adds nothing over threshold rules at this metric granularity." Reviewers will attack: the word "SNR", untested crossmatch columns, no injections, unpinned stochastic decisions — all addressed by §9. Avoid: "detection" without "known/injected"; any PBH claims pre-O4c.
