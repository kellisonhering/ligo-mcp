import json
import os
import threading
import subprocess

# Load API keys from ~/.openclaw/.env before any API clients initialize.
def _load_openclaw_env() -> None:
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

from fastmcp import FastMCP

from loop import run_experiment, run_loop, run_pbh_loop, _load_recent_experiments, EXPERIMENTS_FILE
from reporter import generate_daily_summary, REPORTS_DIR
from snews_monitor import start_monitor, stop_monitor, monitor_status
from canary import run_all_canaries

mcp = FastMCP("ligo-pipeline")

# --- Standalone launchd service that runs the survey loop ---
# The survey loop runs as its own macOS background service, independent of the
# OpenClaw gateway, so it survives gateway restarts / sleep and auto-restarts on
# crash. These tools control that service via launchctl instead of running the
# loop as a thread inside this process.
LAUNCHD_LABEL = "com.kellison.ligo-loop"
LAUNCHD_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
_LAUNCHD_DOMAIN = f"gui/{os.getuid()}"
_LAUNCHD_TARGET = f"{_LAUNCHD_DOMAIN}/{LAUNCHD_LABEL}"


def _launchctl(*args: str) -> tuple[int, str]:
    """Run a launchctl command; return (returncode, combined stdout+stderr)."""
    try:
        proc = subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, timeout=30
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _service_loaded() -> bool:
    """True if the loop service is currently loaded in launchd."""
    rc, _ = _launchctl("print", _LAUNCHD_TARGET)
    return rc == 0


_pbh_thread: threading.Thread | None = None
_pbh_running = False


@mcp.tool()
def analyze_target(gps_time: float, detector: str = "H1") -> dict:
    """
    Run the full LIGO pipeline on a single GPS time window.
    Downloads strain data, computes Q-transform, runs vision analysis,
    and returns an LLM research decision.
    """
    return run_experiment(gps_time=gps_time, detector=detector, mode="agent_requested")


@mcp.tool()
def get_experiment_log(limit: int = 10) -> list[dict]:
    """
    Return the most recent LIGO experiment records from the JSONL dataset.
    """
    return _load_recent_experiments(limit=limit)


@mcp.tool()
def get_candidate_reports() -> list[str]:
    """
    List all generated candidate report filenames in data/reports/.
    """
    if not os.path.exists(REPORTS_DIR):
        return []
    return sorted(os.listdir(REPORTS_DIR))


@mcp.tool()
def get_report(filename: str) -> str:
    """
    Read a specific candidate report by filename.
    """
    report_path = os.path.join(REPORTS_DIR, filename)
    if not os.path.exists(report_path):
        return f"Report not found: {filename}"
    with open(report_path) as f:
        return f.read()


@mcp.tool()
def get_daily_summary() -> str:
    """
    Generate a plain-text summary of recent LIGO experiments.
    """
    experiments = _load_recent_experiments(limit=50)
    return generate_daily_summary(experiments)


@mcp.tool()
def check_health() -> dict:
    """
    Run canary self-tests on every external "sense" of the pipeline (Fermi, IceCube,
    data-quality, SNEWS) by replaying moments in history where the answer is known:
    Fermi must find GW170817's gamma-ray burst, IceCube must find the IC-170922A
    neutrino, data-quality must report GW150914 as clean/usable, and the SNEWS catalog
    must load. Use this to confirm the pipeline's external checks are actually working
    and not silently failing. Returns a per-sense pass/FAIL/error report.
    """
    return run_all_canaries()


@mcp.tool()
def start_loop(max_experiments: int | None = None) -> str:
    """
    Start the autonomous LIGO survey loop.

    The loop runs as a standalone macOS background service (launchd),
    independent of the OpenClaw gateway — so it survives gateway restarts and
    the machine sleeping, and auto-restarts if it crashes. It downloads public
    O1-O3 windows, runs the full pipeline, and logs to data/experiments.jsonl.

    Note: the service runs continuously; max_experiments is accepted for
    backward compatibility but ignored. Use stop_loop to stop it.
    """
    if not os.path.exists(LAUNCHD_PLIST):
        return f"Service definition not found at {LAUNCHD_PLIST}. Install the launchd plist first."
    if _service_loaded():
        return "LIGO survey loop service is already running."

    rc, out = _launchctl("bootstrap", _LAUNCHD_DOMAIN, LAUNCHD_PLIST)
    if rc != 0 and "already" not in out.lower():
        # Fall back to legacy load for older macOS
        rc2, out2 = _launchctl("load", "-w", LAUNCHD_PLIST)
        if rc2 != 0 and not _service_loaded():
            return f"Failed to start loop service: {out or out2}"
    return "LIGO survey loop service started (standalone, auto-restarting — survives gateway restarts, sleep, and reboots)."


