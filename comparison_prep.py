"""Prepare a local development comparison; never import an AI client or the loop."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import random


DATA = Path.home() / "experiment-data/ligo"
SEED = 20261019
VERSION = "1.0.0"
ARMS = ("numbers_only", "numbers_and_images")
SYSTEM_PROMPT = """Assess whether an H1/L1 detector window merits further human investigation.
This is a controlled evaluation of recorded strain windows. Some may contain
simulated signals; others may contain only detector noise. You are never given
the answer. Apply the same standard to every case.

Only H1 and L1 are within the simulated detector network. Virgo, KAGRA, catalog
identities, gamma-ray alerts, and neutrino alerts are outside this evaluation.
Their absence is not evidence against a signal. Data-quality information is
usable only when its status is 'ok'; failed or skipped checks are missing evidence.

The numerical chirp_like flag compares the strongest frequencies in two halves
of the window. It does not trace a continuous chirp and may confuse noise with
a signal or miss a signal. energy_contrast is an excess-power measure, not
matched-filter SNR or a probability. A glitch and a weaker signal can coexist.
If images are supplied, examine both detectors; express conflicts and uncertainty.

Choose archive for a window with no evidence warranting more investigation;
glitch_candidate when the evidence favors an instrumental artifact without
enough reason for signal follow-up; follow_up when further signal validation is
warranted; candidate_for_human_review when evidence strongly merits human review.
Never claim a confirmed astronomical detection. These choices do not trigger
any real notification. interesting_score is a ranking score, not a probability.

