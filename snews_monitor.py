"""
SNEWS 2.0 live monitor — SuperNova Early Warning System integration.

SNEWS is a global network of neutrino detectors. When multiple detectors around
the world all see a burst of neutrinos within 10 seconds of each other, SNEWS
fires a public alert via GCN (NASA's General Coordinates Network). This indicates
a core-collapse supernova in or near our galaxy.

When SNEWS fires:
  - A massive star somewhere within ~100 kpc has just collapsed
  - Neutrinos arrive first (they escape immediately from the core)
  - Gravitational waves arrive at the same time as the neutrinos
  - The optical supernova may not be visible for hours
  - This is the only event where we can observe the collapse itself, not just the aftermath

This monitor subscribes to the GCN Kafka stream for SNEWS alerts. When one arrives,
it immediately triggers a LIGO pipeline analysis at the corresponding GPS time and
generates a priority report. This pipeline would be one of the first independent
automated analyses of a galactic supernova gravitational wave signal.

SETUP REQUIRED:
  1. Register for free at https://gcn.nasa.gov/
  2. Create a new credential (client ID + client secret)
  3. Add to ~/.openclaw/.env:
       GCN_CLIENT_ID=your_client_id_here
       GCN_CLIENT_SECRET=your_client_secret_here

SNEWS topics on GCN Kafka:
  gcn.notices.snews.AlertType    — live SNEWS coincidence alerts
  gcn.notices.snews.Heartbeat    — periodic test pulses (use for connection testing)

Historical note: No SNEWS alert has been issued during the LIGO observing era
(O1 started September 2015). The last galactic supernova was SN 1987A. When the
next one occurs, this monitor will fire within seconds.
"""

import os
import json
import threading
import time
from datetime import datetime, timezone

_monitor_thread: threading.Thread | None = None
_monitor_running = False
_last_alert_gps: float | None = None
_last_alert_id: str | None = None
_total_alerts_processed: int = 0


def _load_gcn_credentials() -> tuple[str | None, str | None]:
    """Load GCN credentials from ~/.openclaw/.env"""
    env_path = os.path.expanduser("~/.openclaw/.env")
    client_id = os.environ.get("GCN_CLIENT_ID")
    client_secret = os.environ.get("GCN_CLIENT_SECRET")

    if env_path and os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GCN_CLIENT_ID="):
                    client_id = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("GCN_CLIENT_SECRET="):
                    client_secret = line.split("=", 1)[1].strip().strip('"')

    return client_id, client_secret


def _parse_snews_gps(message_value: bytes) -> float | None:
    """
    Parse the GPS time from a SNEWS GCN alert.
    SNEWS 2.0 uses a JSON-based schema with an ISO trigger time.
    Returns GPS time or None if parsing fails.
    """
    try:
        data = json.loads(message_value.decode("utf-8"))

        # SNEWS 2.0 schema fields (based on SNEWS GCN documentation)
        trigger_time_str = (
            data.get("trigger_time")
            or data.get("triggerTime")
            or data.get("time_of_signal")
            or data.get("record_time")
        )

        if trigger_time_str:
            from astropy.time import Time
            t = Time(trigger_time_str, format="iso", scale="utc")
            return float(t.gps)

        # Fallback: try to extract p_value and use current time as proxy
        # (for test/heartbeat messages where exact time may not be present)
        return None

    except Exception as e:
        print(f"[SNEWS] Failed to parse alert: {e}")
        return None


def _handle_snews_alert(gps_time: float, alert_data: dict) -> None:
    """
    Triggered when a SNEWS alert arrives. Immediately runs the full pipeline
    at the alert GPS time, generates a priority report, and logs to JSONL.
    """
    global _last_alert_gps, _last_alert_id, _total_alerts_processed

    alert_id = alert_data.get("alert_id") or alert_data.get("id") or "unknown"
    _last_alert_gps = gps_time
    _last_alert_id = alert_id
    _total_alerts_processed += 1

    print(f"\n{'='*60}")
    print(f"[SNEWS] *** ALERT RECEIVED ***")
    print(f"[SNEWS] Alert ID: {alert_id}")
    print(f"[SNEWS] GPS time: {gps_time:.1f}")
    print(f"[SNEWS] This may indicate a galactic supernova.")
    print(f"[SNEWS] Running immediate LIGO pipeline analysis...")
    print(f"{'='*60}\n")

    try:
        from loop import run_experiment

        # Analyze both H1 and L1 immediately — supernova GW is too important to skip
        for detector in ["H1", "L1"]:
            try:
                experiment = run_experiment(
                    gps_time=gps_time,
                    detector=detector,
                    mode="snews_triggered",
                )
                decision = experiment.get("llm_review", {}).get("decision", "unknown")
                score = experiment.get("llm_review", {}).get("interesting_score", 0)
                print(f"[SNEWS] {detector} result: decision={decision} score={score}")
            except Exception as e:
                print(f"[SNEWS] {detector} pipeline failed: {e}")

    except Exception as e:
        print(f"[SNEWS] Failed to run pipeline: {e}")