@mcp.tool()
def stop_loop() -> str:
    """
    Stop the standalone LIGO survey loop service.
    It stays stopped (no auto-restart) until start_loop is called again.
    """
    if not _service_loaded():
        return "LIGO survey loop service is not running."

    rc, out = _launchctl("bootout", _LAUNCHD_TARGET)
    if rc != 0:
        rc2, out2 = _launchctl("unload", LAUNCHD_PLIST)
        if rc2 != 0 and _service_loaded():
            return f"Failed to stop loop service: {out or out2}"
    return "LIGO survey loop service stopped. It will stay stopped until start_loop is called again."


@mcp.tool()
def loop_status() -> dict:
    """
    Check the standalone LIGO survey loop service: whether it's running, how many
    experiments have been logged, and when the most recent one was created (so you
    can tell if it's actually producing data, not just loaded).
    """
    count = 0
    last_created = None
    if os.path.exists(EXPERIMENTS_FILE):
        with open(EXPERIMENTS_FILE) as f:
            lines = [line for line in f if line.strip()]
        count = len(lines)
        if lines:
            try:
                last_created = json.loads(lines[-1]).get("created_at")
            except json.JSONDecodeError:
                pass

    return {
        "loop_running": _service_loaded(),
        "run_mode": "standalone launchd service",
        "service_label": LAUNCHD_LABEL,
        "total_experiments": count,
        "last_experiment_created_at": last_created,
        "batch_running": _batch_running,
        "batch_remaining": _batch_remaining,
    }


_batch_thread: threading.Thread | None = None
_batch_running = False
_batch_remaining = 0


@mcp.tool()
def run_batch(count: int, sleep_seconds: int = 0) -> str:
    """
    Run a FIXED number of survey experiments, then stop automatically. Use this
    for a bounded burst instead of the continuous service — e.g. "run 5 experiments
    and stop." Targets are chosen the same way the survey loop chooses them
    (public O1-O3 windows). Results are logged to data/experiments.jsonl with
    mode="batch".

    count: how many experiments to run (must be >= 1).
    sleep_seconds: gap between experiments (default 0 = back-to-back).

    IMPORTANT: do not run a batch while the continuous loop service is running, or
    two loops will write at once (double cost, interleaved data). Call stop_loop
    first if the service is active. This tool refuses to start if the service is up.
    """
    global _batch_thread, _batch_running, _batch_remaining

    if count < 1:
        return "count must be at least 1."
    if _service_loaded():
        return ("The continuous loop service is running. Stop it first with stop_loop, "
                "then run a batch — otherwise two loops would run at the same time.")
    if _batch_running and _batch_thread and _batch_thread.is_alive():
        return f"A batch is already running ({_batch_remaining} experiments remaining)."

    _batch_running = True
    _batch_remaining = count

    def _run():
        global _batch_running, _batch_remaining
        import time as _time
        from loop import run_experiment, _fetch_survey_windows
        try:
            queue = _fetch_survey_windows(limit=max(count, 50))
            for i in range(count):
                if not queue:
                    break
                target = queue[i % len(queue)]
                gps = target.get("gps_time")
                if gps is None:
                    continue
                run_experiment(gps_time=gps, detector=target.get("detector", "H1"), mode="batch")
                _batch_remaining -= 1
                if _batch_remaining > 0 and sleep_seconds > 0:
                    _time.sleep(sleep_seconds)
        finally:
            _batch_running = False
            _batch_remaining = 0

    _batch_thread = threading.Thread(target=_run, daemon=True)
    _batch_thread.start()
    return (f"Started a batch of {count} survey experiments (gap {sleep_seconds}s between each). "
            "Check loop_status or get_experiment_log to watch progress. It stops on its own when done.")


