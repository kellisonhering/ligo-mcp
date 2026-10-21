"""Offline grading for the locked July 2026 campaign. No file or API access."""
from __future__ import annotations

import json
import math
from collections import Counter
from statistics import NormalDist


SCORER_VERSION = "1.1.0"
SNR_EDGES = tuple(range(4, 25, 2))
CONFIDENCE_THRESHOLDS = (0.3, 0.45, 0.6)
ESCALATIONS = {"follow_up", "candidate_for_human_review"}
DECISIONS = ESCALATIONS | {"archive", "benchmark_validated", "glitch_candidate", "rerun"}
RULES = {
    "primary": "detection.chirp_like is True OR coincidence.coincident is True",
    "secondary": "llm_review.decision in {follow_up, candidate_for_human_review}",
    "truth_source": "sealed truth kind; injection.injected is never a signal label",
    "snr_axis": "achieved_network_snr against the measured noise PSD",
    "snr_bins": "[4,6), [6,8), ..., [22,24]; final upper edge included",
    "intervals": "95% Wilson binomial intervals; empty groups are unavailable",
    "snr50": "linear interpolation between adjacent nonempty bin centers; all crossings shown",
    "comparison": "paired LLM secondary vs recomputed physics primary; API-error pairs excluded",
    "available_call_comparison": "supplementary paired comparison also excluding pipeline-skipped AI calls",
    "calibration": "five fixed score bins of width 0.2; unavailable LLM calls excluded",
    "false_alarm_units": "fraction of noise-control windows, not events per year",
}


class ScoringError(ValueError):
    """Invalid or incomplete inputs; do not silently change denominators."""


