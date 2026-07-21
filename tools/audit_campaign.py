#!/usr/bin/env python3
"""
LOCAL campaign audit tool — NOT a public unit test.

This is deliberately separate from tests/ because it inspects the local,
off-repo campaign artifacts (pool spec filenames, the campaign records file,
and the SHA-256 seal manifests). It never PRINTS truth contents: it checks
structure, numerical finiteness, no duplicate execution, and both seals
mechanically. It does not compare outcomes against truth.

Usage:
    python tools/audit_campaign.py

Exit code 0 = all checks pass, 1 = a check failed.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from campaign_quality import load_excluded_spec_ids, validate_spec_file

HOME = os.path.expanduser("~")
CAMPAIGN_DIR = os.path.join(HOME, "experiment-data/ligo/campaign")
POOL_DIR = os.path.join(CAMPAIGN_DIR, "pool")
RECORDS_FILE = os.path.join(HOME, "experiment-data/ligo/campaign_2026_07.jsonl")
MANIFEST = os.path.join(os.path.dirname(__file__), "..", "campaign_seal", "SHA256SUMS.txt")
REPLACEMENT_DIR = os.path.join(HOME, "experiment-data/ligo/campaign_replacements_2026_07")
REPLACEMENT_POOL_DIR = os.path.join(REPLACEMENT_DIR, "pool")
REPLACEMENT_TRUTH = os.path.join(REPLACEMENT_DIR, "campaign_truth_replacements.jsonl")
REPLACEMENT_INDEX = os.path.join(REPLACEMENT_DIR, "pool_index_replacements.json")
REPLACEMENT_MANIFEST = os.path.join(
    os.path.dirname(__file__), "..", "campaign_replacement_seal", "SHA256SUMS.txt"
)

EXPECTED_POOL_SIZE = 300


def _fail(msg):
    print(f"  FAIL: {msg}")
    return False


def check_pool_contiguous():
    """Exactly 300 specs, IDs spec_0000..spec_0299 with no gaps or extras."""
    names = [f for f in os.listdir(POOL_DIR) if f.endswith(".npz")]
    nums = sorted(int(n.replace("spec_", "").replace(".npz", "")) for n in names)
    if len(nums) != EXPECTED_POOL_SIZE:
        return _fail(f"pool has {len(nums)} specs, expected {EXPECTED_POOL_SIZE}")
    if nums != list(range(EXPECTED_POOL_SIZE)):
        missing = sorted(set(range(EXPECTED_POOL_SIZE)) - set(nums))
        return _fail(f"pool IDs not contiguous 0..{EXPECTED_POOL_SIZE-1}; missing {missing[:10]}")
    print(f"  OK: pool is {EXPECTED_POOL_SIZE} contiguous specs (spec_0000..spec_0299)")
    return True


def check_original_pool_numerical_validity():
    """Confirm the frozen exclusion list exactly matches the mechanical audit."""
    excluded = load_excluded_spec_ids()
    invalid = set()
    for i in range(EXPECTED_POOL_SIZE):
        spec_id = f"spec_{i:04d}"
        valid, _ = validate_spec_file(os.path.join(POOL_DIR, f"{spec_id}.npz"))
        if not valid:
            invalid.add(spec_id)
    if invalid != excluded:
        return _fail(
            "mechanically invalid original specs do not match the frozen exclusion list"
        )
    print(f"  OK: exclusion list exactly matches {len(excluded)} non-finite originals")
    return True


def check_replacement_pool():
    """Exactly one finite replacement exists for every frozen exclusion."""
    if not os.path.isdir(REPLACEMENT_POOL_DIR) or not os.path.exists(REPLACEMENT_TRUTH):
        return _fail("replacement pool is not complete")
    excluded = load_excluded_spec_ids()
    expected = {f"replacement_{spec_id}" for spec_id in excluded}
    names = {name[:-4] for name in os.listdir(REPLACEMENT_POOL_DIR) if name.endswith(".npz")}
    if names != expected:
        return _fail(f"replacement roster has {len(names)} specs, expected {len(expected)}")
    for spec_id in sorted(expected):
        valid, reason = validate_spec_file(os.path.join(REPLACEMENT_POOL_DIR, f"{spec_id}.npz"))
        if not valid:
            return _fail(f"replacement {spec_id} is invalid: {reason}")

    mappings = {}
    with open(REPLACEMENT_TRUTH) as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            mappings[row["spec_id"]] = row.get("replaces_spec_id")
    expected_mappings = {f"replacement_{sid}": sid for sid in excluded}
    if mappings != expected_mappings:
        return _fail("replacement truth does not map one-to-one onto the exclusions")
    with open(REPLACEMENT_INDEX) as f:
        index_ids = set(json.load(f))
    if index_ids != expected:
        return _fail("replacement index does not match the replacement roster")
    print(f"  OK: all {len(expected)} replacements are one-to-one and numerically finite")
    return True


def check_no_duplicate_execution():
    """No campaign spec_id appears in the records file more than once."""
    if not os.path.exists(RECORDS_FILE):
        print("  SKIP: no campaign records file yet")
        return True
    seen = {}
    n = 0
    with open(RECORDS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n += 1
            sid = (json.loads(line).get("injection") or {}).get("spec_id")
            if sid:
                seen[sid] = seen.get(sid, 0) + 1
    dups = {k: v for k, v in seen.items() if v > 1}
    if dups:
        return _fail(f"duplicate spec execution: {dups}")
    print(f"  OK: {n} campaign records, {len(seen)} unique specs, no duplicates")
    return True


def check_records_carry_no_truth():
    """Structural check: no record leaks a truth field into its body."""
    if not os.path.exists(RECORDS_FILE):
        print("  SKIP: no campaign records file yet")
        return True
    truth_fields = {"mass1", "mass2", "target_network_snr", "achieved_network_snr",
                    "kind", "seed", "ra", "dec", "polarization", "inclination"}
    with open(RECORDS_FILE) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            keys = set(_all_keys(json.loads(line)))
            leaked = keys & truth_fields
            if leaked:
                return _fail(f"record {i} leaks truth field(s): {leaked}")
    print("  OK: no campaign record body carries a truth field")
    return True


def _all_keys(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(k)
            _all_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _all_keys(v, out)
    return out


def _check_seal_manifest(manifest, artifact_dir, label):
    """Mechanically verify every hashed artifact still matches one seal manifest."""
    if not os.path.exists(manifest):
        return _fail(f"{label} manifest not found at {manifest}")
    bad = 0
    total = 0
    with open(manifest) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            digest, name = line.split("  ", 1)
            path = os.path.join(artifact_dir, name)
            total += 1
            if not os.path.exists(path):
                bad += 1
                print(f"  FAIL: sealed file missing: {name}")
                continue
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != digest:
                bad += 1
                print(f"  FAIL: hash mismatch (altered since seal): {name}")
    if bad:
        return _fail(f"{bad}/{total} sealed artifacts failed verification")
    print(f"  OK: all {total} {label} artifacts match the manifest (unaltered)")
    return True


def check_original_seal_manifest():
    return _check_seal_manifest(MANIFEST, CAMPAIGN_DIR, "original sealed")


def check_replacement_seal_manifest():
    return _check_seal_manifest(
        REPLACEMENT_MANIFEST, REPLACEMENT_DIR, "replacement sealed"
    )


def main():
    print("Campaign audit (structure + finiteness + seals; no truth revealed):")
    checks = [
        check_pool_contiguous,
        check_original_pool_numerical_validity,
        check_replacement_pool,
        check_no_duplicate_execution,
        check_records_carry_no_truth,
        check_original_seal_manifest,
        check_replacement_seal_manifest,
    ]
    ok = all(c() for c in checks)
    print("PASS" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
