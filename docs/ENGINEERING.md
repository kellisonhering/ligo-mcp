# Engineering Notes

This project is an exercise in building and evaluating a Python system whose
outputs need more scrutiny than a successful API response. Claude Code and Codex
assisted implementation, review, debugging, tests, and documentation.

## System Boundaries

The numerical pipeline fetches strain, creates Q-transform spectrograms, measures
features, and checks detector coincidence. An image model describes a spectrogram;
a planner receives the numerical and image summaries and returns a triage choice.
The historical planner does not receive raw strain or injection labels.

Records are append-only JSONL with detector measurements, model settings,
software/prompt versions, and explicit errors. Scoring is a separate offline
operation that joins records to labels only after the campaign is opened.

| Component | Responsibility |
| --- | --- |
| `pipeline.py` | Q-transform, excess-power trigger, chirp heuristic, coincidence |
| `planner.py` | Image/planner/selector prompts, API calls, decision validation |
| `loop.py` | Scheduling, experiment assembly, provenance, persistence |
| `crossmatch.py`, `catalog.py`, `alert_catalogs.json` | External evidence and historical catalog lookups |
| `server.py`, `snews_monitor.py` | MCP tools and optional monitoring |
| `canary.py`, `reporter.py` | Integration checks and readable reports |
| `injections.py`, `injection_pool_generator.py` | Waveform generation, detector projection, prepared pool |
| `campaign_quality.py`, `tools/generate_campaign_replacements.py` | Numerical integrity and frozen replacements |
| `analyze_injections.py`, `campaign_scoring.py` | CLI boundary and pure grading/statistics |
| `campaign_matched_filter.py`, `scoring_report.py` | Physical recovery yardstick and report artifacts |
| `tools/audit_campaign.py`, `tools/export_campaign_summary.py` | Local audit and public aggregate export |
| `name_ablation.py`, `tools/inspect_campaign_misses.py` | Historical identifier experiment and exploratory diagnosis |
| `comparison_prep.py`, `fable_batch_prep.py` | Local preparation for a later controlled model comparison |

## Failures Must Be Visible

An external service that failed cannot supply a trustworthy negative result.
Historical fixes added `ok`, `failed`, and `skipped` status fields so missing
evidence can be distinguished from a successful check that found no alert.
Canary checks exercise integrations against known examples; these are distinct
from the offline unit tests and may require network access.

AI parse/API failures return a safe archival default and preserve error fields.
That protects the operational workflow, but a fallback zero is not a real AI
judgment. The scorer retains those defaults in full operational denominators and
also reports paired comparisons with unavailable calls removed from both arms.

## Preserve Raw Evidence, Recompute Derived Rules

An early baseline implementation used an AND rule where the locked rule required
OR. The raw discriminators had been saved, so scoring recomputes the same OR rule
for every record rather than rewriting the original dataset. The erratum remains
in the review history.

The source pool also contained 52 non-finite specs. The frozen integrity rule
excluded every affected original, independent of outcomes. Separate replacements
preserved the assignments and signal parameters. Sixteen previously attempted
invalid originals remain in the 316-line log; the final scoring roster has 300
active records. Original and replacement artifacts have separate manifests.

## Blinding and Integrity Have Different Limits

The planner input allowlist rejects truth-shaped fields. The answer key is held
separately, and readiness mode verifies its hash without parsing labels. The real
scoring command requires explicit unblinding and a matching source/input receipt.

Checksums detect changes to files; they cannot prove that nobody previously read
a file. Human blinding was a procedural agreement. Original sealing occurred
after seven campaign records and that timing is disclosed in the original seal
note. This distinction qualifies stronger wording in the preserved July notes.

Reports go into new directories. Checksums cover outputs, and the scorer writes
`COMPLETE.json` only after the report and all charts succeed. A failed generation
may leave a partial directory, but should never be mistaken for a completed run.

## What the Tests Establish

Invented records exercise the locked rule, strict joins, duplicates, missing
measurements, API defaults, bins, Wilson intervals, conservative SNR50 handling,
and answer-key access boundaries. Temporary mock campaigns check file flow and
integrity failures. Analytic waveform tests check matched-filter normalization,
including an independent frequency-domain inner-product calculation.

Preparation tests check equal evidence across comparison arms, separation of
answers, deterministic group selection, and the absence of network clients in
offline preparation. These checks establish software behavior on specified
inputs; they do not validate astronomical performance or calibrated AI judgment.

## Findings That Shape the Next Version

All 37 control false alarms entered through the chirp-like branch. That heuristic
compares two half-window frequency maxima rather than tracking a continuous
ridge. A strong injection can finish before the midpoint and leave the second
half dominated by noise. A more appropriate signal feature needs a separate
design and evaluation, not a retrospective threshold chosen to improve this score.

The test also added signals only to H1 and L1 while some AI explanations penalized
missing Virgo confirmation. Archived external alerts are not generated by a
software injection either. Prepared follow-up inputs limit the detector network
and mark unsupported evidence outside the evaluation for every case.

These are exploratory diagnoses, not causal model ablations. Changes to prompt,
images, detector context, and model all need to be controlled before attributing
an improvement to one component. The open 30-example preparation set is for
development. A later fixed method needs fresh hidden examples.

The original review, implementation phases, and pre-registration are retained
unchanged. Follow [the result summary](RESULTS.md) for current measurements and
[setup](SETUP.md) for commands that can be run without private data.
