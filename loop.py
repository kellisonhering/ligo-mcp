import json
import os
import random
import time
import traceback
import uuid
from datetime import datetime, timezone


def _load_openclaw_env() -> None:
    """
    Load API keys from ~/.openclaw/.env into the process environment.
    The Anthropic Python client looks for ANTHROPIC_API_KEY as an env var.
    OpenClaw stores keys in this file — without loading it, the planner fails.
    Called once at module import time before any API clients are created.
    """
    env_path = os.path.expanduser("~/.openclaw/.env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_openclaw_env()

PIPELINE_VERSION = "1.2.0"
PLANNER_PROMPT_VERSION = "1.2.0"   # bump when SYSTEM_PROMPT in planner.py changes
VISION_PROMPT_VERSION = "1.0.0"    # bump when VISION_SYSTEM_PROMPT in planner.py changes
DETECTION_ALGORITHM_VERSION = "2.0.0"  # bump when pipeline.py signal detection logic changes
# 2.0.0 (July 10, 2026): W1 — "snr" renamed to "energy_contrast" (it was never
# matched-filter SNR); confidence_score rebuilt from discriminators (chirp,
# time-of-flight coincidence, DQ) instead of loudness. W2 — coincidence now
# requires arrival-time agreement within light travel between sites.
# Records from this version on are schema_version 3; confidence_score is NOT
# comparable across the v2→v3 boundary.

from pipeline import run_pipeline, compute_confidence_score
from catalog import check_catalog, SUBSOLAR_CANDIDATES
from planner import (
    run_planner, run_vision_analysis, select_next_target,
    PLANNER_MODEL, VISION_MODEL, SELECTOR_MODEL, LLM_TEMPERATURE,
)
from reporter import generate_candidate_report, generate_daily_summary
from crossmatch import (
    check_fermi_gbm, check_data_quality,
    check_snews_archive, check_icecube_gcn,
)

# Data lives OUTSIDE the iCloud-synced Desktop (in the home folder) so iCloud can't
# lock experiments.jsonl mid-write — that caused intermittent "Resource deadlock
# avoided" failures at the save step. See ~/experiment-data/.
DATA_DIR = os.path.expanduser("~/experiment-data/ligo")
EXPERIMENTS_FILE = os.path.join(DATA_DIR, "experiments.jsonl")
PLOTS_DIR = os.path.join(DATA_DIR, "plots")

CANDIDATE_DECISIONS = {"follow_up", "candidate_for_human_review"}
BENCHMARK_INTERVAL = 5
SLEEP_BETWEEN_EXPERIMENTS = 3600  # 1 hour — 24 experiments per day (temporarily faster to make up lost time)
REQUEUE_DECISIONS = {"rerun", "follow_up"}
MAX_REQUEUE_ATTEMPTS = 3

# O3 observing run boundaries (GPS time)
O3_START = 1238166018
O3_END   = 1269363618

# O4 observing run (started May 2023, scheduled through late 2025)
O4_START = 1368720018  # approx May 24, 2023

# Public strain-data cutoff. GWOSC has fully released O1/O2/O3 (through O3_END).
# O4 data releases gradually through 2026 — GraceDB lists recent events whose
# strain data isn't downloadable yet, so we skip anything past this cutoff to
# avoid wasting cycles on data that doesn't exist publicly. Raise this as O4 releases.
PUBLIC_DATA_GPS_CUTOFF = O3_END  # 1269363618 (~March 2020, end of O3)

# Known GW events — rotate as benchmarks every BENCHMARK_INTERVAL experiments.
BENCHMARK_TARGETS = [
    {"gps_time": 1126259462.4, "detector": "H1", "mode": "benchmark", "label": "GW150914"},
    {"gps_time": 1187008882.4, "detector": "H1", "mode": "benchmark", "label": "GW170817"},
    {"gps_time": 1242442967.4, "detector": "H1", "mode": "benchmark", "label": "GW190521"},
    {"gps_time": 1135136350.6, "detector": "H1", "mode": "benchmark", "label": "GW151226"},
    {"gps_time": 1186741861.5, "detector": "H1", "mode": "benchmark", "label": "GW170814"},
]

# Sub-solar mass / PBH benchmarks — rotate during pbh_survey mode.
# S251112cm: first sub-solar mass GW candidate (November 2025).
# GPS is approximate (±12 hours) — exact time pending public GWOSC release.
PBH_BENCHMARK_TARGETS = [
    {
        "gps_time": 1446984000.0,
        "detector": "H1",
        "mode": "pbh_benchmark",
        "label": "S251112cm",
        "subsolar_mode": True,
        "note": "First sub-solar mass GW candidate — potential primordial black hole. GPS approx.",
    },
]


def run_experiment(
    gps_time: float,
    detector: str = "H1",
    mode: str = "survey",
    subsolar_mode: bool = False,
    provenance: dict | None = None,
) -> dict:
    experiment_id = f"ligo_{datetime.now(timezone.utc).strftime('%Y_%m_%d')}_{uuid.uuid4().hex[:6]}"
    created_at = datetime.now(timezone.utc).isoformat()
    t_start = time.perf_counter()

    mode_tag = " [PBH]" if subsolar_mode else ""
    print(f"[{experiment_id}] Analyzing GPS {gps_time:.1f} {detector}{mode_tag}")

    plot_path = os.path.join(PLOTS_DIR, f"{experiment_id}.png")
    pipeline_result = run_pipeline(
        gps_time=gps_time,
        detector=detector,
        plot_path=plot_path,
        subsolar_mode=subsolar_mode,
    )
    print(f"[{experiment_id}] Pipeline complete — detected={pipeline_result.signal_detected} contrast={pipeline_result.energy_contrast}")

    if pipeline_result.error:
        print(f"[{experiment_id}] Pipeline error: {pipeline_result.error}")

    catalog_result = check_catalog(gps_time=gps_time)
    print(f"[{experiment_id}] Catalog — known_event={catalog_result.known_event_match} name={catalog_result.event_name} pbh={catalog_result.is_pbh_candidate}")

    fermi_result = check_fermi_gbm(gps_time=gps_time)
    print(f"[{experiment_id}] Fermi GBM — trigger_found={fermi_result.trigger_found}")

    dq_result = check_data_quality(gps_time=gps_time, detector=detector)
    print(f"[{experiment_id}] Data quality — usable={dq_result.data_usable} cat1={dq_result.cat1_active} cat2={dq_result.cat2_active}")

    snews_result = check_snews_archive(gps_time=gps_time)
    if snews_result.alert_found:
        print(f"[{experiment_id}] *** SNEWS ALERT FOUND *** id={snews_result.alert_id} offset={snews_result.alert_time_offset_s}s")
    else:
        print(f"[{experiment_id}] SNEWS — no alert (expected — no galactic supernova detected)")

    icecube_result = check_icecube_gcn(gps_time=gps_time)
    if icecube_result.alert_found:
        print(f"[{experiment_id}] IceCube — alert found! stream={icecube_result.stream} signalness={icecube_result.signalness}")
    else:
        print(f"[{experiment_id}] IceCube — no alert in window")

    # W1: confidence is now built from discriminators (chirp, time-of-flight
    # coincidence, data quality) — computed here because it needs the DQ result,
    # which the pipeline doesn't have. Loudness is deliberately not an input.
    confidence_score = compute_confidence_score(
        signal_detected=pipeline_result.signal_detected,
        chirp_like=pipeline_result.chirp_like,
        coincidence=pipeline_result.coincidence,
        dq_usable=dq_result.data_usable if dq_result.status == "ok" else None,
        dq_cat2_active=dq_result.cat2_active if dq_result.status == "ok" else None,
    )

    experiment_summary = {
        "gps_time": gps_time,
        "detector": detector,
        "subsolar_mode": subsolar_mode,
        "signal_detected": pipeline_result.signal_detected,
        "peak_frequency_hz": pipeline_result.peak_frequency_hz,
        "peak_time_offset_s": pipeline_result.peak_time_offset_s,
        "energy_contrast": pipeline_result.energy_contrast,
        "confidence_score": confidence_score,
        "classification_hint": pipeline_result.classification_hint,
        "freq_early_hz": pipeline_result.freq_early_hz,
        "freq_late_hz": pipeline_result.freq_late_hz,
        "freq_sweep_hz_per_s": pipeline_result.freq_sweep_hz_per_s,
        "chirp_like": pipeline_result.chirp_like,
        "coincidence_checked": pipeline_result.coincidence.checked if pipeline_result.coincidence else False,
        "coincident": pipeline_result.coincidence.coincident if pipeline_result.coincidence else None,
        "coincidence_second_detector": pipeline_result.coincidence.second_detector if pipeline_result.coincidence else None,
        "coincidence_second_energy_contrast": pipeline_result.coincidence.second_energy_contrast if pipeline_result.coincidence else None,
        "coincidence_freq_agreement": pipeline_result.coincidence.freq_agreement if pipeline_result.coincidence else None,
        "coincidence_dt_peak_s": pipeline_result.coincidence.dt_peak_s if pipeline_result.coincidence else None,
        "coincidence_dt_agreement": pipeline_result.coincidence.dt_agreement if pipeline_result.coincidence else None,
        "virgo_checked": pipeline_result.coincidence.virgo_checked if pipeline_result.coincidence else False,
        "virgo_energy_contrast": pipeline_result.coincidence.virgo_energy_contrast if pipeline_result.coincidence else None,
        "virgo_dt_peak_s": pipeline_result.coincidence.virgo_dt_peak_s if pipeline_result.coincidence else None,
        "virgo_coincident": pipeline_result.coincidence.virgo_coincident if pipeline_result.coincidence else None,
        "triple_coincident": pipeline_result.coincidence.triple_coincident if pipeline_result.coincidence else False,
        "kagra_checked": pipeline_result.coincidence.kagra_checked if pipeline_result.coincidence else False,
        "kagra_energy_contrast": pipeline_result.coincidence.kagra_energy_contrast if pipeline_result.coincidence else None,
        "kagra_dt_peak_s": pipeline_result.coincidence.kagra_dt_peak_s if pipeline_result.coincidence else None,
        "kagra_coincident": pipeline_result.coincidence.kagra_coincident if pipeline_result.coincidence else None,
        "quad_coincident": pipeline_result.coincidence.quad_coincident if pipeline_result.coincidence else False,
        "fermi_trigger_found": fermi_result.trigger_found,
        "fermi_trigger_name": fermi_result.trigger_name,
        "fermi_offset_s": fermi_result.trigger_time_offset_s,
        "fermi_classification": fermi_result.classification,
        "fermi_t90_s": fermi_result.t90_s,
        "fermi_status": fermi_result.status,
        "dq_data_usable": dq_result.data_usable,
        "dq_cat1_active": dq_result.cat1_active,
        "dq_cat2_active": dq_result.cat2_active,
        "dq_flags_found": dq_result.flags_found,
        "dq_has_data": dq_result.has_data,
        "dq_status": dq_result.status,
        "snews_alert_found": snews_result.alert_found,
        "snews_alert_id": snews_result.alert_id,
        "snews_alert_offset_s": snews_result.alert_time_offset_s,
        "snews_status": snews_result.status,
        "icecube_alert_found": icecube_result.alert_found,
        "icecube_event_id": icecube_result.event_id,
        "icecube_alert_offset_s": icecube_result.alert_time_offset_s,
        "icecube_signalness": icecube_result.signalness,
        "icecube_stream": icecube_result.stream,
        "icecube_status": icecube_result.status,
        "known_event_match": catalog_result.known_event_match,
        "event_name": catalog_result.event_name,
        "event_type": catalog_result.event_type,
        "time_offset_s": catalog_result.time_offset_s,
        "observing_run": catalog_result.observing_run,
        "is_subsolar": catalog_result.is_subsolar,
        "is_pbh_candidate": catalog_result.is_pbh_candidate,
    }

    # If the pipeline produced no usable data (e.g. strain data not public yet, or a
    # download failure), skip vision + planner. There is nothing to analyze, and running
    # the LLM here makes it misread a null energy_contrast as "quiet clean background noise" when the
    # truth is the data was simply unavailable. This also saves the two LLM API calls on
    # an experiment that has nothing in it. A failed download is discoverable in the
    # dataset via detection.pipeline_error.
    data_unavailable = pipeline_result.error is not None and pipeline_result.energy_contrast is None

    vision_result = None
    if data_unavailable:
        from planner import PlannerResult
        planner_result = PlannerResult(
            decision="archive",
            interesting_score=0.0,
            reasoning=(
                "No strain data could be analyzed for this GPS time "
                f"(pipeline error: {pipeline_result.error}). This is a data-availability "
                "issue, NOT a quiet-sky result — the data could not be downloaded, so no "
                "vision or LLM analysis was performed. Archived."
            ),
            next_actions=[],
            human_review_required=False,
        )
        print(f"[{experiment_id}] Data unavailable — skipped vision + planner ({pipeline_result.error})")
    else:
        if pipeline_result.plot_path:
            vision_result = run_vision_analysis(pipeline_result.plot_path)
            print(f"[{experiment_id}] Vision: {vision_result.likely_signal_type} (modifier={vision_result.score_modifier})")
            experiment_summary["vision_shape"] = vision_result.shape_classification
            experiment_summary["vision_signal_type"] = vision_result.likely_signal_type
            experiment_summary["vision_confidence"] = vision_result.confidence
            experiment_summary["vision_score_modifier"] = vision_result.score_modifier
            experiment_summary["vision_reasoning"] = vision_result.reasoning

        planner_result = run_planner(experiment_summary)
        print(f"[{experiment_id}] LLM decision: {planner_result.decision} (score={planner_result.interesting_score})")

    # Escalate SNEWS and IceCube coincidences regardless of LLM score
    if snews_result.alert_found and planner_result.decision not in CANDIDATE_DECISIONS:
        print(f"[{experiment_id}] SNEWS alert overrides LLM decision — escalating to candidate_for_human_review")
        from dataclasses import replace
        planner_result = replace(
            planner_result,
            decision="candidate_for_human_review",
            human_review_required=True,
            interesting_score=max(planner_result.interesting_score, 0.95),
        )

    experiment = {
        "experiment_id": experiment_id,
        "gps_time": gps_time,
        "detector": detector,
        "mission": "LIGO",
        "subsolar_mode": subsolar_mode,
        "detection": {
            "signal_detected": pipeline_result.signal_detected,
            "peak_frequency_hz": pipeline_result.peak_frequency_hz,
            "peak_time_offset_s": pipeline_result.peak_time_offset_s,
            "energy_contrast": pipeline_result.energy_contrast,
            "confidence_score": confidence_score,
            "classification_hint": pipeline_result.classification_hint,
            "freq_early_hz": pipeline_result.freq_early_hz,
            "freq_late_hz": pipeline_result.freq_late_hz,
            "freq_sweep_hz_per_s": pipeline_result.freq_sweep_hz_per_s,
            "chirp_like": pipeline_result.chirp_like,
            "pipeline_error": pipeline_result.error,
        },
        "coincidence": {
            "checked": pipeline_result.coincidence.checked if pipeline_result.coincidence else False,
            "coincident": pipeline_result.coincidence.coincident if pipeline_result.coincidence else None,
            "second_detector": pipeline_result.coincidence.second_detector if pipeline_result.coincidence else None,
            "second_energy_contrast": pipeline_result.coincidence.second_energy_contrast if pipeline_result.coincidence else None,
            "freq_agreement": pipeline_result.coincidence.freq_agreement if pipeline_result.coincidence else None,
            "second_peak_time_offset_s": pipeline_result.coincidence.second_peak_time_offset_s if pipeline_result.coincidence else None,
            "dt_peak_s": pipeline_result.coincidence.dt_peak_s if pipeline_result.coincidence else None,
            "dt_agreement": pipeline_result.coincidence.dt_agreement if pipeline_result.coincidence else None,
            "virgo_checked": pipeline_result.coincidence.virgo_checked if pipeline_result.coincidence else False,
            "virgo_energy_contrast": pipeline_result.coincidence.virgo_energy_contrast if pipeline_result.coincidence else None,
            "virgo_dt_peak_s": pipeline_result.coincidence.virgo_dt_peak_s if pipeline_result.coincidence else None,
            "virgo_dt_agreement": pipeline_result.coincidence.virgo_dt_agreement if pipeline_result.coincidence else None,
            "virgo_coincident": pipeline_result.coincidence.virgo_coincident if pipeline_result.coincidence else None,
            "triple_coincident": pipeline_result.coincidence.triple_coincident if pipeline_result.coincidence else False,
            "kagra_checked": pipeline_result.coincidence.kagra_checked if pipeline_result.coincidence else False,
            "kagra_energy_contrast": pipeline_result.coincidence.kagra_energy_contrast if pipeline_result.coincidence else None,
            "kagra_dt_peak_s": pipeline_result.coincidence.kagra_dt_peak_s if pipeline_result.coincidence else None,
            "kagra_dt_agreement": pipeline_result.coincidence.kagra_dt_agreement if pipeline_result.coincidence else None,
            "kagra_coincident": pipeline_result.coincidence.kagra_coincident if pipeline_result.coincidence else None,
            "quad_coincident": pipeline_result.coincidence.quad_coincident if pipeline_result.coincidence else False,
        },
        "catalog": {
            "known_event_match": catalog_result.known_event_match,
            "event_name": catalog_result.event_name,
            "event_type": catalog_result.event_type,
            "event_gps": catalog_result.event_gps,
            "time_offset_s": catalog_result.time_offset_s,
            "observing_run": catalog_result.observing_run,
            "is_subsolar": catalog_result.is_subsolar,
            "is_pbh_candidate": catalog_result.is_pbh_candidate,
            "catalog_error": catalog_result.catalog_error,
        },
        "vision": vision_result.to_dict() if vision_result else None,
        "crossmatch": {
            "fermi": fermi_result.to_dict(),
            "data_quality": dq_result.to_dict(),
            "snews": snews_result.to_dict(),
            "icecube": icecube_result.to_dict(),
        },
        "llm_review": {
            "decision": planner_result.decision,
            "interesting_score": planner_result.interesting_score,
            "reasoning": planner_result.reasoning,
            "next_actions": planner_result.next_actions,
            "human_review_required": planner_result.human_review_required,
            "planner_error": planner_result.error,
        },
        "mode": mode,
        "schema_version": 3,  # v3 (July 10, 2026): snr→energy_contrast, discriminator confidence, dt fields
        "target_provenance": provenance or {},
        "wall_seconds": round(time.perf_counter() - t_start, 2),
        "plot_path": pipeline_result.plot_path,
        "created_at": created_at,
        "versions": {
            "pipeline": PIPELINE_VERSION,
            "planner_prompt": PLANNER_PROMPT_VERSION,
            "vision_prompt": VISION_PROMPT_VERSION,
            "detection_algorithm": DETECTION_ALGORITHM_VERSION,
            "planner_model": PLANNER_MODEL,
            "vision_model": VISION_MODEL,
            "selector_model": SELECTOR_MODEL,
            "llm_temperature": LLM_TEMPERATURE,
        },
    }

    _append_experiment(experiment)

    if planner_result.decision in CANDIDATE_DECISIONS:
        report_path = generate_candidate_report(experiment)
        experiment["report_path"] = report_path
        print(f"[{experiment_id}] Candidate report saved: {report_path}")

    return experiment


def run_loop(
    starting_targets: list[dict] | None = None,
    max_experiments: int | None = None,
    sleep_seconds: int = SLEEP_BETWEEN_EXPERIMENTS,
) -> None:
    """Standard survey loop — targets GraceDB retracted candidates."""
    queue = list(starting_targets or _fetch_survey_windows(limit=50))
    history = _load_recent_experiments(limit=20)
    benchmark_cycle = list(BENCHMARK_TARGETS)
    count = 0

    print(f"Starting LIGO research loop. Initial queue: {len(queue)} windows.")

    # F5: prove the external "senses" still work at startup (replay known-positive
    # history). Non-fatal — a failing canary is logged loudly but collection continues.
    try:
        from canary import run_all_canaries
        health = run_all_canaries()
        print(f"[startup canary] {health['summary']}")
        for name, c in health["checks"].items():
            print(f"[startup canary]   {name}: {c['status']} — {c['detail']}")
    except Exception as e:
        print(f"[startup canary] health check could not run: {e}")

    while True:
        if max_experiments is not None and count >= max_experiments:
            print(f"Reached max_experiments={max_experiments}. Stopping.")
            break

        if count > 0 and count % BENCHMARK_INTERVAL == 0:
            target = benchmark_cycle[count // BENCHMARK_INTERVAL % len(benchmark_cycle)]
            print(f"[benchmark] Inserting known event {target.get('label')} GPS {target['gps_time']}")
        else:
            if not queue:
                print("Queue empty. Asking LLM what to analyze next...")
                try:
                    next_target = select_next_target(history)
                    queue.append({
                        "gps_time": float(next_target["gps_time"]),
                        "detector": next_target.get("detector", "H1"),
                        "mode": "llm_selected",
                    })
                    print(f"LLM selected: GPS {next_target['gps_time']} — {next_target.get('reasoning', '')}")
                except Exception as e:
                    # F1: never stop the service just because the target-selector failed.
                    # Fall back to random public O3 windows and keep collecting. A clean
                    # exit here would look like success to launchd, which would NOT restart.
                    print(f"Failed to get next target from LLM: {e}. Falling back to random O3 windows.")
                    queue.extend(_fetch_random_o3_windows(limit=10))
                if not queue:
                    print("No targets available even after fallback. Sleeping before retry.")
                    time.sleep(sleep_seconds)
                    continue
            target = queue.pop(0)

        try:
            experiment = run_experiment(
                gps_time=target["gps_time"],
                detector=target.get("detector", "H1"),
                mode=target.get("mode", "survey"),
                subsolar_mode=target.get("subsolar_mode", False),
                provenance=dict(target),
            )
            history.append(experiment)
            if len(history) > 20:
                history = history[-20:]
            count += 1

            decision = experiment.get("llm_review", {}).get("decision")
            requeue_count = target.get("requeue_count", 0)
            if decision in REQUEUE_DECISIONS and requeue_count < MAX_REQUEUE_ATTEMPTS:
                other_detector = "L1" if target.get("detector", "H1") == "H1" else "H1"
                requeue_target = {
                    "gps_time": target["gps_time"],
                    "detector": other_detector,
                    "mode": "requeue",
                    "subsolar_mode": target.get("subsolar_mode", False),
                    "requeue_count": requeue_count + 1,
                }
                queue.append(requeue_target)
                print(f"[requeue] GPS {target['gps_time']} added back on {other_detector} — attempt {requeue_count + 1}/{MAX_REQUEUE_ATTEMPTS}")
            elif decision in REQUEUE_DECISIONS and requeue_count >= MAX_REQUEUE_ATTEMPTS:
                print(f"[requeue] GPS {target['gps_time']} hit max requeue limit. Archiving.")

        except Exception as e:
            print(f"Experiment failed for GPS {target['gps_time']}: {e}")
            # Full traceback so we can see WHERE it failed (e.g. the intermittent
            # 'Resource deadlock avoided' that only hits experiments run after a
            # long idle gap / system sleep). Goes to the stderr log.
            traceback.print_exc()

        if queue or (max_experiments is None or count < max_experiments):
            print(f"Sleeping {sleep_seconds}s before next experiment...")
            time.sleep(sleep_seconds)


def run_pbh_loop(
    max_experiments: int | None = None,
    sleep_seconds: int = SLEEP_BETWEEN_EXPERIMENTS,
) -> None:
    """
    Sub-solar mass / primordial black hole survey loop.

    Uses subsolar_mode=True throughout — wider frequency range (20-4096 Hz),
    lower chirp sweep threshold (5 Hz/s). Prioritizes GraceDB candidates
    where chirp mass estimates suggest sub-solar components.

    Benchmarks use S251112cm (first sub-solar mass GW candidate, November 2025).
    This is the only non-LIGO automated pipeline running targeted PBH surveys.
    """
    queue = list(_fetch_subsolar_candidates(limit=50))
    history = _load_recent_experiments(limit=20)
    pbh_benchmark_cycle = list(PBH_BENCHMARK_TARGETS)
    count = 0

    print(f"Starting LIGO PBH (primordial black hole) survey loop.")
    print(f"Sub-solar mass mode: frange=(20, 4096 Hz), chirp_threshold=5 Hz/s")
    print(f"Initial queue: {len(queue)} sub-solar mass candidates.")

    while True:
        if max_experiments is not None and count >= max_experiments:
            print(f"Reached max_experiments={max_experiments}. Stopping PBH loop.")
            break

        if count > 0 and count % BENCHMARK_INTERVAL == 0:
            target = pbh_benchmark_cycle[count // BENCHMARK_INTERVAL % len(pbh_benchmark_cycle)]
            print(f"[pbh_benchmark] Inserting {target.get('label')} GPS {target['gps_time']}")
        else:
            if not queue:
                print("PBH queue empty. Refetching sub-solar candidates from GraceDB...")
                queue = list(_fetch_subsolar_candidates(limit=50))
                if not queue:
                    print("No new sub-solar candidates. Sleeping 3600s before retry...")
                    time.sleep(3600)
                    continue
            target = queue.pop(0)

        try:
            experiment = run_experiment(
                gps_time=target["gps_time"],
                detector=target.get("detector", "H1"),
                mode=target.get("mode", "pbh_survey"),
                subsolar_mode=True,
                provenance=dict(target),
            )
            history.append(experiment)
            if len(history) > 20:
                history = history[-20:]
            count += 1

        except Exception as e:
            print(f"PBH experiment failed for GPS {target['gps_time']}: {e}")

        if max_experiments is None or count < max_experiments:
            print(f"Sleeping {sleep_seconds}s before next PBH experiment...")
            time.sleep(sleep_seconds)


def _fetch_subsolar_candidates(limit: int = 50, detector: str = "H1") -> list[dict]:
    """
    Fetch GraceDB superevents that may be sub-solar mass candidates.
    Prioritizes events where the FAR is low or where labels suggest unusual sources.
    Falls back to known PBH candidate GPS times from the catalog.
    """
    import requests

    targets = []

    try:
        print("Fetching GraceDB superevents for sub-solar mass screening...")
        response = requests.get(
            "https://gracedb.ligo.org/api/superevents/",
            params={"public": "true", "format": "json", "count": 200},
            timeout=20,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
        superevents = data.get("superevents", [])

        for ev in superevents:
            gps = ev.get("t_0")
            if not gps:
                continue

            labels = ev.get("labels", [])
            far = ev.get("far")  # false alarm rate — lower = more interesting
            gps_f = float(gps)

            # Include: retracted candidates (sub-threshold, LIGO thought it was real),
            # and any event with a very low FAR regardless of label.
            # Exclude confirmed events (handled as normal benchmarks).
            is_interesting = (
                "RETRACTED" in labels
                or (far is not None and far < 1e-6)  # ~1 per million seconds
            )

            if is_interesting:
                targets.append({
                    "gps_time": gps_f,
                    "detector": detector,
                    "mode": "pbh_survey",
                    "subsolar_mode": True,
                    "gracedb_id": ev.get("superevent_id"),
                    "gracedb_labels": labels,
                    "gracedb_far": far,
                })

        if targets:
            random.shuffle(targets)
            targets = targets[:limit]
            print(f"GraceDB: found {len(targets)} candidates for PBH screening.")
            return targets

    except Exception as e:
        print(f"GraceDB fetch failed: {e}")

    # Fallback: use the known sub-solar mass candidate GPS times from catalog
    print("Falling back to known sub-solar mass candidate GPS times...")
    for name, ev in SUBSOLAR_CANDIDATES.items():
        targets.append({
            "gps_time": ev["gps"],
            "detector": detector,
            "mode": "pbh_benchmark",
            "subsolar_mode": True,
            "label": name,
        })

    # Pad with random O4 windows in sub-solar search mode
    if len(targets) < limit:
        o4_targets = _fetch_random_o4_windows(limit=limit - len(targets), detector=detector)
        for t in o4_targets:
            t["subsolar_mode"] = True
            t["mode"] = "pbh_survey"
        targets.extend(o4_targets)

    random.shuffle(targets)
    return targets[:limit]


def _fetch_gracedb_triggers(limit: int = 50, detector: str = "H1") -> list[dict]:
    """
    Fetches public superevent candidates from GraceDB.
    Prioritizes retracted candidates over confirmed events.
    Falls back to random O3 windows if GraceDB is unavailable.
    """
    import requests
    from catalog import GWTC_EVENTS
    known_gps = {round(ev["gps"]) for ev in GWTC_EVENTS.values()}

    try:
        print("Fetching public GraceDB superevents...")
        response = requests.get(
            "https://gracedb.ligo.org/api/superevents/",
            params={"public": "true", "format": "json", "count": 200},
            timeout=20,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
        superevents = data.get("superevents", [])

        targets = []
        retracted = []
        candidates = []

        skipped_nonpublic = 0
        for ev in superevents:
            gps = ev.get("t_0")
            if not gps:
                continue
            labels = ev.get("labels", [])
            gps_f = float(gps)
            gps_rounded = round(gps_f)

            if gps_rounded in known_gps:
                continue

            # Skip events whose strain data isn't public yet (recent O4 events).
            if gps_f > PUBLIC_DATA_GPS_CUTOFF:
                skipped_nonpublic += 1
                continue

            entry = {
                "gps_time": gps_f,
                "detector": detector,
                "mode": "gracedb_candidate",
                "gracedb_id": ev.get("superevent_id"),
                "gracedb_labels": labels,
            }

            if "RETRACTED" in labels:
                retracted.append(entry)
            else:
                candidates.append(entry)

        targets = retracted + candidates
        random.shuffle(targets)
        targets = targets[:limit]

        if targets:
            print(f"GraceDB: {len(retracted)} retracted, {len(candidates)} other, "
                  f"{skipped_nonpublic} skipped (data not public yet). Using {len(targets)}.")
            return targets

    except Exception as e:
        print(f"GraceDB fetch failed: {e}. Falling back to O3 random windows.")

    return _fetch_random_o3_windows(limit=limit, detector=detector)


def _fetch_random_o4_windows(limit: int = 50, detector: str = "H1") -> list[dict]:
    try:
        from gwosc.timeline import get_segments
        segments = get_segments(f"{detector}_DATA", O4_START, int(O4_START + 6e7), cache=True)
        targets = []
        for _ in range(limit * 5):
            if not segments or len(targets) >= limit:
                break
            seg_start, seg_end = random.choice(segments)
            if seg_end - seg_start < 16:
                continue
            gps = random.uniform(seg_start + 4, seg_end - 4)
            targets.append({"gps_time": round(gps, 1), "detector": detector, "mode": "survey"})
        if targets:
            print(f"Fetched {len(targets)} O4 survey windows.")
            return targets
    except Exception as e:
        print(f"O4 segments unavailable: {e}. Using random O4 GPS times.")
    return [
        {"gps_time": float(random.randint(O4_START, O4_START + 60_000_000)), "detector": detector, "mode": "survey"}
        for _ in range(limit)
    ]


def _fetch_random_o3_windows(limit: int = 50, detector: str = "H1") -> list[dict]:
    try:
        from gwosc.timeline import get_segments
        segments = get_segments(f"{detector}_DATA", O3_START, O3_END)
        targets = []
        for _ in range(limit * 5):
            if not segments or len(targets) >= limit:
                break
            seg_start, seg_end = random.choice(segments)
            if seg_end - seg_start < 16:
                continue
            gps = random.uniform(seg_start + 4, seg_end - 4)
            targets.append({"gps_time": round(gps, 1), "detector": detector, "mode": "survey"})
        if targets:
            print(f"Fetched {len(targets)} survey windows from O3 science segments.")
            return targets
    except Exception as e:
        print(f"Could not fetch science segments: {e}. Using random GPS times.")

    return [
        {"gps_time": float(random.randint(O3_START, O3_END)), "detector": detector, "mode": "survey"}
        for _ in range(limit)
    ]


def _fetch_survey_windows(limit: int = 50, detector: str = "H1") -> list[dict]:
    return _fetch_gracedb_triggers(limit=limit, detector=detector)


def _append_experiment(experiment: dict) -> None:
    os.makedirs(os.path.dirname(EXPERIMENTS_FILE), exist_ok=True)
    with open(EXPERIMENTS_FILE, "a") as f:
        f.write(json.dumps(experiment) + "\n")


def _load_recent_experiments(limit: int = 20) -> list[dict]:
    if not os.path.exists(EXPERIMENTS_FILE):
        return []
    with open(EXPERIMENTS_FILE) as f:
        lines = f.readlines()
    recent = lines[-limit:]
    results = []
    for line in recent:
        try:
            results.append(json.loads(line.strip()))
        except json.JSONDecodeError:
            pass
    return results


if __name__ == "__main__":
    run_loop()
