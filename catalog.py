import json
import os
import time
from dataclasses import dataclass

# W5 (July 10, 2026): the full confirmed-event catalog is now fetched from GWOSC at
# runtime (~300 events) and cached on disk. The hardcoded list below is the OFFLINE
# FALLBACK only — it stays small and famous. Before W5, a random O3 window landing
# on a lesser-known real event (e.g. GW190425, the second BNS ever) would have been
# filed as an unknown coincident candidate.
GWTC_CACHE_PATH = os.path.expanduser("~/experiment-data/ligo/gwtc_catalog_cache.json")
GWTC_ALLEVENTS_URL = "https://gwosc.org/eventapi/json/allevents/"
GWTC_CACHE_MAX_AGE_S = 30 * 86400  # refresh monthly — new catalogs appear rarely

# GPS times and metadata for confirmed gravitational wave events (GWTC-3 / GWTC-5.0).
# Used as benchmarks — if the pipeline's target GPS time is within MATCH_WINDOW_S
# of a known event, that experiment is treated as a validation run, not a discovery.
GWTC_EVENTS = {
    "GW150914": {"gps": 1126259462.4, "type": "BBH", "mass1": 35.6,  "mass2": 30.6, "run": "O1", "subsolar": False},
    "GW151226": {"gps": 1135136350.6, "type": "BBH", "mass1": 14.2,  "mass2":  7.5, "run": "O1", "subsolar": False},
    "GW170104": {"gps": 1167559936.6, "type": "BBH", "mass1": 31.2,  "mass2": 19.4, "run": "O2", "subsolar": False},
    "GW170608": {"gps": 1180922494.5, "type": "BBH", "mass1": 12.0,  "mass2":  7.0, "run": "O2", "subsolar": False},
    "GW170814": {"gps": 1186741861.5, "type": "BBH", "mass1": 30.5,  "mass2": 25.3, "run": "O2", "subsolar": False},
    "GW170817": {"gps": 1187008882.4, "type": "BNS", "mass1":  1.46, "mass2":  1.27,"run": "O2", "subsolar": False},
    "GW190412": {"gps": 1239082262.2, "type": "BBH", "mass1": 29.7,  "mass2":  8.4, "run": "O3a","subsolar": False},
    "GW190521": {"gps": 1242442967.4, "type": "BBH", "mass1": 85.0,  "mass2": 66.0, "run": "O3a","subsolar": False},
    "GW190814": {"gps": 1249852257.0, "type": "NSBH","mass1": 23.2,  "mass2":  2.6, "run": "O3a","subsolar": False},
    "GW200225": {"gps": 1266378940.0, "type": "BBH", "mass1": 19.3,  "mass2": 13.8, "run": "O3b","subsolar": False},
}

MATCH_WINDOW_S = 30.0  # seconds — GPS time within this of a known event = benchmark

# Sub-threshold and unconfirmed candidates — not in the confirmed catalog.
# These are events that triggered LIGO's pipeline but haven't been formally confirmed,
# or events where confirmation is pending. Wider match window because GPS times are
# often approximate from public reports.
#
# S251112cm: detected November 12, 2025. First ever sub-solar mass GW candidate.
# At least one component weighed less than 1 solar mass. No known stellar collapse
# mechanism produces black holes this small — primordial black hole origin is the
# leading hypothesis. If confirmed, this would be the first direct evidence of
# primordial black holes (which are a dark matter candidate).
# GPS time is approximate (±12 hours). Verify exact time at gwosc.org when available.
SUBSOLAR_CANDIDATES = {
    "S251112cm": {
        "gps": 1446984000.0,   # approx. Nov 12, 2025 noon UTC — verify at gwosc.org
        "type": "PBH_BBH",     # primordial black hole binary black hole
        "mass1": None,         # sub-solar (exact masses pending confirmation)
        "mass2": None,
        "run": "O4",
        "subsolar": True,
        "note": "First sub-solar mass GW candidate. P(mass<1 Msun) > 99%. GPS is approximate.",
    }
}

SUBSOLAR_MATCH_WINDOW_S = 43200.0  # ±12 hours — GPS time is approximate for candidates


def _classify_type(m1, m2) -> str:
    """BNS / NSBH / BBH by source masses (3 solar masses = conventional NS/BH divide)."""
    if m1 is None or m2 is None:
        return "unknown"
    lo, hi = sorted([float(m1), float(m2)])
    if hi < 3.0:
        return "BNS"
    if lo < 3.0:
        return "NSBH"
    return "BBH"


def _run_from_gps(gps: float) -> str:
    if 1126051217 <= gps <= 1137254417:
        return "O1"
    if 1164556817 <= gps <= 1187733618:
        return "O2"
    if 1238166018 <= gps <= 1253977218:
        return "O3a"
    if 1256655618 <= gps <= 1269363618:
        return "O3b"
    if gps >= 1368720018:
        return "O4"
    return "unknown"


def _is_confident_catalog(short_name: str) -> bool:
    """
    Keep only LVK confident/discovery catalogs. Marginal, auxiliary, preliminary,
    and external-group (IAS) triggers are excluded — a "known event match" must
    mean a confirmed detection. Plain "GWTC-2" is excluded because GWTC-2.1
    re-vetted it and demoted some events; resurrecting them would be wrong.
    """
    s = (short_name or "").lower()
    if any(bad in s for bad in ("marginal", "auxiliary", "preliminary", "ias", "initial")):
        return False
    if s == "gwtc-2":
        return False
    return s.startswith("gwtc") or "discovery" in s


