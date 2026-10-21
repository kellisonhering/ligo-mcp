#!/usr/bin/env python3
"""Exploratory inspection of an already graded campaign; local files only."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path


DATA = Path.home() / "experiment-data/ligo"
CASES = ("spec_0260", "spec_0262", "spec_0243", "replacement_spec_0009", "spec_0214")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def reconstruct(spec_id, graded, record, pool, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from gwpy.timeseries import TimeSeries

    with np.load(pool / f"{spec_id}.npz", allow_pickle=False) as saved:
        gps, rate = float(saved["gps"]), float(saved["sample_rate"])
        arrays = {key: saved[key] for key in ("H1", "L1", "inj_H1", "inj_L1")}
    fig, axes = plt.subplots(2, 2, figsize=(12, 7), layout="constrained")
    measurements = {}
    for row, detector in enumerate(("H1", "L1")):
        transforms = []
        for values in (arrays[detector], arrays[detector] + arrays[f"inj_{detector}"]):
            series = TimeSeries(values, sample_rate=rate, t0=gps - 16)
            transforms.append(series.q_transform(qrange=(4, 64), frange=(20, 2048),
                                                 outseg=(gps - 2, gps + 2)))
        maximum = max(float(q.value.max()) for q in transforms)
        for column, q in enumerate(transforms):
            ax = axes[row, column]
            mesh = ax.pcolormesh(q.times.value - gps, q.frequencies.value, q.value.T,
                                 shading="auto", cmap="viridis", vmin=0, vmax=maximum)
            ax.set(yscale="log", ylim=(float(q.frequencies.value[0]), float(q.frequencies.value[-1])),
                   xlim=(-2, 2), xlabel="Time relative to injection center [s]", ylabel="Frequency [Hz]",
                   title=f"{detector}: " + ("saved noise only" if column == 0 else "noise + added signal"))
            fig.colorbar(mesh, ax=ax, label="Normalized energy")
        q = transforms[1]
        energy, frequencies = q.value, q.frequencies.value
        middle = len(energy) // 2
        early = float(frequencies[np.argmax(np.max(energy[:middle], axis=0))])
        late = float(frequencies[np.argmax(np.max(energy[middle:], axis=0))])
        peak = np.unravel_index(np.argmax(energy), energy.shape)
        measurements[detector] = {
            "freq_early_hz": early, "freq_late_hz": late,
            "freq_sweep_hz_per_s": (late - early) / 2,
            "chirp_like": bool((late - early) / 2 >= 10),
            "peak_frequency_hz": float(frequencies[peak[1]]),
            "peak_time_offset_s": float(q.times.value[peak[0]]) - gps,
        }
    fig.suptitle(f"Exploratory reconstruction: {spec_id}, network optimal SNR {graded['snr']:.3f}")
    fig.savefig(output / f"{spec_id}_comparison.png", dpi=140)
    plt.close(fig)
    primary = record["detector"]
    measurements["comparison_with_recorded_primary"] = {
        "detector": primary,
        "recorded": {key: record["detection"][key] for key in measurements[primary]},
        "reconstructed": measurements[primary],
    }
    return measurements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new output directory")
    scored = DATA / "analysis/campaign-score-v1"
    for line in (scored / "SHA256SUMS.txt").read_text().splitlines():
        expected, name = line.split("  ", 1)
        if Path(name).name != name or digest(scored / name) != expected:
            raise ValueError("graded output failed its integrity check")
    result = json.loads((scored / "results.json").read_text())
    if result["demo"] or not result["input_snapshot"]["truth_values_opened"]:
        raise ValueError("requires completed real grading")
    records_path = DATA / "campaign_2026_07.jsonl"
    if digest(records_path) != result["input_snapshot"]["records_sha256"]:
        raise ValueError("campaign records changed after grading")
    raw_rows = read_rows(records_path)
    raw = {r["injection"]["spec_id"]: r for r in raw_rows}
    graded = {r["spec_id"]: r for r in read_rows(scored / "scored_records.jsonl")}
    if len(raw) != len(raw_rows) or len(graded) != 300 or not graded.keys() <= raw.keys():
        raise ValueError("unexpected duplicate or incomplete roster")
    injections = [sid for sid, row in graded.items() if row["kind"] == "injection"]
    controls_flagged = [sid for sid, row in graded.items() if row["kind"] == "noise" and row["primary"]]
    coincident = [sid for sid in injections if raw[sid]["coincidence"]["coincident"] is True]
    strong = [sid for sid in injections if graded[sid]["snr"] >= 20]
    available_misses = [sid for sid in injections if graded[sid]["available_call_eligible"]
                        and not graded[sid]["secondary"]]
    model_fields = ("planner_model", "vision_model", "selector_model")
    evidence = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "exploratory_after_unblinding": True,
        "methods": "Saved records and two saved pool reconstructions; no AI calls or downloads. "
                   "Cases were selected after seeing outcomes and do not estimate general performance. "
                   "Reasoning mentions are text observations, not causal ablations. "
                   "Reconstruction uses the saved noise epoch gps-16; each Q-transform normalizes itself. "
                   "Colors share a scale within each detector row. Original grades remain authoritative.",
        "source_sha256": {
            "records": digest(records_path), "graded_results": digest(scored / "results.json"),
            "graded_rows": digest(scored / "scored_records.jsonl"),
            "inspection_script": digest(Path(__file__)),
        },
        "runtime": {name: importlib.metadata.version(name) for name in ("numpy", "gwpy", "matplotlib")},
        "recorded_models": {field: dict(Counter(raw[sid]["versions"][field] for sid in graded))
                            for field in model_fields},
        "injection_decisions": dict(Counter(raw[sid]["llm_review"]["decision"] for sid in injections)),
        "injection_vision_types": dict(Counter((raw[sid].get("vision") or {}).get("likely_signal_type", "unavailable")
                                              for sid in injections)),
        "coincident_injections": {"total": len(coincident),
                                  "escalated": sum(graded[sid]["secondary"] for sid in coincident)},
        "strong_injections_snr_at_least_20": {"total": len(strong),
                                             "primary": sum(graded[sid]["primary"] for sid in strong),
                                             "escalated": sum(graded[sid]["secondary"] for sid in strong)},
        "control_false_alarms": {"total": len(controls_flagged),
                                 "chirp_like": sum(raw[sid]["detection"]["chirp_like"] for sid in controls_flagged),
                                 "coincident": sum(raw[sid]["coincidence"]["coincident"] is True for sid in controls_flagged)},
        "available_injection_misses": len(available_misses),
        "available_injection_misses_mentioning_virgo": sum(
            "virgo" in raw[sid]["llm_review"]["reasoning"].lower() for sid in available_misses),
        "cases": {sid: {"grade": graded[sid], "detection": raw[sid]["detection"],
                        "coincidence": raw[sid]["coincidence"], "vision": raw[sid]["vision"],
                        "llm_review": raw[sid]["llm_review"], "plot_path": raw[sid]["plot_path"]}
                  for sid in CASES},
    }
    for sid in ("spec_0260", "spec_0262"):
        pool_path = DATA / "campaign/pool" / f"{sid}.npz"
        expected = result["input_snapshot"]["sealed_artifacts"]["original"][f"pool/{sid}.npz"]
        if digest(pool_path) != expected:
            raise ValueError("selected pool file changed after grading")
        evidence["source_sha256"][f"pool/{sid}.npz"] = expected
    args.output.mkdir(parents=True, exist_ok=False)
    for sid in ("spec_0260", "spec_0262"):
        evidence["cases"][sid]["reconstruction"] = reconstruct(
            sid, graded[sid], raw[sid], DATA / "campaign/pool", args.output)
    (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2, allow_nan=False) + "\n")
    print(f"Saved exploratory evidence and two comparison charts: {args.output}")


if __name__ == "__main__":
    main()
