"""
Name-ablation A/B/C experiment (REVIEW_FINDINGS.md §7.3 A4) — measures how much
the answer key inflates the planner's scores on famous benchmark events.

The planner is normally HANDED the event name + GPS time for known events. This
experiment rebuilds each benchmark's summary from its stored record (no strain
downloads, no vision calls — those results are already in the record) and runs
the planner three ways:

  Arm A — full summary, exactly what the planner normally sees (name + GPS)
  Arm B — catalog fields neutralized (known_event_match=False, no name/type/run):
          "what would it say if the catalog had missed this event?"
  Arm C — Arm B + GPS time shifted (+13,000,000 s, breaks memorized-timestamp
          recognition) + Fermi trigger name masked (bn170817529 contains the
          famous date): "pure data-driven judgment, no recognizable identifiers"

All arms use the CURRENT planner prompt at temperature 0, so the ONLY variable
between arms is the ablated identifiers. Score(A) - Score(C) = answer-key inflation.

Writes to ~/experiment-data/ligo/analysis/ — deliberately NOT experiments.jsonl
(these are runs ON the pipeline, not pipeline runs).
"""
import copy
import json
import os
from datetime import datetime, timezone

import loop  # noqa: F401 — imported for its side effect: loads the API key env
from planner import run_planner, PLANNER_MODEL
from loop import PLANNER_PROMPT_VERSION

EXPERIMENTS_FILE = os.path.expanduser("~/experiment-data/ligo/experiments.jsonl")
ANALYSIS_DIR = os.path.expanduser("~/experiment-data/ligo/analysis")
GPS_SHIFT_S = 13_000_000  # ~150 days; breaks string-recognition of famous GPS times

EVENTS = ["GW150914", "GW170817", "GW190521", "GW151226", "GW170814"]


def latest_benchmark_records() -> dict:
    """Most recent record per famous event."""
    best = {}
    with open(EXPERIMENTS_FILE) as f:
        for line in f:
            r = json.loads(line)
            name = r.get("catalog", {}).get("event_name")
            if name in EVENTS:
                best[name] = r  # later lines overwrite: file is append-ordered
    return best


def recompute_confidence(r: dict) -> float:
    """v3 discriminator confidence from stored fields (works for v2 records too)."""
    d, c, dq = r["detection"], r["coincidence"], r["crossmatch"]["data_quality"]
    if not d["signal_detected"]:
        return 0.0
    score = 0.10
    if d["chirp_like"]:
        score += 0.30
    if c.get("coincident"):
        score += 0.35
        if c.get("triple_coincident"):
            score += 0.10
        if c.get("quad_coincident"):
            score += 0.05
    if dq.get("status") == "ok" and dq.get("data_usable"):
        score += 0.05
    if dq.get("status") == "ok" and dq.get("cat2_active"):
        score -= 0.15
    return round(min(1.0, max(0.0, score)), 3)


