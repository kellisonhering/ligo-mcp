"""Local result tables, figures, and a plain-English explanation."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from campaign_scoring import ScoringError


def _percentage(value):
    return "unavailable" if value is None else f"{value:.1%}"


def _rate(value):
    lo, hi = value["ci95"]
    if not value["total"]:
        return "unavailable (no examples)"
    return (f"{value['successes']}/{value['total']} = {_percentage(value['fraction'])} "
            f"(95% interval {_percentage(lo)} to {_percentage(hi)})")


def _snr50(value):
    if value["value"] is not None:
        return f"{value['value']:.2f} (descriptive interpolation; uncertainty not estimated)"
    return f"unavailable: {value['status'].replace('_', ' ')}"


def plain_report(result: dict, rows: list[dict]) -> str:
    counts, metrics = result["counts"], result["metrics"]
    title = "DEMONSTRATION: invented data only" if result["demo"] else "July 2026 campaign results"
    primary, secondary = metrics["primary"], metrics["secondary"]
    lines = [
        f"# {title}", "",
        "This report grades a controlled exam: simulated signals were hidden in real detector noise. "
        "It measures the pipeline's behavior on those examples.", "",
        f"Scored: **{counts['scored']}** runs: {counts['injections']} injected signals and "
        f"{counts['controls']} noise controls. Excluded invalid attempts: {counts['excluded_invalid_attempts']}.", "",
        "## What did the system catch?", "",
        "The primary rule counts a catch when the pipeline sees a chirp shape OR the detectors agree. "
        "This rule is mechanical and is identical to the recomputed physics-only baseline.", "",
        f"- Injected signals caught: {_rate(primary['injections'])}.",
        f"- Noise controls flagged (false alarms per tested window): {_rate(primary['noise_controls'])}.",
        f"- Signal strength at about 50% caught (SNR50): {_snr50(primary['snr50'])}.", "",
        "Here, a catch means the pipeline flagged a window containing an added signal. "
        "It does not prove the flag was caused by that signal: a noise feature could trigger it too. "
        "A miss means that window did not satisfy the locked rule. "
        "A flagged noise control is counted as a false alarm under the locked campaign definition. "
        "The intervals show sampling uncertainty: these percentages come from a finite exam.", "",
        "## Did the AI ask a human to look?", "",
        "The secondary rule counts follow_up or candidate_for_human_review as an escalation.", "",
        f"- Injections escalated: {_rate(secondary['injections'])}.",
        f"- Noise controls escalated: {_rate(secondary['noise_controls'])}.",
        f"- Secondary SNR50: {_snr50(secondary['snr50'])}.", "",
        "These operational totals include safe-default decisions during failures, as required by the "
        "campaign's completeness note. The paired comparison below removes records with a planner or vision "
        "API error from BOTH arms so an unavailable AI call is not treated as a real AI judgment.", "",
        "## Does the AI change the trade-off?", "",
        "| July 30 comparison: same examples | Physics rule | AI escalation | AI minus physics |",
        "| --- | --- | --- | --- |",
    ]
    for kind, label in (("injection", "Injected signals flagged"), ("noise", "Noise controls flagged")):
        paired = result["paired_comparison"][kind]
        difference = paired["llm_minus_physics"]
        change = "unavailable" if difference is None else f"{100 * difference:+.1f} percentage points"
        lines.append(f"| {label} (N={paired['total']}) | {_rate(paired['physics'])} | "
                     f"{_rate(paired['llm'])} | {change} |")
    lines += ["", "More caught injections is useful; fewer flagged noise controls is also useful. "
              "A system that catches fewer signals and raises fewer alarms has a trade-off, not an automatic win. "
              "The differences are descriptive, with no significance claim or interval on the difference.", "",
              f"Pipeline failures skipped the AI entirely on {counts['llm_skipped']} runs. "
              f"The July 30 comparison above retains "
              f"{len(result['paired_comparison']['included_skipped_call_spec_ids'])} of these default decisions "
              "because its exclusion rule names API errors only. They are not AI judgments. "
              "The supplementary table below removes skipped calls from both arms as well; "
              "it does not replace the locked operational totals or the July 30 comparison.", "",
              "| Supplementary: AI available | Physics rule | AI escalation | AI minus physics |",
              "| --- | --- | --- | --- |"]
    for kind, label in (("injection", "Injected signals flagged"), ("noise", "Noise controls flagged")):
        paired = result["available_call_comparison"][kind]
        difference = paired["llm_minus_physics"]
        change = "unavailable" if difference is None else f"{100 * difference:+.1f} percentage points"
        lines.append(f"| {label} (N={paired['total']}) | {_rate(paired['physics'])} | "
                     f"{_rate(paired['llm'])} | {change} |")
    lines += ["", f"Planner errors: {counts['planner_errors']}; vision errors: {counts['vision_errors']}; "
              f"pipeline/data errors: {counts['pipeline_errors']}. Error categories can overlap.", "",
              "## What do the charts mean?", "",
              "- efficiency.png: how often each rule caught injections as the hidden signals got stronger. "
              "Empty bins remain gaps. Error bars are 95% Wilson intervals.",
              "- score_reliability.png: among available AI calls in each score range, what fraction contained "
              "injections? The score is a ranking, not a guaranteed probability. The artificial 200:100 mix "
              "differs from a real survey, so this cannot give the chance that a sky candidate is real.",
              "- matched_filter.png: recovered strength using the exact injected waveform and known arrival "
              "time, colored by whether the primary rule caught it. This is an optimistic yardstick; a real "
              "search must try many waveforms and times, and measure its own false alarms.", "",
              "Supplementary confidence-threshold curves (0.3, 0.45, 0.6) appear in efficiency.csv and results.json. "
              "These use the pipeline's numerical confidence_score, not the AI's interesting_score.", "",
              "## Locked interpretations and their limits", ""]
    value = primary["snr50"]["value"]
    far = primary["noise_controls"]["fraction"]
    if value is None:
        lines.append("SNR50 is not uniquely supported by adjacent bins, so the scorer does not assign "
                     "one of the pre-committed SNR50 interpretations.")
    elif value <= 10:
        lines.append("The point estimate meets the locked SNR50 <= 10 criterion described as competitive "
                     "cheap triage. Its practical meaning also depends on false alarms and sampling uncertainty; "
                     "it does not establish equality to a full matched-filter search.")
    elif value >= 15:
        lines.append("The point estimate meets the locked SNR50 >= 15 criterion: cheap triage misses much "
                     "of the marginal-strength region. That limitation is a result to report.")
    else:
        lines.append("The SNR50 point estimate lies between the two locked interpretation thresholds.")
    if far is not None and far > 0.1:
        lines.append("The primary false-alarm fraction exceeds the locked 10% threshold: the rule is too loose "
                     "on this control set. Report this alongside sensitivity.")
    else:
        lines.append("The primary point estimate does not exceed 10% false alarms; the interval still matters.")
    lines += ["", "The original exam contains stellar-mass binary black-hole injections, not subsolar "
              "injections. Its results do not establish performance on subsolar objects or O4 noise. "
              "No part of this report establishes a new astronomical detection.", "",
              f"Recorded software/model combinations: {len(result['version_groups'])}. See results.json "
              "for each combination and its count. Pooled results describe the versions actually used.", "",
              "## Reproducibility", "",
              "results.json includes the grading conventions, input/source hashes, software versions, "
              "counts and denominators. scored_records.jsonl contains each joined graded row. "
              "excluded_attempts.jsonl lists the invalid originals. SHA256SUMS.txt hashes every output "
              "except itself; COMPLETE.json is written after all reports and charts succeed.", "",
              "The bin edges and rules come from REVIEW_FINDINGS.md sections 7.5, 7.5c, 7.5d and the "
              "July 30 completeness note. The 95% Wilson intervals, fixed score bins, conservative SNR50 "
              "handling, supplementary available-call comparison, and known-time matched-filter convention "
              "were chosen while building and reviewing the scorer, "
              "before opening the campaign answer keys; they were not specified in the original lock.", ""]
    if result["demo"]:
        lines += ["**Every number above is invented demonstration data. No real campaign truth was read. "
                  "The matched-filter panel is a placeholder in this demonstration.**", ""]
    return "\n".join(lines)


def _curves_csv(path, result):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("rule", "low", "high", "center", "successes",
                                                   "total", "fraction", "ci_low", "ci_high"))
        writer.writeheader()
        curves = {name: value["efficiency_curve"] for name, value in result["metrics"].items()}
        curves.update({f"supplementary_confidence_{threshold}": value["efficiency_curve"]
                       for threshold, value in result["supplementary_confidence"].items()})
        for name, curve in curves.items():
            for row in curve:
                writer.writerow({"rule": name, **{k: v for k, v in row.items() if k != "ci95"},
                                 "ci_low": row["ci95"][0], "ci_high": row["ci95"][1]})


def draw_figures(output: Path, result: dict, rows: list[dict]):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    prefix = "DEMONSTRATION ONLY - " if result["demo"] else ""
    with plt.rc_context({"font.size": 11, "figure.dpi": 140}):
        fig, ax = plt.subplots(figsize=(8.5, 5.3), layout="constrained")
        for name, color in (("primary", "#1b6591"), ("secondary", "#bb5a22")):
            curve = result["metrics"][name]["efficiency_curve"]
            x = [b["center"] for b in curve]
            y = np.array([b["fraction"] if b["fraction"] is not None else np.nan for b in curve])
            ax.plot(x, y, "o-", color=color, label=name.capitalize())
            for b in curve:
                if b["fraction"] is not None:
                    lo, hi = b["ci95"]
                    ax.errorbar(b["center"], b["fraction"],
                                yerr=[[max(0, b["fraction"] - lo)], [max(0, hi - b["fraction"])]],
                                color=color, capsize=3)
        ax.axhline(0.5, color="0.6", linestyle=":")
        ax.set(xlabel="Injected network optimal SNR (signal strength)", ylabel="Fraction caught / escalated",
               xlim=(4, 24), ylim=(-0.02, 1.02), title=prefix + "Injection recovery (95% intervals)")
        ax.legend()
        ax.grid(alpha=0.2)
        fig.savefig(output / "efficiency.png")
        fig.savefig(output / "efficiency.pdf")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(8.5, 5.3), layout="constrained")
        for b in result["score_reliability"]["bins"]:
            rate = b["injection_fraction"]
            if not rate["total"]:
                continue
            p, (lo, hi) = rate["fraction"], rate["ci95"]
            ax.errorbar(b["mean_score"], p, yerr=[[max(0, p - lo)], [max(0, hi - p)]],
                        fmt="o", capsize=4, color="#1b6591")
            ax.annotate(f"N={rate['total']}", (b["mean_score"], p), xytext=(7, 7), textcoords="offset points")
        ax.set(xlabel="Mean AI interesting_score in fixed score bin",
               ylabel="Fraction with an injected signal", xlim=(-0.03, 1.1), ylim=(-0.03, 1.1),
               title=prefix + "Score reliability in the designed exam")
        ax.grid(alpha=0.2)
        fig.savefig(output / "score_reliability.png")
        fig.savefig(output / "score_reliability.pdf")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(8.5, 5.3), layout="constrained")
        mf = [r for r in rows if r.get("matched_filter")]
        if mf:
            for caught, color, label in ((True, "#1b6591", "Primary caught"),
                                         (False, "#bb5a22", "Primary missed")):
                selected = [r for r in mf if r["primary"] is caught]
                ax.scatter([r["snr"] for r in selected],
                           [r["matched_filter"]["recovered_network_snr_at_injected_time"] for r in selected],
                           s=24, color=color, alpha=0.7, label=label)
            ax.plot([4, 24], [4, 24], ":", color="0.5", label="Recovered = optimal strength")
            ax.legend()
        else:
            ax.text(0.5, 0.5, "Invented-data demo: no matched-filter recovery computed",
                    ha="center", va="center", transform=ax.transAxes, wrap=True)
        ax.set(xlabel="Injected network optimal SNR", ylabel="Recovered SNR at known injected time",
               title=prefix + "Exact-template, known-time upper-bound yardstick")
        ax.grid(alpha=0.2)
        fig.savefig(output / "matched_filter.png")
        fig.savefig(output / "matched_filter.pdf")
        plt.close(fig)


def write_outputs(output: Path, result: dict, rows: list[dict], invalid: list[dict]):
    if not result["demo"] and any(r["kind"] == "injection" and not r.get("matched_filter") for r in rows):
        raise ScoringError("real report requires the matched-filter yardstick for every injection")
    output.mkdir(parents=True, exist_ok=False)
    for name, content in (("results.json", result),):
        (output / name).write_text(json.dumps(content, indent=2, allow_nan=False) + "\n")
    for name, contents in (("scored_records.jsonl", rows), ("excluded_attempts.jsonl", invalid)):
        (output / name).write_text("".join(json.dumps(r, sort_keys=True, allow_nan=False) + "\n" for r in contents))
    _curves_csv(output / "efficiency.csv", result)
    (output / "REPORT.md").write_text(plain_report(result, rows))
    draw_figures(output, result, rows)
    completion = json.dumps({"complete": True, "demo": result["demo"],
                             "scorer_version": result["scorer_version"]}) + "\n"
    manifest = []
    for path in sorted(output.iterdir()):
        manifest.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
    manifest.append(f"{hashlib.sha256(completion.encode()).hexdigest()}  COMPLETE.json\n")
    (output / "SHA256SUMS.txt").write_text("".join(sorted(manifest, key=lambda s: s.split('  ', 1)[1])))
    (output / "COMPLETE.json").write_text(completion)
