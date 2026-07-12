"""
Pre-generate the campaign injection pool (§7.5 rules 1-3).

RUN UNDER THE INJECTION VENV:
    ~/venvs/ligo-injections/bin/python injection_pool_generator.py

For each of 200 injection specs:
  - draw random BBH masses (uniform 10-40 Msun; total 20-80 to fit the 4s window)
  - draw a target network SNR from a stratified distribution
    (20% invisible, 60% marginal — the science, 20% obvious)
  - pick a random O3 science window (excluding ±64 s of GWTC catalog events)
  - fetch H1+L1 32 s strain and measure its PSD
  - generate the waveform and project onto both detectors (proper Δt + antenna)
  - scale the injection so its network SNR against the MEASURED noise = target
  - save (H1 strain, L1 strain, H1 injection, L1 injection, truth) as an .npz
  - append full truth to campaign_truth.jsonl (SEALED — do not open until scoring)

Idempotent: skips specs already on disk. Uses a fixed seed for reproducibility.

Also generates 100 pure-noise controls: same window selection, no waveform.
Structure: 300 spec ids total; ~2/3 injections, ~1/3 controls, shuffled by id.
"""
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np

# add repo to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from injections import generate_bbh, project_into_window, measured_psd, optimal_snr, network_snr

# ---- config (frozen at lock time) ----
POOL_DIR = os.path.expanduser("~/experiment-data/ligo/campaign/pool")
TRUTH_FILE = os.path.expanduser("~/experiment-data/ligo/campaign/campaign_truth.jsonl")
POOL_INDEX = os.path.expanduser("~/experiment-data/ligo/campaign/pool_index.json")
SEED = 20260711
N_INJECTIONS = 200
N_CONTROLS   = 100

# strain sampling
SAMPLE_RATE = 4096
FETCH_SECONDS = 32          # matches pipeline.py W3 fetch window

# BBH mass range (§7.5 rule 2)
M_LO, M_HI = 10.0, 40.0

# SNR strata (§7.5 rule 2 — 20/60/20 split)
STRATA = [
    (4.0,  6.0,  0.20),
    (6.0,  16.0, 0.60),
    (16.0, 24.0, 0.20),
]

# O3 science-time bounds and catalog-event exclusion (§7.5 rule 3)
O3_START, O3_END = 1238166018, 1269363618
EXCLUDE_S = 64.0

# --- helpers ---

def load_catalog_events():
    """Get GWTC event GPS times to exclude from noise-window draws."""
    from catalog import _load_runtime_catalog
    cat = _load_runtime_catalog()
    return sorted(ev["gps"] for ev in cat.values() if ev.get("gps"))


def draw_snr(rng):
    p = rng.random()
    acc = 0.0
    for lo, hi, w in STRATA:
        acc += w
        if p < acc:
            return float(rng.uniform(lo, hi))
    return float(rng.uniform(STRATA[-1][0], STRATA[-1][1]))


def pick_noise_window(rng, excluded, tried_gps):
    """Return a random O3 GPS >= EXCLUDE_S from any catalog event and not tried before."""
    for _ in range(500):
        gps = float(rng.uniform(O3_START + FETCH_SECONDS, O3_END - FETCH_SECONDS))
        if any(abs(gps - e) < EXCLUDE_S for e in excluded):
            continue
        if any(abs(gps - t) < FETCH_SECONDS for t in tried_gps):
            continue
        return gps
    return None


def try_fetch(gps):
    """Return dict of {H1,L1: gwpy TimeSeries} for FETCH_SECONDS around gps, or None."""
    from gwpy.timeseries import TimeSeries
    start, end = gps - FETCH_SECONDS / 2, gps + FETCH_SECONDS / 2
    out = {}
    for det in ("H1", "L1"):
        try:
            ts = TimeSeries.fetch_open_data(det, start, end, cache=True)
            if ts is None or len(ts) == 0:
                return None
            out[det] = ts
        except Exception:
            return None
    return out


# --- main ---