def finite_number(value, name: str, low=None, high=None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoringError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or (low is not None and value < low) or (
        high is not None and value > high
    ):
        raise ScoringError(f"{name} is outside its allowed range")
    return value


def fraction(successes: int, total: int) -> dict:
    """Wilson score interval, well behaved even for 0/N and N/N."""
    if not 0 <= successes <= total:
        raise ScoringError("invalid binomial counts")
    if not total:
        return {"successes": 0, "total": 0, "fraction": None, "ci95": [None, None]}
    z = NormalDist().inv_cdf(0.975)
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return {"successes": successes, "total": total, "fraction": p,
            "ci95": [max(0.0, center - radius), min(1.0, center + radius)]}


def validate_record(record: dict) -> None:
    """Validate scored fields without reading any answer key."""
    try:
        if record["mode"] != "campaign" or record["schema_version"] != 4:
            raise ScoringError("expected a schema-4 campaign record")
        if not isinstance(record["experiment_id"], str) or not record["experiment_id"]:
            raise ScoringError("missing experiment ID")
        if not isinstance(record["injection"]["spec_id"], str):
            raise ScoringError("missing spec ID")
        detection, coincidence = record["detection"], record["coincidence"]
        if type(detection["chirp_like"]) is not bool:
            raise ScoringError("chirp_like must be a boolean")
        if coincidence["coincident"] is not None and type(coincidence["coincident"]) is not bool:
            raise ScoringError("coincident must be a boolean or null")
        if coincidence["coincident"] is None and coincidence["checked"] is not False:
            raise ScoringError("null coincidence must be explicitly unchecked")
        finite_number(detection["confidence_score"], "confidence_score", 0, 1)
        finite_number(record["gps_time"], "gps_time")
        if record["llm_review"]["decision"] not in DECISIONS:
            raise ScoringError("unrecognized LLM decision")
        finite_number(record["llm_review"]["interesting_score"], "interesting_score", 0, 1)
        for key in ("pipeline", "planner_prompt", "vision_prompt", "detection_algorithm",
                    "planner_model", "vision_model", "llm_temperature"):
            if key not in record["versions"]:
                raise ScoringError(f"missing version stamp: {key}")
    except (KeyError, TypeError) as exc:
        raise ScoringError("record is missing required scoring fields") from exc


def grade_record(record: dict) -> dict:
    validate_record(record)
    primary = record["detection"]["chirp_like"] is True or (
        record["coincidence"]["coincident"] is True
    )
    known = (record.get("catalog") or {}).get("known_event_match") is True
    baseline_decision = (
        "benchmark_validated" if primary and known else
        "candidate_for_human_review" if primary else
        "glitch_candidate" if record["detection"].get("signal_detected") else "archive"
    )
    planner_error = bool(record["llm_review"].get("planner_error"))
    vision_error = bool((record.get("vision") or {}).get("error"))
    pipeline_error = bool(record["detection"].get("pipeline_error"))
    # Match loop.py's data_unavailable branch, which never invokes the planner.
    llm_skipped = pipeline_error and record["detection"].get("energy_contrast") is None
    return {
        "experiment_id": record["experiment_id"],
        "spec_id": record["injection"]["spec_id"],
        "gps_time": record["gps_time"],
        "primary": primary,
        "secondary": record["llm_review"]["decision"] in ESCALATIONS,
        "baseline_primary": primary,
        "baseline_decision": baseline_decision,
        "baseline_escalated": baseline_decision in ESCALATIONS,
        "decision": record["llm_review"]["decision"],
        "confidence_score": record["detection"]["confidence_score"],
        "interesting_score": record["llm_review"]["interesting_score"],
        "planner_error": planner_error, "vision_error": vision_error,
        "pipeline_error": pipeline_error,
        "llm_skipped": llm_skipped,
        "paired_eligible": not (planner_error or vision_error),
        "available_call_eligible": not (planner_error or vision_error or llm_skipped),
        "calibration_eligible": not (planner_error or vision_error or pipeline_error),
        "versions": record["versions"],
    }


def join_truth(records: list[dict], originals: list[dict], replacements: list[dict],
               excluded: set[str], original_ids: set[str],
               expected_injections=200, expected_controls=100) -> tuple[list[dict], list[dict]]:
    """Used only after explicit unblinding. Enforce one-to-one joins."""
    def index(rows, expected):
        out = {}
        for row in rows:
            sid = row.get("spec_id")
            if sid not in expected or sid in out:
                raise ScoringError("answer-key roster has an unexpected or duplicate spec")
            out[sid] = row
        if set(out) != expected:
            raise ScoringError("answer-key roster is incomplete")
        return out

    original = index(originals, original_ids)
    replacement = index(replacements, {f"replacement_{sid}" for sid in excluded})
    for rid, truth in replacement.items():
        oid = rid.removeprefix("replacement_")
        if truth.get("replaces_spec_id") != oid:
            raise ScoringError("replacement mapping disagrees with the frozen exclusions")
        for key in ("kind", "mass1", "mass2", "ra", "dec", "polarization", "inclination",
                    "coa_phase", "target_network_snr", "approximant"):
            if truth.get(key) != original[oid].get(key):
                raise ScoringError("replacement changed a frozen signal assignment")
    active = {sid: row for sid, row in original.items() if sid not in excluded} | replacement
    kinds = Counter(row.get("kind") for row in active.values())
    if kinds != {"injection": expected_injections, "noise": expected_controls}:
        raise ScoringError("answer key does not contain the required injection/control counts")
    rows, invalid = [], []
    seen, experiments = set(), set()
    for record in records:
        sid = (record.get("injection") or {}).get("spec_id")
        eid = record.get("experiment_id")
        if sid in seen or eid in experiments:
            raise ScoringError("duplicate execution in campaign records")
        seen.add(sid)
        experiments.add(eid)
        if sid in excluded:
            invalid.append({"experiment_id": eid, "spec_id": sid,
                            "reason": "frozen non-finite original exclusion"})
            continue
        if sid not in active:
            raise ScoringError("campaign record is outside the frozen roster")
        row, truth = grade_record(record), active[sid]
        gps = finite_number(truth.get("gps_time"), "truth gps_time")
        if abs(row["gps_time"] - gps) > 0.00051:
            raise ScoringError("record GPS does not match its answer-key spec")
        row["kind"] = truth["kind"]
        row["snr"] = None
        if truth["kind"] == "injection":
            row["snr"] = finite_number(truth.get("achieved_network_snr"),
                                       "achieved_network_snr", 4, 24)
        rows.append(row)
    if {row["spec_id"] for row in rows} != set(active):
        raise ScoringError("campaign is incomplete; refusing partial scoring")
    return sorted(rows, key=lambda r: r["spec_id"]), invalid


def snr_curve(rows: list[dict], field: str) -> list[dict]:
    curve = []
    for low, high in zip(SNR_EDGES, SNR_EDGES[1:]):
        selected = [r for r in rows if r["kind"] == "injection" and
                    low <= r["snr"] and (r["snr"] < high or high == 24 and r["snr"] == 24)]
        curve.append({"low": low, "high": high, "center": (low + high) / 2,
                      **fraction(sum(r[field] for r in selected), len(selected))})
    return curve


def snr50(curve: list[dict]) -> dict:
    """Do not invent a crossing across an empty bin or extrapolate a sparse curve."""
    crossings, downward, ambiguous_exact, isolated_exact = [], [], [], []
    for i, point in enumerate(curve):
        if point["fraction"] != 0.5:
            continue
        left = curve[i - 1]["fraction"] if i else None
        right = curve[i + 1]["fraction"] if i + 1 < len(curve) else None
        if left is None and right is None:
            isolated_exact.append(point["center"])
        elif left == 0.5 or right == 0.5:
            ambiguous_exact.append(point["center"])
        elif (left is None or left < 0.5) and (right is None or right > 0.5):
            crossings.append(point["center"])
        elif (left is None or left > 0.5) and (right is None or right < 0.5):
            downward.append(point["center"])
        else:
            # Touching 50% and turning back is not an increasing threshold.
            ambiguous_exact.append(point["center"])
    for left, right in zip(curve, curve[1:]):
        a, b = left["fraction"], right["fraction"]
        if a is None or b is None:
            continue
        if (a < 0.5 < b) or (b < 0.5 < a):
            value = left["center"] + (0.5 - a) * (right["center"] - left["center"]) / (b - a)
            (crossings if b > a else downward).append(value)
    crossings = sorted(set(crossings))
    values = [b["fraction"] for b in curve if b["fraction"] is not None]
    if ambiguous_exact or len(crossings) > 1 or downward:
        status, value = "ambiguous_nonmonotonic_or_flat", None
    elif isolated_exact:
        status, value = "insufficient_adjacent_data", None
    elif len(crossings) == 1:
        status, value = "estimated", crossings[0]
    elif values and min(values) > 0.5:
        status, value = "not_bracketed_above_50_percent", None
    elif values and max(values) < 0.5:
        status, value = "not_bracketed_below_50_percent", None
    else:
        status, value = "insufficient_adjacent_data", None
    return {"status": status, "value": value, "upward_or_exact_crossings": crossings,
            "downward_crossings": downward, "ambiguous_exact_points": ambiguous_exact,
            "isolated_exact_points": isolated_exact,
            "uncertainty": "not estimated; descriptive interpolation"}


def paired_comparison(rows: list[dict], eligibility="paired_eligible") -> dict:
    out = {}
    for kind in ("injection", "noise"):
        selected = [r for r in rows if r["kind"] == kind and r[eligibility]]
        cells = Counter((r["primary"], r["secondary"]) for r in selected)
        n = len(selected)
        out[kind] = {
            "total": n,
            "physics": fraction(sum(r["primary"] for r in selected), n),
            "llm": fraction(sum(r["secondary"] for r in selected), n),
            "both_flagged": cells[True, True], "neither_flagged": cells[False, False],
            "physics_only": cells[True, False], "llm_only": cells[False, True],
            "llm_minus_physics": (cells[False, True] - cells[True, False]) / n if n else None,
        }
    out["excluded_api_error_spec_ids"] = [r["spec_id"] for r in rows if not r["paired_eligible"]]
    out["excluded_spec_ids"] = [r["spec_id"] for r in rows if not r[eligibility]]
    out["included_skipped_call_spec_ids"] = [r["spec_id"] for r in rows if r[eligibility] and r["llm_skipped"]]
    return out


def score_rows(rows: list[dict], invalid: list[dict]) -> dict:
    injection = [r for r in rows if r["kind"] == "injection"]
    noise = [r for r in rows if r["kind"] == "noise"]
    metrics = {}
    for field in ("primary", "secondary"):
        curve = snr_curve(rows, field)
        metrics[field] = {
            "injections": fraction(sum(r[field] for r in injection), len(injection)),
            "noise_controls": fraction(sum(r[field] for r in noise), len(noise)),
            "efficiency_curve": curve, "snr50": snr50(curve),
        }
    supplementary = {}
    for threshold in CONFIDENCE_THRESHOLDS:
        flagged = [dict(r, threshold_flag=r["confidence_score"] >= threshold) for r in rows]
        supplementary[str(threshold)] = {
            "efficiency_curve": snr_curve(flagged, "threshold_flag"),
            "noise_controls": fraction(sum(r["threshold_flag"] for r in flagged if r["kind"] == "noise"),
                                       len(noise)),
        }
    calibration = []
    available = [r for r in rows if r["calibration_eligible"]]
    for i in range(5):
        selected = [r for r in available if min(int(r["interesting_score"] * 5), 4) == i]
        calibration.append({"low": i / 5, "high": (i + 1) / 5,
                            "mean_score": sum(r["interesting_score"] for r in selected) / len(selected)
                            if selected else None,
                            "injection_fraction": fraction(sum(r["kind"] == "injection" for r in selected),
                                                           len(selected))})
    versions = Counter(json.dumps(r["versions"], sort_keys=True) for r in rows)
    return {
        "scorer_version": SCORER_VERSION, "rules": RULES,
        "counts": {"scored": len(rows), "injections": len(injection), "controls": len(noise),
                   "excluded_invalid_attempts": len(invalid),
                   "planner_errors": sum(r["planner_error"] for r in rows),
                   "vision_errors": sum(r["vision_error"] for r in rows),
                   "llm_skipped": sum(r["llm_skipped"] for r in rows),
                   "pipeline_errors": sum(r["pipeline_error"] for r in rows)},
        "metrics": metrics, "paired_comparison": paired_comparison(rows),
        "available_call_comparison": paired_comparison(rows, "available_call_eligible"),
        "supplementary_confidence": supplementary,
        "score_reliability": {"bins": calibration, "eligible": len(available),
                              "excluded_spec_ids": [r["spec_id"] for r in rows if not r["calibration_eligible"]],
                              "note": "interesting_score is a ranking score, not a promised probability; "
                                      "fractions depend on the designed 200:100 class balance"},
        "version_groups": [{"versions": json.loads(v), "records": count}
                           for v, count in sorted(versions.items())],
    }