@mcp.tool()
def start_pbh_survey(max_experiments: int | None = None) -> str:
    """
    Start the primordial black hole (PBH) sub-solar mass survey loop in the background.

    This loop runs in subsolar_mode=True, which means:
    - Wider frequency search range: 20-4096 Hz (vs 20-2048 Hz for normal mode)
    - Lower chirp sweep threshold: 5 Hz/s (vs 10 Hz/s) — sub-solar binaries merge faster
    - Targets GraceDB candidates with potential sub-solar chirp mass estimates
    - Benchmarks use S251112cm (first sub-solar mass GW candidate, November 2025)
    - Every experiment is labeled mode="pbh_survey" in the dataset

    Sub-solar mass black holes cannot form from stellar collapse — any confirmed detection
    would be a primordial black hole (dark matter candidate). This is the frontier of
    what LIGO has ever attempted to detect.
    """
    global _pbh_thread, _pbh_running

    if _pbh_running and _pbh_thread and _pbh_thread.is_alive():
        return "PBH survey loop is already running."

    _pbh_running = True

    def _run():
        global _pbh_running
        try:
            run_pbh_loop(max_experiments=max_experiments)
        finally:
            _pbh_running = False

    _pbh_thread = threading.Thread(target=_run, daemon=True)
    _pbh_thread.start()
    return (
        f"PBH survey loop started. max_experiments={max_experiments}. "
        "Targeting sub-solar mass candidates from GraceDB + S251112cm benchmark. "
        "All results labeled mode=pbh_survey in data/experiments.jsonl."
    )


@mcp.tool()
def stop_pbh_survey() -> str:
    """
    Stop the running PBH survey loop. The current experiment will finish before stopping.
    """
    global _pbh_running
    if not _pbh_running:
        return "PBH survey loop is not running."
    _pbh_running = False
    return "PBH survey stop requested. Will stop after the current experiment finishes."


@mcp.tool()
def pbh_survey_status() -> dict:
    """
    Check whether the PBH survey loop is running.
    """
    pbh_count = 0
    if os.path.exists(EXPERIMENTS_FILE):
        with open(EXPERIMENTS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("subsolar_mode") or record.get("mode", "").startswith("pbh"):
                        pbh_count += 1
                except json.JSONDecodeError:
                    pass

    return {
        "pbh_loop_running": _pbh_running and bool(_pbh_thread and _pbh_thread.is_alive()),
        "pbh_experiments_logged": pbh_count,
        "note": (
            "Sub-solar mass mode targets black holes that cannot form from stellar collapse. "
            "S251112cm (November 2025) is the benchmark — first ever sub-solar mass GW candidate."
        ),
    }


@mcp.tool()
def start_snews_monitor() -> str:
    """
    Start the SNEWS (SuperNova Early Warning System) GCN Kafka monitor.

    SNEWS is a global network of neutrino detectors. When multiple independent
    detectors all see a neutrino burst within 10 seconds, SNEWS fires a public alert
    via GCN — indicating a core-collapse supernova in or near our galaxy.

    When an alert arrives, this monitor immediately runs the full LIGO pipeline at
    the corresponding GPS time on both H1 and L1. This would be one of the first
    independent automated analyses of a galactic supernova gravitational wave signal.

    Setup required (one time):
    1. Register at https://gcn.nasa.gov/ (free)
    2. Create a credential (client ID + client secret)
    3. Add to ~/.openclaw/.env:
         GCN_CLIENT_ID=your_id
         GCN_CLIENT_SECRET=your_secret

    Historical note: No SNEWS alert has been issued since LIGO began observing (2015).
    The last galactic supernova was SN 1987A. This monitor runs silently and waits.
    """
    return start_monitor()


@mcp.tool()
def stop_snews_monitor() -> str:
    """
    Stop the SNEWS GCN Kafka monitor.
    """
    return stop_monitor()


@mcp.tool()
def snews_monitor_status() -> dict:
    """
    Check the status of the SNEWS monitor: whether it is running, the last alert received,
    and whether GCN credentials are configured.
    """
    return monitor_status()


if __name__ == "__main__":
    mcp.run()