def main():
    os.makedirs(POOL_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(TRUTH_FILE), exist_ok=True)

    excluded = load_catalog_events()
    print(f"Excluding {len(excluded)} GWTC events (±{EXCLUDE_S}s each).")

    rng = np.random.default_rng(SEED)

    # deterministic order: mix injections + controls, shuffle by seed
    kinds = ["injection"] * N_INJECTIONS + ["noise"] * N_CONTROLS
    rng.shuffle(kinds)

    truth_index = {}
    if os.path.exists(POOL_INDEX):
        truth_index = json.load(open(POOL_INDEX))
        print(f"Resuming: {len(truth_index)} specs already in pool.")

    tried_gps = list(truth_index.values())  # we'll extend below with each new gps

    truth_out = open(TRUTH_FILE, "a")

    for i, kind in enumerate(kinds):
        spec_id = f"spec_{i:04d}"
        if spec_id in truth_index:
            continue

        # draw a valid noise window
        for attempt in range(5):
            gps = pick_noise_window(rng, excluded, [t["gps"] for t in tried_gps]
                                    if tried_gps and isinstance(tried_gps[0], dict) else tried_gps)
            if gps is None:
                print(f"  {spec_id}: no candidate window found"); break
            data = try_fetch(gps)
            if data is not None:
                break
            print(f"  {spec_id}: no data at gps {gps:.1f}, retrying")
        if data is None:
            print(f"  {spec_id}: giving up (no available data windows)"); continue

        H1 = np.array(data["H1"].value, dtype=np.float64)
        L1 = np.array(data["L1"].value, dtype=np.float64)
        n_samples = len(H1)

        truth = {
            "spec_id": spec_id, "kind": kind, "gps_time": round(gps, 4),
            "seed": int(rng.integers(0, 2**31 - 1)),
        }

        if kind == "injection":
            m1 = float(rng.uniform(M_LO, M_HI))
            m2 = float(rng.uniform(M_LO, M_HI))
            ra = float(rng.uniform(0, 2 * np.pi))
            dec = float(np.arcsin(rng.uniform(-1, 1)))     # uniform in solid angle
            psi = float(rng.uniform(0, np.pi))
            iota = float(np.arccos(rng.uniform(-1, 1)))    # uniform in cos(iota)
            phi = float(rng.uniform(0, 2 * np.pi))
            target_snr = draw_snr(rng)

            hp, hc = generate_bbh(m1, m2, distance_mpc=400.0, inclination=iota, coa_phase=phi)
            fetch_start = gps - FETCH_SECONDS / 2
            inj_H1 = project_into_window(hp, hc, "H1", ra, dec, psi, gps, fetch_start, n_samples)
            inj_L1 = project_into_window(hp, hc, "L1", ra, dec, psi, gps, fetch_start, n_samples)

            psd_H1 = measured_psd(data["H1"])
            psd_L1 = measured_psd(data["L1"])
            snr_H1_raw = optimal_snr(inj_H1, psd_H1, FETCH_SECONDS)
            snr_L1_raw = optimal_snr(inj_L1, psd_L1, FETCH_SECONDS)
            net_raw = network_snr({"H1": snr_H1_raw, "L1": snr_L1_raw})
            if net_raw < 1e-6:
                print(f"  {spec_id}: zero unscaled SNR, skipping"); continue
            scale = target_snr / net_raw
            inj_H1 *= scale
            inj_L1 *= scale
            achieved = network_snr({"H1": optimal_snr(inj_H1, psd_H1, FETCH_SECONDS),
                                    "L1": optimal_snr(inj_L1, psd_L1, FETCH_SECONDS)})

            truth.update({
                "mass1": m1, "mass2": m2, "ra": ra, "dec": dec, "polarization": psi,
                "inclination": iota, "coa_phase": phi,
                "target_network_snr": round(target_snr, 3),
                "achieved_network_snr": round(achieved, 3),
                "approximant": "IMRPhenomD",
            })
            np.savez_compressed(os.path.join(POOL_DIR, f"{spec_id}.npz"),
                                H1=H1, L1=L1, inj_H1=inj_H1, inj_L1=inj_L1,
                                sample_rate=SAMPLE_RATE, gps=gps)
            print(f"  {spec_id} injection m={m1:.1f}+{m2:.1f} tgt_snr={target_snr:.1f} achieved={achieved:.2f}")
        else:  # noise control
            np.savez_compressed(os.path.join(POOL_DIR, f"{spec_id}.npz"),
                                H1=H1, L1=L1,
                                inj_H1=np.zeros_like(H1), inj_L1=np.zeros_like(L1),
                                sample_rate=SAMPLE_RATE, gps=gps)
            print(f"  {spec_id} noise-only control @ {gps:.1f}")

        truth["generated_at"] = datetime.now(timezone.utc).isoformat()
        truth_out.write(json.dumps(truth) + "\n"); truth_out.flush()
        truth_index[spec_id] = gps
        tried_gps.append(gps)
        json.dump(truth_index, open(POOL_INDEX, "w"), indent=1)

    truth_out.close()
    print(f"\nPool complete: {len(truth_index)} specs in {POOL_DIR}")
    print(f"Truth (SEALED): {TRUTH_FILE}")


if __name__ == "__main__":
    main()
