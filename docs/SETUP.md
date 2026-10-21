# Setup and Testing

The offline demo is the recommended starting point. It does not need an API key,
private campaign files, GWOSC downloads, a running service, or Home Assistant.

## Free Demo

Use Python 3.11+; GitHub Actions exercises Python 3.12.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-demo.txt
python analyze_injections.py demo --output demo-output
```

In Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` instead.
If `python3` is unavailable, use your Python 3 interpreter's `python` command.

The output contains a Markdown report, JSON metrics, a CSV of efficiency curves,
PNG/PDF charts, a checksum manifest, and a final completion marker. Everything
is labelled demonstration data. The matched-filter chart is a placeholder in
demo mode; the demo does not generate real strain or run signal recovery.

Existing output directories are never overwritten. Choose a new directory to
repeat the demonstration. Dependency installation uses the network; the demo
itself uses local invented data and performs no paid calls.

## Tests

The small scorer/preparation tests need only the demo dependencies and pytest:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_campaign_scoring.py tests/test_comparison_prep.py tests/test_fable_batch_prep.py tests/test_public_results.py -q
```

To run the legacy pipeline tests as well, install the optional full-pipeline
dependencies. The tests do not start the survey loop or make paid API calls:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

The five matched-filter tests require PyCBC/GWpy and skip when PyCBC is absent.
Use a separate environment for these larger numerical dependencies:

```bash
python3 -m venv .venv-injections
source .venv-injections/bin/activate
python -m pip install -r requirements-injections.txt
python -m unittest discover -s tests -p test_campaign_matched_filter.py -v
```

They use analytic signals and temporary synthetic arrays, not the private pool.
CI runs the free demo, full offline unit suite, and matched-filter analytic
checks as separate jobs. No API secrets are supplied to those jobs.

## Working With the Original Campaign

The published aggregate summary and charts are in `docs/results/`. Original
records, noise/injection arrays, and answer keys remain outside this repo.
The public demo and tests can be reproduced without them. Full independent
re-scoring of the original campaign cannot be done from this public subset.

If you have the original data, the expected layout is:

```text
~/experiment-data/ligo/
  campaign_2026_07.jsonl
  campaign/
    campaign_truth.jsonl
    pool/
  campaign_replacements_2026_07/
    campaign_truth_replacements.jsonl
    pool/
  analysis/
```

The readiness check hashes answer files without parsing labels. Explicit real
scoring opens the answer keys. The July campaign was already unblinded and
scored on October 18, 2026; this command boundary is still useful for later
work. See [SCORING_GUIDE.md](../SCORING_GUIDE.md) for the commands and exact
conventions. Its saved receipt filenames refer to the historical reviewed
source. If tracked scorer sources change, generate a new receipt rather than
overwriting the earlier one.

The [aggregate export tool](../tools/export_campaign_summary.py) creates only
the public tables and selected charts from a completed local grading folder:

```bash
python tools/export_campaign_summary.py --source /path/to/completed-score --output /path/to/new-public-export
```

It refuses an existing output directory, verifies the original output checksums,
and does not export per-record data, answer keys, or local file paths.

## Optional Live Pipeline and MCP Interface

The original code is preserved for inspection. Its entry points are `loop.py`
for survey processing and `server.py` for FastMCP tools. `requirements.txt`
contains their dependencies. Real analysis downloads GWOSC data and calls
Anthropic; it is a different execution path from the free demo.

The AI client expects `ANTHROPIC_API_KEY` in the environment. The legacy code
also loads an operator's `~/.openclaw/.env` if present. No key or machine-specific
environment file is shipped in this repository.

Output is stored under `~/experiment-data/ligo/` by default, historically chosen
to avoid iCloud mid-write locks. The original macOS installation used a launchd
agent named `com.kellison.ligo-loop`, managed by the MCP service tools. Its local
plist is not distributed. Installing dependencies, running the demo, or running
offline tests does not register or start that service.

O4/subsolar support still has known issues in the original review, and is not
validated by the stellar-mass O3 campaign. The original background experiment
remains stopped; this portfolio update does not resume data collection.
