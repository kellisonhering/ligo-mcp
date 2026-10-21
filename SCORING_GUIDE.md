# Grading the July 2026 experiment

The experiment is a 300-question exam for the pipeline. Some windows contain a
signal we added; others are controls with no added signal. The sealed answer
keys say which is which. The scorer grades the exam using the rules already
locked in REVIEW_FINDINGS.md sections 7.5, 7.5c, 7.5d and the July 30 note.

Building or testing the scorer does not open the real answers. This tool runs
locally, without API calls or restarting the experiment.

## Three commands, three purposes

Run from the code folder (`~/ligo-mcp`).

1. Check the real campaign's readiness, keeping labels private:

   ```bash
   python3 analyze_injections.py check --receipt ~/experiment-data/ligo/analysis/scorer-ready-v2.json
   ```

   Checks all 300 active IDs, unique executions, required measurements, the
   frozen exclusion list, numerical validity and both artifact seals. Truth files
   are read as opaque bytes to verify their hashes; labels are never parsed.
   There is no performance report in this mode.
   The receipt saves hashes of the inputs and scorer source before unblinding.
   Plain `check` is also available without writing a receipt. A receipt's parent
   folder must exist; existing receipts are never overwritten.

2. Try the complete report with invented data:

   ```bash
   python3 analyze_injections.py demo --output ~/experiment-data/ligo/analysis/scorer-demo-v1
   ```

   Every number in this report is made up. It shows how the report and charts
   work. It never reads the real campaign. Choose a new output folder each time.
   The matched-filter panel is a clearly marked placeholder in the demo.

3. When ready to open the actual answer keys and grade the real exam:

   ```bash
   ~/venvs/ligo-injections/bin/python analyze_injections.py score --unblind --receipt ~/experiment-data/ligo/analysis/scorer-ready-v2.json --output ~/experiment-data/ligo/analysis/campaign-score-v1
   ```

   This command opens both real answer keys. Use it after reviewing and freezing
   the scorer. It runs the readiness checks first, joins each result to its
   answer, computes the matched-filter yardstick, verifies unchanged inputs,
   and writes results into a new folder. Omitting `--unblind` stops before any
   campaign data is opened. Existing output folders are never overwritten.
   The scorer and inputs must match the saved readiness receipt. Any later code
   or input change is reported before either answer key is parsed.

   The injection environment supplies PyCBC and GWpy. The other commands need
   the existing project dependencies. No dependencies are installed by the tool.

## What gets measured

- **Primary catch:** did it see a chirp shape OR agreement between detectors?
  This is the frozen mechanical rule. It is recomputed from raw measurements,
  including for records whose old stored baseline used the wrong AND rule.
  A flag in an injected window can also arise from a noise feature. The locked
  rule measures window flagging, not proof that a flag matched the added signal.
- **Secondary escalation:** did the AI choose `follow_up` or
  `candidate_for_human_review`, asking a human to look?
- **Recovery by strength:** of the signals we added, what fraction was caught
  as they got stronger? The x-axis is achieved network optimal SNR measured
  against that window's noise. It is different from energy contrast.
- **False alarms:** of the noise controls, what fraction got flagged? These
  are fractions per tested window, not an astronomical false-alarm rate per year.
- **AI comparison:** on the same available runs, how many signals and controls
  did AI escalation flag versus the physics rule? Catching more signals and
  avoiding noise must be considered together. Primary and physics-baseline
  catches are the same rule, so they are not two competing detectors.
- **Score reliability:** do higher AI scores correspond to more injections?
  The score is not a promised probability. This designed exam contains 200
  injections and 100 controls; real survey candidates have a different mix.
- **Matched-filter yardstick:** how much signal can an analysis recover when
  handed the exact waveform and arrival time? It uses the saved H1/L1 noise
  plus injection, with the same measured noise PSD as generation. Its optimistic
  advantage is disclosed: it does not search a template bank or measure that
  search's false-alarm rate. Controls have no true waveform, so no matched-filter
  recovery or false-alarm fraction is invented for them.

## Accounting for known problems

The active roster is 248 finite originals plus 52 finite replacements. The 16
already-attempted invalid originals are retained in the audit export and never
scored. `injection.injected` means a campaign spec was used, and is true for
both injections and controls. It is never used as the answer label.

