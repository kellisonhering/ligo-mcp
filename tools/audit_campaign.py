#!/usr/bin/env python3
"""
LOCAL campaign audit tool — NOT a public unit test.

This is deliberately separate from tests/ because it inspects the local,
off-repo campaign artifacts (pool spec filenames, the campaign records file,
and the SHA-256 seal manifest). It never PRINTS truth contents: it checks
structure (counts, contiguity, no duplicate spec execution) and verifies the
seal manifest mechanically. It does not compare outcomes against truth.

Usage:
    python tools/audit_campaign.py

Exit code 0 = all checks pass, 1 = a check failed.
"""
import hashlib
import json
import os
import sys

HOME = os.path.expanduser("~")
CAMPAIGN_DIR = os.path.join(HOME, "experiment-data/ligo/campaign")
POOL_DIR = os.path.join(CAMPAIGN_DIR, "pool")
RECORDS_FILE = os.path.join(HOME, "experiment-data/ligo/campaign_2026_07.jsonl")
MANIFEST = os.path.join(os.path.dirname(__file__), "..", "campaign_seal", "SHA256SUMS.txt")

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


def check_seal_manifest():
    """Mechanically verify every hashed artifact still matches the sealed manifest."""
    if not os.path.exists(MANIFEST):
        return _fail(f"manifest not found at {MANIFEST}")
    bad = 0
    total = 0
    with open(MANIFEST) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            digest, name = line.split("  ", 1)
            path = os.path.join(CAMPAIGN_DIR, name)
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
    print(f"  OK: all {total} sealed artifacts match the manifest (unaltered)")
    return True


def main():
    print("Campaign audit (structure + seal only; no truth revealed):")
    checks = [
        check_pool_contiguous,
        check_no_duplicate_execution,
        check_records_carry_no_truth,
        check_seal_manifest,
    ]
    ok = all(c() for c in checks)
    print("PASS" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
