import os
from datetime import datetime, timezone

REPORTS_DIR = os.path.expanduser("~/experiment-data/ligo/reports")

DISCLAIMER = "INTERNAL CANDIDATE REPORT — NOT PEER REVIEWED — HUMAN APPROVAL REQUIRED"


def generate_candidate_report(experiment: dict) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    experiment_id = experiment.get("experiment_id", "unknown")
    report_path = os.path.join(REPORTS_DIR, f"{experiment_id}.md")

    detection = experiment.get("detection", {})
    catalog = experiment.get("catalog", {})
    vision = experiment.get("vision") or {}
    llm = experiment.get("llm_review", {})

    gps_time = experiment.get("gps_time", "unknown")
    detector = experiment.get("detector", "unknown")

    lines = [
        f"# {DISCLAIMER}",
        "",
        f"## Candidate Report — {experiment_id}",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}",
        f"**GPS Time:** {gps_time}",
        f"**Detector:** {detector}",
        f"**Mode:** {experiment.get('mode', 'unknown')}",
        "",
        "---",
        "",
        "## Detection Metrics",
        f"- Signal Detected: {detection.get('signal_detected')}",
        f"- Peak Frequency: {detection.get('peak_frequency_hz')} Hz",
        f"- Peak Time Offset: {detection.get('peak_time_offset_s')} s",
        f"- Energy Contrast (peak/median tile energy — not matched-filter SNR): "
        f"{detection.get('energy_contrast', detection.get('snr'))}",
        f"- Confidence Score (discriminator-based): {detection.get('confidence_score')}",
        f"- Classification Hint: {detection.get('classification_hint')}",
        "",
        "---",
        "",
        "## Catalog Check",
        f"- Known Event Match: {catalog.get('known_event_match')}",
        f"- Event Name: {catalog.get('event_name')}",
        f"- Event Type: {catalog.get('event_type')}",
        f"- Time Offset from Known Event: {catalog.get('time_offset_s')} s",
        f"- Observing Run: {catalog.get('observing_run')}",
        "",
        "---",
        "",
        "## Vision Analysis",
        f"- Shape Classification: {vision.get('shape_classification')}",
        f"- Likely Signal Type: {vision.get('likely_signal_type')}",
        f"- Vision Confidence: {vision.get('confidence')}",
        f"- Vision Reasoning: {vision.get('reasoning')}",
        "",
        "---",
        "",
        "## LLM Decision",
        f"- Decision: **{llm.get('decision')}**",
        f"- Interesting Score: {llm.get('interesting_score')}",
        f"- Human Review Required: {llm.get('human_review_required')}",
        "",
        "**Reasoning:**",
        llm.get("reasoning", ""),
        "",
        "**Recommended Next Actions:**",
    ]

    for action in llm.get("next_actions", []):
        lines.append(f"- {action}")

    lines += [
        "",
        "---",
        "",
        "*All data sourced from LIGO Open Science Center. This report is not a scientific claim.*",
    ]

    with open(report_path, "w") as f:
        f.write("\n".join(lines))

    return report_path


def generate_daily_summary(experiments: list[dict]) -> str:
    if not experiments:
        return "No experiments recorded yet."

    total = len(experiments)
    detected = sum(1 for e in experiments if e.get("detection", {}).get("signal_detected"))
    decisions = {}
    for e in experiments:
        d = e.get("llm_review", {}).get("decision", "unknown")
        decisions[d] = decisions.get(d, 0) + 1

    lines = [
        f"LIGO Pipeline Summary — {total} experiments",
        f"Signal detected: {detected}/{total}",
        "",
        "Decisions:",
    ]
    for decision, count in sorted(decisions.items(), key=lambda x: -x[1]):
        lines.append(f"  {decision}: {count}")

    # Median, not mean — one extreme value (e.g. GW170817's L1 glitch at ~216k)
    # would poison a mean. Reads both schema v2 ("snr") and v3 ("energy_contrast").
    contrast_vals = sorted(
        v for v in (
            e.get("detection", {}).get("energy_contrast", e.get("detection", {}).get("snr"))
            for e in experiments
        ) if v is not None
    )
    if contrast_vals:
        median = contrast_vals[len(contrast_vals) // 2]
        lines.append(f"\nMedian energy contrast: {median:.1f}")

    interesting = [
        e for e in experiments
        if (e.get("llm_review", {}).get("interesting_score") or 0) >= 0.6
    ]
    if interesting:
        lines.append(f"\nHigh-interest experiments ({len(interesting)}):")
        for e in interesting:
            lines.append(
                f"  GPS {e.get('gps_time')} {e.get('detector')} — "
                f"score={e.get('llm_review', {}).get('interesting_score')} — "
                f"{e.get('llm_review', {}).get('decision')}"
            )

    return "\n".join(lines)
