#!/usr/bin/env python3
"""Generate blind, numerically valid replacements for the §7.5d exclusions.

Run with the injection environment:
    ~/venvs/ligo-injections/bin/python tools/generate_campaign_replacements.py

The script reads the original sealed truth internally only to preserve each
excluded spec's preselected injection/control assignment and, for injections,
its waveform parameters and target SNR. It never prints those values. Original
artifacts and campaign records are read-only inputs and are never modified.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

from campaign_quality import (  # noqa: E402
    EXPECTED_SAMPLES,
    EXPECTED_SAMPLE_RATE,
    load_excluded_spec_ids,
    require_finite,
    validate_spec_file,
)
from injection_pool_generator import (  # noqa: E402
    FETCH_SECONDS,
    load_catalog_events,
    pick_noise_window,
    try_fetch,
)
from injections import (  # noqa: E402
    generate_bbh,
    measured_psd,
    network_snr,
    optimal_snr,
    project_into_window,
)

HOME = Path.home()
ORIGINAL_DIR = HOME / "experiment-data/ligo/campaign"
ORIGINAL_POOL = ORIGINAL_DIR / "pool"
ORIGINAL_TRUTH = ORIGINAL_DIR / "campaign_truth.jsonl"
ORIGINAL_INDEX = ORIGINAL_DIR / "pool_index.json"

REPLACEMENT_DIR = HOME / "experiment-data/ligo/campaign_replacements_2026_07"
REPLACEMENT_POOL = REPLACEMENT_DIR / "pool"
REPLACEMENT_TRUTH = REPLACEMENT_DIR / "campaign_truth_replacements.jsonl"
REPLACEMENT_INDEX = REPLACEMENT_DIR / "pool_index_replacements.json"
REPLACEMENT_LOG = REPLACEMENT_DIR / "pool_gen_replacements.log"

REPLACEMENT_SEED = 20260721
MAX_WINDOW_ATTEMPTS = 50


def replacement_id(original_spec_id: str) -> str:
    return f"replacement_{original_spec_id}"


def _read_jsonl_by_id(path: Path) -> dict[str, dict]:
    rows = {}
    with open(path) as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                rows[row["spec_id"]] = row
    return rows


def _atomic_savez(path: Path, **arrays) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".npz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        np.savez_compressed(tmp_path, **arrays)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _append_jsonl(path: Path, row: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _write_index(path: Path, index: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(index, f, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _strict_noise_window(rng, excluded_events, tried_gps):
    for _ in range(MAX_WINDOW_ATTEMPTS):
        gps = pick_noise_window(rng, excluded_events, tried_gps)
        if gps is None:
            continue
        data = try_fetch(gps)
        if data is None:
            continue
        arrays = {det: np.asarray(data[det].value, dtype=np.float64) for det in ("H1", "L1")}
        if any(len(arrays[det]) != EXPECTED_SAMPLES for det in arrays):
            continue
        if any(not np.isfinite(arrays[det]).all() for det in arrays):
            continue
        return gps, data, arrays
    return None


def _build_replacement(original: dict, rng, excluded_events, tried_gps):
    for _ in range(MAX_WINDOW_ATTEMPTS):
        fetched = _strict_noise_window(rng, excluded_events, tried_gps)
        if fetched is None:
            break
        gps, data, noise = fetched
        n_samples = len(noise["H1"])
        inj_h1 = np.zeros(n_samples, dtype=np.float64)
        inj_l1 = np.zeros(n_samples, dtype=np.float64)
        achieved = None

        try:
            if original["kind"] == "injection":
                hp, hc = generate_bbh(
                    original["mass1"], original["mass2"], distance_mpc=400.0,
                    inclination=original["inclination"], coa_phase=original["coa_phase"],
                )
                fetch_start = gps - FETCH_SECONDS / 2
                inj_h1 = project_into_window(
                    hp, hc, "H1", original["ra"], original["dec"],
                    original["polarization"], gps, fetch_start, n_samples,
                )
                inj_l1 = project_into_window(
                    hp, hc, "L1", original["ra"], original["dec"],
                    original["polarization"], gps, fetch_start, n_samples,
                )
                require_finite("unscaled H1 injection", inj_h1)
                require_finite("unscaled L1 injection", inj_l1)

                psd_h1 = measured_psd(data["H1"])
                psd_l1 = measured_psd(data["L1"])
                require_finite("H1 PSD", psd_h1)
                require_finite("L1 PSD", psd_l1)
                raw_h1 = optimal_snr(inj_h1, psd_h1, FETCH_SECONDS)
                raw_l1 = optimal_snr(inj_l1, psd_l1, FETCH_SECONDS)
                net_raw = network_snr({"H1": raw_h1, "L1": raw_l1})
                require_finite("unscaled network SNR", net_raw)
                if net_raw < 1e-6:
                    continue

                scale = float(original["target_network_snr"]) / net_raw
                require_finite("injection scale", scale)
                inj_h1 = inj_h1 * scale
                inj_l1 = inj_l1 * scale
                require_finite("scaled H1 injection", inj_h1)
                require_finite("scaled L1 injection", inj_l1)
                achieved = network_snr({
                    "H1": optimal_snr(inj_h1, psd_h1, FETCH_SECONDS),
                    "L1": optimal_snr(inj_l1, psd_l1, FETCH_SECONDS),
                })
                require_finite("achieved network SNR", achieved)
        except Exception:
            tried_gps.append(gps)
            continue

        return gps, noise["H1"], noise["L1"], inj_h1, inj_l1, achieved
    return None


def main() -> None:
    excluded_ids = sorted(load_excluded_spec_ids())
    original_truth = _read_jsonl_by_id(ORIGINAL_TRUTH)
    missing_truth = set(excluded_ids) - set(original_truth)
    if missing_truth:
        raise SystemExit(f"missing sealed truth rows for {len(missing_truth)} excluded specs")

    # Re-derive the exclusion mechanically. This never consults truth labels or outcomes.
    derived_invalid = {
        spec_id for spec_id in original_truth
        if not validate_spec_file(ORIGINAL_POOL / f"{spec_id}.npz")[0]
    }
    if derived_invalid != set(excluded_ids):
        raise SystemExit("public exclusion list does not match the mechanical finiteness audit")

    REPLACEMENT_POOL.mkdir(parents=True, exist_ok=True)
    replacement_truth = (
        _read_jsonl_by_id(REPLACEMENT_TRUTH) if REPLACEMENT_TRUTH.exists() else {}
    )
    replacement_index = (
        json.load(open(REPLACEMENT_INDEX)) if REPLACEMENT_INDEX.exists() else {}
    )
    original_index = json.load(open(ORIGINAL_INDEX))
    tried_gps = [float(gps) for gps in original_index.values()]
    tried_gps.extend(float(gps) for gps in replacement_index.values())
    excluded_events = load_catalog_events()

    with open(REPLACEMENT_LOG, "a") as log:
        for original_id in excluded_ids:
            rid = replacement_id(original_id)
            path = REPLACEMENT_POOL / f"{rid}.npz"
            if rid in replacement_truth and rid in replacement_index and validate_spec_file(path)[0]:
                continue

            numeric_id = int(original_id.split("_")[1])
            rng = np.random.default_rng(REPLACEMENT_SEED + numeric_id)
            built = _build_replacement(
                original_truth[original_id], rng, excluded_events, tried_gps,
            )
            if built is None:
                raise SystemExit(f"could not generate a valid replacement for {original_id}")
            gps, h1, l1, inj_h1, inj_l1, achieved = built
            _atomic_savez(
                path, H1=h1, L1=l1, inj_H1=inj_h1, inj_L1=inj_l1,
                sample_rate=EXPECTED_SAMPLE_RATE, gps=gps,
            )
            valid, reason = validate_spec_file(path)
            if not valid:
                path.unlink(missing_ok=True)
                raise SystemExit(f"generated replacement failed validation: {reason}")

            original = original_truth[original_id]
            row = {
                "spec_id": rid,
                "replaces_spec_id": original_id,
                "replacement_reason": "non-finite original pool arrays under §7.5d",
                "kind": original["kind"],
                "gps_time": round(gps, 4),
                "seed": REPLACEMENT_SEED + numeric_id,
            }
            if original["kind"] == "injection":
                for key in (
                    "mass1", "mass2", "ra", "dec", "polarization", "inclination",
                    "coa_phase", "target_network_snr", "approximant",
                ):
                    row[key] = original[key]
                row["achieved_network_snr"] = round(float(achieved), 3)
            row["generated_at"] = datetime.now(timezone.utc).isoformat()

            _append_jsonl(REPLACEMENT_TRUTH, row)
            replacement_truth[rid] = row
            replacement_index[rid] = gps
            _write_index(REPLACEMENT_INDEX, replacement_index)
            tried_gps.append(gps)
            message = f"{datetime.now(timezone.utc).isoformat()} generated {rid}\n"
            log.write(message)
            log.flush()
            os.fsync(log.fileno())
            print(f"generated {rid} ({len(replacement_truth)}/{len(excluded_ids)})", flush=True)

    print(f"replacement pool complete: {len(replacement_truth)} blind specs")


if __name__ == "__main__":
    main()
