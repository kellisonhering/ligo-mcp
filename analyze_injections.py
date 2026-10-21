#!/usr/bin/env python3
"""Campaign scorer: demo, blind readiness check, or explicit real unblinding.

See SCORING_GUIDE.md. Importing this module never reads campaign data.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import random
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from campaign_scoring import (
    ScoringError, grade_record, join_truth, score_rows, validate_record,
)


REPO = Path(__file__).resolve().parent
DATA = Path.home() / "experiment-data/ligo"
ORIGINAL_IDS = {f"spec_{i:04d}" for i in range(300)}
SOURCE_FILES = (
    "analyze_injections.py", "campaign_scoring.py", "campaign_matched_filter.py",
    "scoring_report.py", "campaign_quality.py", "injections.py", "REVIEW_FINDINGS.md",
    "SCORING_GUIDE.md", "tests/test_campaign_scoring.py",
    "tests/test_campaign_matched_filter.py",
)


@dataclass(frozen=True)
class CampaignPaths:
    repo: Path = REPO
    data: Path = DATA

    @property
    def original(self):
        return self.data / "campaign"

    @property
    def replacement(self):
        return self.data / "campaign_replacements_2026_07"

    @property
    def records(self):
        return self.data / "campaign_2026_07.jsonl"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError) as exc:
                raise ScoringError(f"invalid JSON in {path.name}, line {number}") from exc
            if not isinstance(row, dict):
                raise ScoringError(f"expected an object in {path.name}, line {number}")
            rows.append(row)
    return rows


def verify_manifest(manifest: Path, root: Path, expected: set[str]) -> dict:
    """Hash sealed bytes; never parse or print their truth values."""
    entries = {}
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        try:
            checksum, name = line.split("  ", 1)
        except ValueError as exc:
            raise ScoringError("invalid seal manifest") from exc
        if name not in expected or name in entries or len(checksum) != 64:
            raise ScoringError("seal manifest has an unexpected or duplicate entry")
        path = root / name
        if path.resolve() != root.resolve() / name:
            raise ScoringError("sealed artifact resolves outside its expected directory")
        if digest(path) != checksum:
            raise ScoringError(f"sealed artifact changed: {name}")
        entries[name] = checksum
    if set(entries) != expected:
        raise ScoringError("seal manifest is incomplete")
    return entries


def _keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _keys(nested)


def check_campaign(paths: CampaignPaths) -> tuple[dict, list[dict], set[str]]:
    """Validate complete roster, arrays, and seals without parsing either key."""
    from campaign_quality import load_excluded_spec_ids, validate_spec_file

    exclusions = paths.repo / "campaign_repair/invalid_specs.json"
    excluded = load_excluded_spec_ids(exclusions)
    if len(excluded) != 52 or not excluded <= ORIGINAL_IDS:
        raise ScoringError("frozen exclusion list must contain 52 original IDs")
    replacement_ids = {f"replacement_{sid}" for sid in excluded}
    active = ORIGINAL_IDS - excluded | replacement_ids
    original_manifest = paths.repo / "campaign_seal/SHA256SUMS.txt"
    replacement_manifest = paths.repo / "campaign_replacement_seal/SHA256SUMS.txt"
    original_expected = {f"pool/{sid}.npz" for sid in ORIGINAL_IDS} | {
        "campaign_truth.jsonl", "pool_index.json", "pool_gen.log"}
    replacement_expected = {f"pool/{sid}.npz" for sid in replacement_ids} | {
        "campaign_truth_replacements.jsonl", "pool_index_replacements.json", "pool_gen_replacements.log"}
    seals = {
        "original": verify_manifest(original_manifest, paths.original, original_expected),
        "replacement": verify_manifest(replacement_manifest, paths.replacement, replacement_expected),
    }
    for directory, expected_ids in ((paths.original, ORIGINAL_IDS), (paths.replacement, replacement_ids)):
        if {p.stem for p in (directory / "pool").glob("*.npz")} != expected_ids:
            raise ScoringError("pool filenames disagree with the frozen roster")
        for sid in sorted(expected_ids):
            valid, _ = validate_spec_file(directory / "pool" / (sid + ".npz"))
            if valid == (sid in excluded):
                raise ScoringError("numerical validity disagrees with the frozen exclusions")
    records_hash = digest(paths.records)
    records = read_jsonl(paths.records)
    seen, experiments = set(), set()
    forbidden = {"kind", "mass1", "mass2", "target_network_snr", "achieved_network_snr",
                 "seed", "ra", "dec", "polarization", "inclination"}
    for record in records:
        sid = (record.get("injection") or {}).get("spec_id")
        eid = record.get("experiment_id")
        if sid not in active | excluded or sid in seen or not eid or eid in experiments:
            raise ScoringError("campaign has unexpected or duplicate executions")
        if forbidden & set(_keys(record)):
            raise ScoringError("campaign record contains a forbidden truth field")
        seen.add(sid)
        experiments.add(eid)
        if sid not in excluded:
            validate_record(record)
    if seen - excluded != active:
        raise ScoringError(f"campaign incomplete: {len(seen - excluded)}/300 active specs recorded")
    if digest(paths.records) != records_hash:
        raise ScoringError("campaign records changed during the readiness check")
    source_hashes = {name: digest(paths.repo / name) for name in SOURCE_FILES}
    snapshot = {
        "ready": True, "truth_values_opened": False,
        "records": len(records), "active_records": len(active),
        "excluded_attempts": len(seen & excluded),
        "records_sha256": records_hash, "source_sha256": source_hashes,
        "exclusions_sha256": digest(exclusions),
        "manifest_sha256": {"original": digest(original_manifest), "replacement": digest(replacement_manifest)},
        "sealed_artifacts": seals,
    }
    return snapshot, records, excluded


def make_demo() -> tuple[list[dict], list[dict]]:
    """A reproducible invented exam, unrelated to the real campaign."""
    rng = random.Random(20261018)
    rows = []
    for i in range(300):
        injected = i < 200
        snr = 5 + 2 * (i // 20) if injected else None
        caught = rng.random() < ((snr - 2) / 24 if injected else 0.08)
        escalated = caught and rng.random() < 0.85
        record = {
            "experiment_id": f"DEMO_{i:04d}", "mode": "campaign", "schema_version": 4,
            "gps_time": float(i), "injection": {"spec_id": f"demo_{i:04d}", "injected": True},
            "detection": {"chirp_like": caught, "signal_detected": True,
                          "confidence_score": 0.8 if caught else 0.15, "pipeline_error": None},
            "coincidence": {"checked": True, "coincident": False},
            "catalog": {"known_event_match": False}, "vision": {"error": None},
            "llm_review": {"decision": "follow_up" if escalated else "archive",
                           "interesting_score": 0.75 if escalated else 0.1,
                           "planner_error": "synthetic API failure" if i == 10 else None},
            "versions": {"pipeline": "DEMO", "detection_algorithm": "DEMO",
                         "planner_prompt": "DEMO", "vision_prompt": "DEMO",
                         "planner_model": "INVENTED", "vision_model": "INVENTED", "llm_temperature": 0},
        }
        if i == 10:
            record["llm_review"].update(decision="archive", interesting_score=0.0)
        rows.append(dict(grade_record(record), kind="injection" if injected else "noise", snr=snr))
    return rows, []


def runtime_provenance() -> dict:
    import platform
    packages = {}
    for name in ("numpy", "matplotlib", "gwpy", "pycbc", "scipy"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "not installed"
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {"python": platform.python_version(), "packages": packages, "git_head": commit}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check real readiness; no truth parsing or scoring")
    check.add_argument("--receipt", type=Path, help="save a new readiness/source-hash receipt")
    demo = commands.add_parser("demo", help="write an invented-data demonstration")
    demo.add_argument("--output", type=Path, required=True, help="new output directory")
    score = commands.add_parser("score", help="open real keys and produce the locked results")
    score.add_argument("--unblind", action="store_true", help="explicitly open the real answer keys")
    score.add_argument("--receipt", type=Path, required=True, help="receipt from the reviewed scorer's check")
    score.add_argument("--output", type=Path, required=True, help="new output directory")
    args = parser.parse_args(argv)
    if args.command == "score" and not args.unblind:
        parser.error("real scoring opens sealed truth; add --unblind when ready")
    if args.command != "check" and args.output.expanduser().exists():
        parser.error("output directory already exists; choose a new one to preserve prior results")
    try:
        if args.command == "check":
            snapshot, _, _ = check_campaign(CampaignPaths())
            if args.receipt:
                with args.receipt.expanduser().open("x") as stream:
                    json.dump({"prepared_at": datetime.now(timezone.utc).isoformat(),
                               "snapshot": snapshot}, stream, indent=2, allow_nan=False)
                    stream.write("\n")
            print(f"PASS: {snapshot['active_records']}/300 active records; "
                  f"{snapshot['excluded_attempts']} excluded attempts; both seals intact.")
            print("Answer keys were hashed only. No truth values were parsed or scored.")
            return 0
        from scoring_report import write_outputs
        if args.command == "demo":
            rows, invalid = make_demo()
            snapshot = {"demo": True, "truth_values_opened": False,
                        "source_sha256": {name: digest(REPO / name) for name in SOURCE_FILES}}
        else:
            # Check dependencies and the full blinded campaign before opening either key.
            import matplotlib
            import pycbc
            import gwpy
            from campaign_matched_filter import recover_campaign

            paths = CampaignPaths()
            snapshot, records, excluded = check_campaign(paths)
            receipt_path = args.receipt.expanduser()
            receipt = json.loads(receipt_path.read_text())
            if not isinstance(receipt, dict) or not isinstance(receipt.get("prepared_at"), str):
                raise ScoringError("invalid readiness receipt")
            if receipt.get("snapshot") != snapshot:
                raise ScoringError("scorer or inputs differ from the reviewed readiness receipt")
            originals = read_jsonl(paths.original / "campaign_truth.jsonl")
            replacements = read_jsonl(paths.replacement / "campaign_truth_replacements.jsonl")
            rows, invalid = join_truth(records, originals, replacements, excluded, ORIGINAL_IDS)
            recover_campaign(rows, paths.original / "pool", paths.replacement / "pool")
            after, _, _ = check_campaign(paths)
            if snapshot != after:
                raise ScoringError("inputs or scorer source changed while scoring")
            snapshot["truth_values_opened"] = True
            snapshot["readiness_receipt_sha256"] = digest(receipt_path)
            snapshot["prepared_at"] = receipt["prepared_at"]
        result = score_rows(rows, invalid)
        result.update(demo=args.command == "demo", created_at=datetime.now(timezone.utc).isoformat(),
                      input_snapshot=snapshot, runtime=runtime_provenance())
        write_outputs(args.output.expanduser(), result, rows, invalid)
        print(f"{'DEMONSTRATION ONLY' if result['demo'] else 'Scored campaign'}: {args.output.expanduser()}")
        return 0
    except (ValueError, OSError, ImportError) as exc:
        print(f"Scoring stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