def build_summary(r: dict) -> dict:
    """Rebuild the planner summary from a stored record, in current v3 naming."""
    d, c, cat = r["detection"], r["coincidence"], r["catalog"]
    fermi = r["crossmatch"]["fermi"]
    dq = r["crossmatch"]["data_quality"]
    snews = r["crossmatch"]["snews"]
    ic = r["crossmatch"]["icecube"]
    vision = r.get("vision") or {}

    summary = {
        "gps_time": r["gps_time"],
        "detector": r["detector"],
        "subsolar_mode": r.get("subsolar_mode", False),
        "signal_detected": d["signal_detected"],
        "peak_frequency_hz": d["peak_frequency_hz"],
        "peak_time_offset_s": d["peak_time_offset_s"],
        "energy_contrast": d.get("energy_contrast", d.get("snr")),
        "confidence_score": recompute_confidence(r),
        "classification_hint": d["classification_hint"],
        "freq_early_hz": d["freq_early_hz"],
        "freq_late_hz": d["freq_late_hz"],
        "freq_sweep_hz_per_s": d["freq_sweep_hz_per_s"],
        "chirp_like": d["chirp_like"],
        "coincidence_checked": c["checked"],
        "coincident": c["coincident"],
        "coincidence_second_detector": c["second_detector"],
        "coincidence_second_energy_contrast": c.get("second_energy_contrast", c.get("second_snr")),
        "coincidence_freq_agreement": c["freq_agreement"],
        "coincidence_dt_peak_s": c.get("dt_peak_s"),
        "coincidence_dt_agreement": c.get("dt_agreement"),
        "virgo_checked": c.get("virgo_checked", False),
        "virgo_energy_contrast": c.get("virgo_energy_contrast", c.get("virgo_snr")),
        "virgo_dt_peak_s": c.get("virgo_dt_peak_s"),
        "virgo_coincident": c.get("virgo_coincident"),
        "triple_coincident": c.get("triple_coincident", False),
        "kagra_checked": c.get("kagra_checked", False),
        "kagra_energy_contrast": c.get("kagra_energy_contrast", c.get("kagra_snr")),
        "kagra_dt_peak_s": c.get("kagra_dt_peak_s"),
        "kagra_coincident": c.get("kagra_coincident"),
        "quad_coincident": c.get("quad_coincident", False),
        "fermi_trigger_found": fermi["trigger_found"],
        "fermi_trigger_name": fermi["trigger_name"],
        "fermi_offset_s": fermi["trigger_time_offset_s"],
        "fermi_classification": fermi["classification"],
        "fermi_t90_s": fermi["t90_s"],
        "fermi_status": fermi.get("status", "ok"),
        "dq_data_usable": dq["data_usable"],
        "dq_cat1_active": dq["cat1_active"],
        "dq_cat2_active": dq["cat2_active"],
        "dq_flags_found": dq["flags_found"],
        "dq_has_data": dq.get("has_data"),
        "dq_status": dq.get("status", "ok"),
        "snews_alert_found": snews["alert_found"],
        "snews_alert_id": snews["alert_id"],
        "snews_alert_offset_s": snews["alert_time_offset_s"],
        "snews_status": snews.get("status", "ok"),
        "icecube_alert_found": ic["alert_found"],
        "icecube_event_id": ic["event_id"],
        "icecube_alert_offset_s": ic["alert_time_offset_s"],
        "icecube_signalness": ic["signalness"],
        "icecube_stream": ic["stream"],
        "icecube_status": ic.get("status", "ok"),
        "known_event_match": cat["known_event_match"],
        "event_name": cat["event_name"],
        "event_type": cat["event_type"],
        "time_offset_s": cat["time_offset_s"],
        "observing_run": cat["observing_run"],
        "is_subsolar": cat["is_subsolar"],
        "is_pbh_candidate": cat["is_pbh_candidate"],
    }
    if vision:
        summary["vision_shape"] = vision.get("shape_classification")
        summary["vision_signal_type"] = vision.get("likely_signal_type")
        summary["vision_confidence"] = vision.get("confidence")
        summary["vision_score_modifier"] = vision.get("score_modifier")
        summary["vision_reasoning"] = vision.get("reasoning")
    return summary


def arm_b(summary: dict) -> dict:
    """Catalog fields neutralized — looks like an unrecognized event."""
    s = copy.deepcopy(summary)
    s["known_event_match"] = False
    s["event_name"] = None
    s["event_type"] = None
    s["time_offset_s"] = None
    s["observing_run"] = None
    return s


def arm_c(summary: dict) -> dict:
    """Arm B + GPS shifted + Fermi trigger name masked — no recognizable identifiers."""
    s = arm_b(summary)
    s["gps_time"] = round(s["gps_time"] + GPS_SHIFT_S, 1)
    if s.get("fermi_trigger_name"):
        s["fermi_trigger_name"] = "trigger_masked_1"  # found+offset stay: real evidence
    return s


def main():
    os.makedirs(ANALYSIS_DIR, exist_ok=True)
    records = latest_benchmark_records()
    missing = [e for e in EVENTS if e not in records]
    if missing:
        print(f"WARNING: no benchmark record for {missing}")

    results = []
    for event in EVENTS:
        if event not in records:
            continue
        r = records[event]
        base = build_summary(r)
        arms = {"A_full": base, "B_no_name": arm_b(base), "C_no_identifiers": arm_c(base)}

        row = {
            "event": event,
            "source_record": r["experiment_id"],
            "source_schema_version": r.get("schema_version"),
            "stored_score_historical": r["llm_review"]["interesting_score"],
        }
        for arm_name, summary in arms.items():
            res = run_planner(summary)
            row[arm_name] = {
                "decision": res.decision,
                "interesting_score": res.interesting_score,
                "reasoning": res.reasoning,
                "error": res.error,
            }
            print(f"{event:10s} {arm_name:17s} score={res.interesting_score:<5} {res.decision}")
        results.append(row)

    out = {
        "experiment": "name_ablation",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "planner_model": PLANNER_MODEL,
        "planner_prompt_version": PLANNER_PROMPT_VERSION,
        "gps_shift_s": GPS_SHIFT_S,
        "design": "A = full summary; B = catalog stripped; C = B + GPS shifted + Fermi name masked. Temperature 0; only the identifiers vary between arms.",
        "results": results,
    }
    out_path = os.path.join(ANALYSIS_DIR, "name_ablation_20260710.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {out_path}")

    print("\n=== ANSWER-KEY INFLATION (A minus C) ===")
    for row in results:
        a = row["A_full"]["interesting_score"]
        b = row["B_no_name"]["interesting_score"]
        c = row["C_no_identifiers"]["interesting_score"]
        print(f"  {row['event']:10s} A={a:<5} B={b:<5} C={c:<5}  inflation={round(a - c, 3)}")


if __name__ == "__main__":
    main()
