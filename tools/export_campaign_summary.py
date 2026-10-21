#!/usr/bin/env python3
"""Export aggregate campaign results without records, labels, or machine paths."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


ASSETS = ("efficiency.csv", "efficiency.png", "score_reliability.png", "matched_filter.png")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def public_summary(result):
    if result.get("demo") is not False or result["input_snapshot"].get("truth_values_opened") is not True:
        raise ValueError("requires a completed real campaign result")
    snapshot = result["input_snapshot"]
    return {
        "campaign": "July 2026 O3 H1/L1 injection campaign",
        "graded_at_utc": result["created_at"], "scorer_version": result["scorer_version"],
        "counts": result["counts"], "rules": result["rules"], "metrics": result["metrics"],
        "paired_comparison": {k: result["paired_comparison"][k] for k in ("injection", "noise")},
        "available_call_comparison": {k: result["available_call_comparison"][k] for k in ("injection", "noise")},
        "supplementary_confidence": result["supplementary_confidence"],
        "score_reliability": {k: result["score_reliability"][k] for k in ("bins", "eligible", "note")},
        "version_groups": result["version_groups"],
        "provenance": {"records_sha256": snapshot["records_sha256"],
                       "manifest_sha256": snapshot["manifest_sha256"],
                       "reviewed_source_sha256": snapshot["source_sha256"],
                       "runtime": {k: result["runtime"][k] for k in ("python", "packages", "git_head")}},
        "scope": "Aggregate export only. Full independent re-scoring needs the off-repository campaign pool and records.",
    }


def export(source, output):
    if output.exists():
        raise ValueError("choose a new output directory")
    entries = {}
    for line in (source / "SHA256SUMS.txt").read_text().splitlines():
        expected, name = line.split("  ", 1)
        if Path(name).name != name or name in entries or digest(source / name) != expected:
            raise ValueError("original grading output failed its integrity check")
        entries[name] = expected
    if not {"results.json", "COMPLETE.json", *ASSETS} <= entries.keys():
        raise ValueError("original grading manifest is incomplete")
    complete = json.loads((source / "COMPLETE.json").read_text())
    if complete.get("complete") is not True or complete.get("demo") is not False:
        raise ValueError("original campaign result is not complete")
    result = json.loads((source / "results.json").read_text())
    public = public_summary(result)
    public["provenance"]["original_results_sha256"] = entries["results.json"]
    public["provenance"]["original_asset_sha256"] = {name: entries[name] for name in ASSETS}
    public["provenance"]["csv_newlines"] = "LF in public copy; original asset hashes refer to untouched source files"
    output.mkdir(parents=True, exist_ok=False)
    (output / "summary.json").write_text(json.dumps(public, indent=2, allow_nan=False) + "\n")
    for name in ASSETS:
        if name.endswith(".csv"):
            (output / name).write_bytes((source / name).read_bytes().replace(b"\r\n", b"\n"))
        else:
            shutil.copyfile(source / name, output / name)
    (output / "SHA256SUMS.txt").write_text("".join(
        f"{digest(p)}  {p.name}\n" for p in sorted(output.iterdir()) if p.is_file()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        export(args.source.expanduser(), args.output.expanduser())
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Export stopped: {exc}\n")
    print(f"Exported aggregate results: {args.output.expanduser()}")


if __name__ == "__main__":
    main()