The two recorded API failures remain in the full operational counts and control
denominators, as disclosed in the July 30 note. For the paired AI comparison,
any row with a planner or vision API error is removed from both arms. The report
lists those IDs. The score-reliability chart also excludes pipeline errors where
no AI analysis was possible. This prevents a fallback zero from masquerading as
an AI opinion. Missing required fields cause an error; they do not become misses.

Pipeline failures can skip the AI without setting an API error. The July 30
comparison retains these runs, following its stated exclusion rule, but now
reports their count and IDs explicitly. A supplementary available-call comparison
also excludes runs where `pipeline_error` is populated and `energy_contrast` is
null, matching the pipeline's skip condition. It removes the same runs from both
arms and leaves every primary/secondary operational denominator unchanged.

Results include all recorded model/version combinations. If models changed
during collection, pooled results describe that actual mixture, not one model.

## Choices made before opening the keys

The following details complete the reporting method where the original rules
did not specify an exact implementation. They do not change the frozen catch
rules or label assignments:

- 95% Wilson binomial intervals for proportions, including zero successes.
- Strength bins `[4,6), [6,8), ..., [22,24]`; the final bin includes 24.
- SNR50 interpolated between adjacent bin centers at a 50% caught fraction.
  Empty bins are not bridged. Multiple crossings or an unbracketed curve leave
  the point estimate unavailable. Its uncertainty is not estimated; sensitivity
  intervals and false alarms must accompany any interpretation.
  Exact 50% points require adjacent support for an upward crossing. Downward
  crossings, touches that turn back, flat 50% plateaus, and isolated 50% points
  do not produce an interpretable single threshold.
- Five fixed AI-score bins of width 0.2. No fitted score calibration or claim
  that `interesting_score` is a probability.
- Matched filtering above 20 Hz, using the exact saved injection as template,
  PSD measured on the saved noise, and correlation at zero lag (the known injected
  time). H1/L1 magnitudes are combined in quadrature. Recomputed optimal SNR must
  match the sealed achieved label within 0.002, allowing its three-decimal rounding.
- Paired differences are descriptive. No new significance threshold, fitted
  operating point, or matched-filter detection threshold is chosen after scoring.

## Pre-unblinding review, 2026-10-18

Scorer version 1.1.0 corrects the exact-50% edge cases and discloses pipeline-skipped
AI calls, with a separate available-call comparison. These changes were made
using invented labeled data only. The original scoring rules and campaign data
were not edited. The new readiness receipt is `scorer-ready-v2.json`; the earlier
receipt is preserved but no longer matches the reviewed source.

## Reading the output

Start with `REPORT.md`. `efficiency.png` shows catch rates by strength;
`score_reliability.png` shows score groups; `matched_filter.png` shows the
optimistic physical yardstick. Each also has a PDF for sharing.

`results.json` contains counts, denominators, intervals, comparisons, method
choices, record version groups, source/input SHA-256 hashes and runtime versions.
`scored_records.jsonl` contains graded rows and joined labels;
`excluded_attempts.jsonl` records the invalid attempts. `efficiency.csv` includes
the frozen supplementary numerical-confidence thresholds 0.3, 0.45 and 0.6.

`COMPLETE.json` indicates all reports and charts finished. A failed run may
leave a partial output folder without this marker. `SHA256SUMS.txt` hashes the
output files. Scoring reads the saved experiment and writes reports only.

These results concern the existing stellar-mass BBH exam in O3 noise. A separate
study would be needed for O4 noise or subsolar signals. A noise-control label is
an experimental assignment, not a proof that every natural feature is known.

Before a public release, document the source-data attribution and licensing,
review identifiers and annotations, and include the experiment's limitations.

## Testing without real truth

```bash
python3 -m pytest tests/ -q
~/venvs/ligo-injections/bin/python -m unittest discover -s tests -p test_campaign_matched_filter.py -v
```

Tests use invented records, temporary mock campaigns, and analytic waveforms.
They cover the frozen rules, replacements, error handling, bin edges,
uncertainty, answer-key access boundary, output integrity, and matched-filter
normalization. The existing local audit can also run as before.