def _fetch_allevents() -> dict:
    """Fetch every confident GWTC event from GWOSC → {commonName: event dict}."""
    import requests

    resp = requests.get(GWTC_ALLEVENTS_URL, timeout=60)
    resp.raise_for_status()
    events = {}
    # Sort keys so later catalog versions overwrite earlier ones on dedupe.
    payload = resp.json().get("events", {})
    for key in sorted(payload):
        ev = payload[key]
        if not _is_confident_catalog(str(ev.get("catalog.shortName") or "")):
            continue
        gps = ev.get("GPS")
        if gps is None:
            continue
        name = ev.get("commonName") or key.rsplit("-v", 1)[0]
        m1, m2 = ev.get("mass_1_source"), ev.get("mass_2_source")
        events[name] = {
            "gps": float(gps),
            "type": _classify_type(m1, m2),
            "mass1": m1,
            "mass2": m2,
            "run": _run_from_gps(float(gps)),
            "subsolar": bool(m2 is not None and float(m2) < 1.0),
        }
    return events


_runtime_catalog: dict | None = None


def _load_runtime_catalog() -> dict:
    """
    Full confirmed-event catalog (W5): fresh cache → GWOSC fetch → stale cache →
    hardcoded fallback. Never raises; the worst case is the famous-events list.
    """
    global _runtime_catalog
    if _runtime_catalog is not None:
        return _runtime_catalog

    cached = None
    try:
        with open(GWTC_CACHE_PATH) as f:
            cached = json.load(f)
        if time.time() - cached.get("fetched_at", 0) < GWTC_CACHE_MAX_AGE_S:
            _runtime_catalog = cached["events"]
            return _runtime_catalog
    except Exception:
        cached = None

    try:
        events = _fetch_allevents()
        # Sanity floor: a truncated/failed parse must never shrink the catalog
        # below the hardcoded famous events.
        if len(events) >= len(GWTC_EVENTS):
            os.makedirs(os.path.dirname(GWTC_CACHE_PATH), exist_ok=True)
            tmp = GWTC_CACHE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"fetched_at": time.time(), "source": GWTC_ALLEVENTS_URL,
                           "events": events}, f, indent=1)
            os.replace(tmp, GWTC_CACHE_PATH)
            print(f"  Catalog: fetched {len(events)} confident GWTC events from GWOSC")
            _runtime_catalog = events
            return _runtime_catalog
        print(f"  Catalog: GWOSC returned only {len(events)} events — suspicious, using fallback")
    except Exception as e:
        print(f"  Catalog: GWOSC fetch failed ({e}) — using {'stale cache' if cached else 'hardcoded fallback'}")

    _runtime_catalog = cached["events"] if cached else dict(GWTC_EVENTS)
    return _runtime_catalog


@dataclass
class CatalogResult:
    known_event_match: bool
    event_name: str | None
    event_type: str | None        # BBH, BNS, NSBH, PBH_BBH
    event_gps: float | None
    time_offset_s: float | None
    observing_run: str | None
    is_subsolar: bool = False     # True if at least one component is sub-solar mass
    is_pbh_candidate: bool = False  # True if event is in the subsolar candidates dict
    catalog_error: str | None = None


def check_catalog(gps_time: float) -> CatalogResult:
    try:
        # First check confirmed GWTC events (full runtime catalog — W5)
        best_name = None
        best_offset = None
        best_event = None

        for name, ev in _load_runtime_catalog().items():
            offset = abs(gps_time - ev["gps"])
            if best_offset is None or offset < best_offset:
                best_offset = offset
                best_name = name
                best_event = ev

        if best_offset is not None and best_offset <= MATCH_WINDOW_S:
            return CatalogResult(
                known_event_match=True,
                event_name=best_name,
                event_type=best_event["type"],
                event_gps=best_event["gps"],
                time_offset_s=round(best_offset, 2),
                observing_run=best_event["run"],
                is_subsolar=best_event.get("subsolar", False),
                is_pbh_candidate=False,
            )

        # Then check subsolar / PBH candidates with wider window
        for name, ev in SUBSOLAR_CANDIDATES.items():
            offset = abs(gps_time - ev["gps"])
            if offset <= SUBSOLAR_MATCH_WINDOW_S:
                return CatalogResult(
                    known_event_match=True,
                    event_name=name,
                    event_type=ev["type"],
                    event_gps=ev["gps"],
                    time_offset_s=round(offset, 2),
                    observing_run=ev["run"],
                    is_subsolar=ev.get("subsolar", True),
                    is_pbh_candidate=True,
                )

        return CatalogResult(
            known_event_match=False,
            event_name=None,
            event_type=None,
            event_gps=None,
            time_offset_s=None,
            observing_run=None,
            is_subsolar=False,
            is_pbh_candidate=False,
        )

    except Exception as e:
        return CatalogResult(
            known_event_match=False,
            event_name=None,
            event_type=None,
            event_gps=None,
            time_offset_s=None,
            observing_run=None,
            is_subsolar=False,
            is_pbh_candidate=False,
            catalog_error=str(e),
        )