def start_monitor() -> str:
    """
    Start the SNEWS GCN Kafka monitor in a background thread.
    Requires GCN_CLIENT_ID and GCN_CLIENT_SECRET in ~/.openclaw/.env.
    Register at https://gcn.nasa.gov/ to get credentials (free).
    """
    global _monitor_thread, _monitor_running

    if _monitor_running and _monitor_thread and _monitor_thread.is_alive():
        return "SNEWS monitor is already running."

    client_id, client_secret = _load_gcn_credentials()

    if not client_id or not client_secret:
        return (
            "GCN credentials not found. To set up:\n"
            "1. Register at https://gcn.nasa.gov/ (free)\n"
            "2. Create a new credential (client ID + client secret)\n"
            "3. Add to ~/.openclaw/.env:\n"
            "   GCN_CLIENT_ID=your_id\n"
            "   GCN_CLIENT_SECRET=your_secret\n"
            "4. Restart the LIGO MCP server and try again."
        )

    _monitor_running = True

    def _run():
        global _monitor_running

        try:
            from gcn_kafka import Consumer
        except ImportError:
            print("[SNEWS] gcn-kafka not installed. Run: pip install gcn-kafka")
            _monitor_running = False
            return

        print("[SNEWS] Connecting to GCN Kafka stream...")

        try:
            consumer = Consumer(
                client_id=client_id,
                client_secret=client_secret,
            )

            # Subscribe to SNEWS alert topics
            consumer.subscribe([
                "gcn.notices.snews.AlertType",  # live supernova alerts
                "gcn.notices.snews.Heartbeat",  # periodic test pulses
            ])

            print("[SNEWS] Subscribed to SNEWS topics. Waiting for alerts...")
            print("[SNEWS] (No galactic supernova has been detected since 1987. This monitor is ready and waiting.)")

            while _monitor_running:
                try:
                    messages = consumer.consume(num_messages=1, timeout=5.0)

                    for message in messages:
                        if message.error():
                            print(f"[SNEWS] Kafka error: {message.error()}")
                            continue

                        value = message.value()
                        topic = message.topic()

                        if "Heartbeat" in topic:
                            # Test pulse — connection is alive, don't trigger pipeline
                            print(f"[SNEWS] Heartbeat received ({datetime.now(timezone.utc).isoformat()})")
                            continue

                        # Real alert
                        try:
                            alert_data = json.loads(value.decode("utf-8"))
                        except Exception:
                            alert_data = {}

                        gps_time = _parse_snews_gps(value)

                        if gps_time is not None:
                            _handle_snews_alert(gps_time, alert_data)
                        else:
                            print(f"[SNEWS] Received alert but could not parse GPS time. Raw: {value[:200]}")

                except Exception as e:
                    if _monitor_running:
                        print(f"[SNEWS] Consume error: {e}. Retrying in 30s...")
                        time.sleep(30)

        except Exception as e:
            print(f"[SNEWS] Monitor failed: {e}")
        finally:
            _monitor_running = False
            print("[SNEWS] Monitor stopped.")

    _monitor_thread = threading.Thread(target=_run, daemon=True)
    _monitor_thread.start()

    return (
        "SNEWS monitor started. Subscribed to GCN Kafka stream for supernova alerts.\n"
        "Heartbeat messages will confirm the connection is alive.\n"
        "When a galactic supernova occurs, the pipeline will analyze LIGO data immediately."
    )


def stop_monitor() -> str:
    global _monitor_running
    if not _monitor_running:
        return "SNEWS monitor is not running."
    _monitor_running = False
    return "SNEWS monitor stop requested. Will disconnect after current poll completes."


def monitor_status() -> dict:
    return {
        "monitor_running": _monitor_running and bool(_monitor_thread and _monitor_thread.is_alive()),
        "last_alert_gps": _last_alert_gps,
        "last_alert_id": _last_alert_id,
        "total_alerts_processed": _total_alerts_processed,
        "credentials_configured": all(_load_gcn_credentials()),
        "setup_instructions": (
            "Register at https://gcn.nasa.gov/ then add GCN_CLIENT_ID and "
            "GCN_CLIENT_SECRET to ~/.openclaw/.env"
        ),
    }


if __name__ == "__main__":
    # Run the monitor directly from the terminal for testing
    print(start_monitor())
    try:
        while True:
            time.sleep(60)
            status = monitor_status()
            print(f"[SNEWS] Status: running={status['monitor_running']} alerts={status['total_alerts_processed']}")
    except KeyboardInterrupt:
        print(stop_monitor())
