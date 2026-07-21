# LIGO Pipeline — Implementation Plan (approved in phases July 8, 2026)

> **CURRENT STATUS (2026-07-21), appended — historical phase notes below are left as-written.**
> W3 and the injection campaign shipped as described below. After 76 records, the campaign was paused for §7.5d: a blind finiteness audit found 52 invalid original pool specs caused by non-finite GWOSC samples. The repair preserves all original artifacts/records, excludes all 52 under an outcome-independent rule, generates 52 separately sealed replacements with the original assignments/parameters, and resumes only after numerical audits pass. The final target remains 300 valid scored runs. Still not done: O4 cutoff raise, B3 PBH fixes, F10 benchmark matrix, F11, Gravity Spy, dashboards, errata backfill for pre-campaign records.

Companion to `REVIEW_FINDINGS.md` (the review) — this is the fix plan. Fixes are grouped F1–F12. Phase-1 batch (**F1, F2, F3, F4, F5, F9**) approved for implementation July 8, 2026. Phase-2 batch (**F6=W1, F7=W2, F8=W5, IceCube population**) approved and implemented July 10, 2026.

## STATUS: Phase-1 DEPLOYED (July 9) · Phase-2 DEPLOYED (July 10)
Phase-1 (F1-F5, F9) went live with the July 9 service restart; records since then carry `schema_version: 2`. Phase-2 (W1 energy_contrast rename + discriminator confidence, W2 time-of-flight coincidence, W5 full GWTC runtime catalog, IceCube catalog populated with 217 GCN/AMON alerts, reporter mean→median) went live with the July 10 service restart; records since then carry `schema_version: 3` — see the STATUS UPDATE block at the top of `REVIEW_FINDINGS.md` for validation results. Still NOT approved/done: O4 cutoff raise, B3 PBH fixes, F10 benchmark matrix, F11, F12, injections, Gravity Spy, dashboards, errata backfill for old records.

## Approved phase-1 batch

- **F1 — loop self-stop fix.** `loop.py run_loop`: on `select_next_target` failure, fall back to `_fetch_random_o3_windows(limit=10)` instead of `break` (clean exit → launchd `KeepAlive SuccessfulExit=false` won't restart → silent death every ~2 days). `planner.py select_next_target`: add JSON error handling.
- **F2 — three-state check status + schema version.** `crossmatch.py`: add `status` field (`ok`/`failed`/`skipped`) to Fermi/DQ/SNEWS/IceCube dataclasses. `loop.py`: add `schema_version: 2` to records. `planner.py` prompt: only treat `alert_found` as evidence when status==ok.
- **F3 — data-quality fix, SEMANTICS TESTED FIRST.** VERIFIED July 8: at GW150914 (clean data) the CBC_CAT1/CAT2 segments are PRESENT and cover the event → **segment-present = data PASSES = GOOD**. The current code assumes present = problem → **fully inverted**. Fix: int GPS bounds (float caused HTTP 400), ±16 s window, correct direction (`data_usable = has_data AND passes_cat1`; `cat1_active`=problem=`has_data AND NOT passes_cat1`; `cat2_active`=`has_data AND NOT passes_cat2`), per-flag errors recorded not swallowed, three-state status.
- **F4 — SNEWS/IceCube historical catalogs.** Replace dead live `gcn.nasa.gov/api/v0/notices` (404) with a bundled local `alert_catalogs.json` looked up by time window. Archival analysis wants archival facts. SNEWS legitimately empty (no alert since 1987). IceCube seeded with verified alerts incl. IC-170922A (GPS 1190148888.43) for the canary; completeness documented; times beyond catalog coverage return status=`skipped`, not a fake negative.
- **F5 — canaries + check_health.** New `canary.py`: Fermi→GW170817 GRB (~2 s), IceCube→IC-170922A (found), DQ→GW150914 (queries succeed), catalogs load with known entries. New `check_health` MCP tool in `server.py`. Auto-run at loop startup, logged, non-fatal.
- **F9 — provenance/model/version.** `planner.py`: hoist model IDs to constants (keep existing `claude-sonnet-4-6` — model CHOICE is out of scope), `temperature=0` for repeatability, clamp `interesting_score` to [0,1], keep raw LLM text on parse failure. `loop.py`: `versions` block gains model IDs + temperature; new `target_provenance` sub-dict (gracedb_id/labels/far/selection reason); `wall_seconds`.

## NOT in this batch (do not implement yet)
F6 (SNR→energy_contrast rename + retire confidence_score), F7 (Δt time-of-flight coincidence), F8 (full GWTC catalog at runtime), F10 (benchmark matrix + drift), F11 (small bundle), F12 (32 s fetch window). Also deferred: O4 cutoff raise, PBH-mode fixes, injections, Gravity Spy, get_plot/query_experiments tools, errata backfill (needs separate approval).

## F12 note (explained, NOT implemented)
Fetch 32 s, analyze central 4 s → stable noise baseline → trustworthy loudness. Changes every metric's scale (ruler changes) → discontinuity with the 39 existing records. Recommendation: bundle with F6's version bump so the dataset has ONE clean algorithm-v2 boundary, not two. Wait; do it with the rename.

## Dataset rule
JSONL is append-only. Existing 39 records: never edited. SNEWS/IceCube/DQ columns were untested (checks silently failed) but their default values happened to be correct for clean data; documented, recomputable via errata LATER (needs separate approval). `snr` field = energy contrast (numerically fine, misnamed); `confidence_score` = rescaled loudness (not a probability, deprecated). schema_version distinguishes v1 (existing) from v2 (new).

## Testing
New `tests/` (pytest) for pure logic; the semantics probe (done); canary/replay suite (IceCube test fails-before/passes-after = the bug-fix proof); golden-benchmark regression on GW150914 (still detects) + GW170817 (still fails coincidence — the L1 glitch). No full experiments written to the live dataset during testing (service is running concurrently).

## Order
F1 → F2 → F3 → F4 → F5 → F9. One service restart at the end (loop keeps running old code until restarted). Save goldens before, verify after.