Return JSON with decision (one of the four choices), interesting_score (0 to 1),
reasoning (2-3 sentences), and human_review_required (true for follow_up or
candidate_for_human_review)."""


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def select_cohort(rows, seed=SEED):
    """Balance labels/strengths without selecting on a detector or AI outcome."""
    if len({r["spec_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate graded spec IDs")
    rng = random.Random(seed)
    selected = []
    for low, high in ((4, 6), (6, 16), (16, 24)):
        pool = sorted((r for r in rows if r["kind"] == "injection" and low <= r["snr"]
                       and (r["snr"] < high or high == 24 and r["snr"] == 24)),
                      key=lambda r: r["spec_id"])
        if len(pool) < 5:
            raise ValueError("not enough injections in a strength group")
        selected.extend(rng.sample(pool, 5))
    controls = sorted((r for r in rows if r["kind"] == "noise"), key=lambda r: r["spec_id"])
    if len(controls) < 15:
        raise ValueError("not enough noise controls")
    selected.extend(rng.sample(controls, 15))
    rng.shuffle(selected)
    return selected


def summarize_transform(energy, frequencies, relative_times):
    import numpy as np

    energy, frequencies, times = map(np.asarray, (energy, frequencies, relative_times))
    if (energy.ndim != 2 or energy.shape != (len(times), len(frequencies))
            or len(times) < 2 or not len(frequencies)
            or not all(np.isfinite(a).all() for a in (energy, frequencies, times))):
        raise ValueError("invalid Q-transform")
    middle = len(times) // 2
    early = float(frequencies[np.argmax(np.max(energy[:middle], axis=0))])
    late = float(frequencies[np.argmax(np.max(energy[middle:], axis=0))])
    peak = np.unravel_index(np.argmax(energy), energy.shape)
    contrast = float(energy.max() / (np.median(energy) + 1e-10))
    return {
        "energy_contrast": contrast, "signal_detected": bool(contrast >= 5),
        "peak_frequency_hz": float(frequencies[peak[1]]),
        "peak_time_offset_s": float(times[peak[0]]),
        "freq_early_hz": early, "freq_late_hz": late,
        "freq_sweep_hz_per_s": (late - early) / 2,
        "chirp_like": bool((late - early) / 2 >= 10),
    }


def build_summary(measurements, primary, record):
    """Use an allowlist so source IDs, labels, prior AI outputs cannot leak."""
    metric_keys = ("energy_contrast", "signal_detected", "peak_frequency_hz", "peak_time_offset_s",
                   "freq_early_hz", "freq_late_hz", "freq_sweep_hz_per_s", "chirp_like")
    detectors = {detector: {key: measurements[detector][key] for key in metric_keys}
                 for detector in ("H1", "L1")}
    if primary not in detectors:
        raise ValueError("comparison is limited to H1 and L1")
    second = "L1" if primary == "H1" else "H1"
    a, b = detectors[primary], detectors[second]
    frequency_agreement = abs(a["peak_frequency_hz"] - b["peak_frequency_hz"]) / (
        a["peak_frequency_hz"] + 1e-10) <= 0.3
    difference = abs(a["peak_time_offset_s"] - b["peak_time_offset_s"])
    timing_agreement = difference <= 0.015
    checked = a["signal_detected"]
    coincidence = {
        "checked": checked, "second_detector": second,
        "freq_agreement": frequency_agreement if checked else None,
        "dt_peak_s": difference if checked else None,
        "dt_agreement": timing_agreement if checked else None,
        "coincident": bool(checked and b["energy_contrast"] >= 4
                           and frequency_agreement and timing_agreement),
    }
    dq = (record.get("crossmatch") or {}).get("data_quality") or {}
    # Keep only the original primary detector's check; do not invent L1/H1 checks.
    data_quality = {"detector": primary, "status": dq.get("status", "skipped")}
    for key in ("data_usable", "cat1_active", "cat2_active"):
        data_quality[key] = dq.get(key) if data_quality["status"] == "ok" else None
    return {"analyzed_seconds": 4, "primary_detector": primary, "detectors": detectors,
            "coincidence": coincidence, "data_quality": data_quality}


def model_requests(case_id, summary):
    return [{"case_id": case_id, "arm": arm, "system_prompt": SYSTEM_PROMPT,
             "summary": summary,
             "image_paths": [f"images/{case_id}.png"] if arm == "numbers_and_images" else []}
            for arm in ARMS]


def render_case(pool_path, primary, record, case_id, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from gwpy.timeseries import TimeSeries

    with np.load(pool_path, allow_pickle=False) as saved:
        gps, rate = float(saved["gps"]), float(saved["sample_rate"])
        arrays = {key: saved[key] for key in ("H1", "L1", "inj_H1", "inj_L1")}
    if rate != 4096 or any(a.shape != (32 * 4096,) or not np.isfinite(a).all() for a in arrays.values()):
        raise ValueError("invalid saved strain arrays")
    measurements = {}
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.7), layout="constrained")
    for detector, ax in zip(("H1", "L1"), axes):
        series = TimeSeries(arrays[detector] + arrays[f"inj_{detector}"], sample_rate=rate, t0=gps - 16)
        q = series.q_transform(qrange=(4, 64), frange=(20, 2048), outseg=(gps - 2, gps + 2))
        times, frequencies = q.times.value - gps, q.frequencies.value
        measurements[detector] = summarize_transform(q.value, frequencies, times)
        mesh = ax.pcolormesh(times, frequencies, q.value.T, shading="auto", cmap="viridis")
        ax.set(yscale="log", ylim=(float(frequencies[0]), float(frequencies[-1])), xlim=(-2, 2),
               xlabel="Time relative to window midpoint [s]", ylabel="Frequency [Hz]", title=detector)
        fig.colorbar(mesh, ax=ax, label="Normalized energy")
    fig.suptitle(case_id)
    fig.savefig(output / "images" / f"{case_id}.png", dpi=120)
    plt.close(fig)
    return build_summary(measurements, primary, record)


def prepare(output, data=DATA):
    if output.exists():
        raise ValueError("choose a new output directory")
    scored = data / "analysis/campaign-score-v1"
    for line in (scored / "SHA256SUMS.txt").read_text().splitlines():
        expected, name = line.split("  ", 1)
        if Path(name).name != name or digest(scored / name) != expected:
            raise ValueError("graded output failed its integrity check")
    result = json.loads((scored / "results.json").read_text())
    if result["demo"] or not result["input_snapshot"]["truth_values_opened"]:
        raise ValueError("requires the completed real grading")
    records_path = data / "campaign_2026_07.jsonl"
    if digest(records_path) != result["input_snapshot"]["records_sha256"]:
        raise ValueError("campaign records changed after grading")
    raw_rows = read_rows(records_path)
    raw = {r["injection"]["spec_id"]: r for r in raw_rows}
    rows = read_rows(scored / "scored_records.jsonl")
    if len(raw) != len(raw_rows) or len(rows) != 300 or any(r["spec_id"] not in raw for r in rows):
        raise ValueError("invalid campaign roster")
    selected = select_cohort(rows)
    sources = {"campaign_records": digest(records_path), "graded_results": digest(scored / "results.json"),
               "graded_rows": digest(scored / "scored_records.jsonl"),
               "comparison_prep.py": digest(Path(__file__))}
    pool_paths = {}
    for row in selected:
        sid = row["spec_id"]
        replacement = sid.startswith("replacement_")
        root = "campaign_replacements_2026_07" if replacement else "campaign"
        pool = data / root / "pool" / f"{sid}.npz"
        seal = result["input_snapshot"]["sealed_artifacts"]["replacement" if replacement else "original"]
        if digest(pool) != seal[f"pool/{sid}.npz"]:
            raise ValueError("selected pool file changed after grading")
        pool_paths[sid] = pool
        sources[f"{root}/pool/{sid}.npz"] = digest(pool)
    (output / "images").mkdir(parents=True, exist_ok=False)
    requests, answers = [], []
    baseline = {kind: {"total": 0, "flagged": 0} for kind in ("injection", "noise")}
    for number, row in enumerate(selected, 1):
        case_id, sid = f"case_{number:03d}", row["spec_id"]
        record = raw[sid]
        summary = render_case(pool_paths[sid], record["detector"], record, case_id, output)
        requests.extend(model_requests(case_id, summary))
        caught = summary["detectors"][record["detector"]]["chirp_like"] or summary["coincidence"]["coincident"]
        baseline[row["kind"]]["total"] += 1
        baseline[row["kind"]]["flagged"] += caught
        answers.append({"case_id": case_id, "spec_id": sid, "kind": row["kind"], "snr": row["snr"],
                        "replayed_physics_caught": caught, "original_primary": row["primary"],
                        "original_secondary": row["secondary"], "original_versions": row["versions"]})
        print(f"Prepared {number}/30", flush=True)
    for name, contents in (("model_inputs.jsonl", requests), ("private_answer_key.jsonl", answers)):
        (output / name).write_text("".join(json.dumps(r, allow_nan=False) + "\n" for r in contents))
    (output / "physics_baseline.json").write_text(json.dumps(baseline, indent=2) + "\n")
    protocol = {
        "version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(), "seed": SEED,
        "status": "prepared_only_no_AI_calls", "exploratory_after_unblinding": True,
        "cohort": {"injections": 15, "controls": 15, "injection_strengths": "five each: [4,6), [6,16), [16,24]"},
        "selection": "seeded random samples sorted by spec ID; no AI or detector outcomes used",
        "arms": list(ARMS), "requests_per_model": len(requests),
        "candidate_models": ["claude-sonnet-4-6", "claude-sonnet-5-5"],
        "model_access_and_cost_checked": False,
        "interpretation": "Development only; old examples are already open. Compare models on identical new inputs. "
                          "Differences from archived decisions also change prompt, detector context, image presentation, "
                          "and local numerical replay; they do not isolate model improvements. "
                          "Use a fresh held-out campaign for independent validation.",
        "reconstruction": "Saved noise plus injection in H1/L1; 32s at 4096Hz, analyzed central 4s, "
                          "Q range 4-64, frequency range requested 20-2048Hz (GWpy may lower its upper limit). "
                          "Epoch assumed gps-16. Not an exact replay of fetched strain. "
                          "The original two-half chirp heuristic and H1/L1 coincidence thresholds are retained.",
        "source_sha256": sources,
        "runtime": {name: importlib.metadata.version(name) for name in ("numpy", "gwpy", "matplotlib")},
    }
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    (output / "README.md").write_text(
        "# Small corrected comparison, ready for review\n\n"
        "30 saved examples: 15 with added signals and 15 controls. No paid calls have been made.\n\n"
        "## What changed\n\n"
        "The AI receives H1/L1 measurements and, in one arm, their images. Other detector and external-alert "
        "evidence is outside the test. No GPS times, labels, signal strengths, prior AI decisions, or original "
        "spec IDs appear in model_inputs.jsonl or image titles. Both arms use the same prompt and measurements.\n\n"
        "## Files\n\n"
        "- model_inputs.jsonl: 60 requests, two per example. Only the image arm includes image paths.\n"
        "- images/: the 30 corresponding two-detector images, with neutral case IDs.\n"
        "- private_answer_key.jsonl: answers and original IDs. Keep this OUT of model requests.\n"
        "- physics_baseline.json: local numerical-rule comparison on these reconstructed inputs.\n"
        "- protocol.json: selection, method, source hashes, and software versions.\n\n"
        "## What happens next\n\n"
        "Check model availability and a cost estimate before submitting requests. Running both proposed models "
        "on both arms would make 120 requests. No API client, credentials, submission, or scheduler is included. "
        "Batch processing can be used by a later runner where supported.\n\n"
        "This is a development sample from answers already opened, not independent validation. Changes include "
        "prompt, detector context, numerical reconstruction, and paired detector images. A comparison with "
        "the old saved decisions alone cannot identify which change helped. The final evaluation needs new "
        "hidden examples after the method is fixed. Original campaign results stay preserved.\n")
    completion = json.dumps({"complete": True, "version": VERSION, "AI_calls": 0}) + "\n"
    paths = sorted(p for p in output.rglob("*") if p.is_file())
    manifest = [f"{digest(p)}  {p.relative_to(output)}\n" for p in paths]
    manifest.append(f"{hashlib.sha256(completion.encode()).hexdigest()}  COMPLETE.json\n")
    (output / "SHA256SUMS.txt").write_text("".join(manifest))
    (output / "COMPLETE.json").write_text(completion)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.output.expanduser())
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(1, f"Preparation stopped: {exc}\n")
    print(f"Ready for review: {args.output.expanduser()}. No AI calls were made.")


if __name__ == "__main__":
    main()
