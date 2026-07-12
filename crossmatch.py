"""
External data source cross-matching for LIGO experiment enrichment.

- Fermi GBM: checks NASA's gamma-ray burst catalog for triggers near the GPS time.
  Neutron star mergers produce a gamma-ray burst within seconds of the gravitational
  wave signal. GW170817 was confirmed this way — GW first, GRB 1.7 seconds later.

- LIGO Data Quality: checks GWOSC timeline segments for known environmental
  disturbances / hardware problems at the GPS time. (F3, July 2026: fixed a fully
  inverted flag interpretation and float-GPS bounds that made every query silently
  fail — see check_data_quality.)

- SNEWS / IceCube: looked up in a bundled HISTORICAL catalog (alert_catalogs.json),
  not a live alert API (F4, July 2026). The pipeline analyzes settled history
  (O1-O3), so alerts are a deterministic lookup. The old live endpoint 404'd and
  silently never fired — it failed to find even IC-170922A.

STATUS FIELD (F2): every check reports a three-state `status`:
  - "ok"      : the check ran to completion; its result is meaningful
  - "failed"  : the check errored; its result must be IGNORED (see `error`)
  - "skipped" : the check was not applicable (e.g. GPS outside catalog coverage)
This replaces the old ambiguous `checked: true`, which recorded a FAILED check and a
genuine negative identically. All functions fail gracefully — a failed cross-match
never blocks an experiment.
"""

import os
import json as _json
from dataclasses import dataclass


@dataclass
class FermiResult:
    checked: bool
    trigger_found: bool
    trigger_name: str | None       # e.g. "bn170817529"
    trigger_time_offset_s: float | None  # seconds from our GPS time
    classification: str | None     # GRB, SOLAR_FLARE, TGF, etc.
    t90_s: float | None            # burst duration
    error: str | None = None
    status: str = "ok"             # ok | failed | skipped (F2)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class DataQualityResult:
    checked: bool
    cat1_active: bool   # a real CAT1 PROBLEM is present (data exists but fails CAT1)
    cat2_active: bool   # environmental disturbance (data exists but fails CAT2)
    data_usable: bool   # data exists AND passes CAT1
    flags_found: list[str]  # which specific problems were found
    error: str | None = None
    status: str = "ok"             # ok | failed | skipped (F2)
    has_data: bool | None = None   # whether strain data exists at this time at all

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class SnewsResult:
    checked: bool
    alert_found: bool
    alert_id: str | None           # SNEWS event ID
    alert_time_offset_s: float | None  # seconds between SNEWS alert and our GPS time
    alert_source: str | None       # which detector network contributed
    note: str | None = None        # human-readable context
    error: str | None = None
    status: str = "ok"             # ok | failed | skipped (F2)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class IceCubeResult:
    checked: bool
    alert_found: bool
    event_id: str | None           # IceCube run/event number or alert ID
    alert_time_offset_s: float | None  # seconds between neutrino alert and our GPS time
    signalness: float | None       # 0-1 probability of astrophysical origin
    stream: str | None             # "gold" (>50%) or "bronze" (>30%)
    note: str | None = None        # human-readable context / catalog completeness caveat
    error: str | None = None
    status: str = "ok"             # ok | failed | skipped (F2)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


FERMI_WINDOW_S = 30.0   # seconds either side of GPS time to search for GRB triggers
SNEWS_WINDOW_S = 60.0   # seconds — SNEWS alert timing matches GW within light travel time
ICECUBE_WINDOW_S = 1000.0  # seconds — IceCube sky localization is large, wider window


# --- Historical alert catalogs (F4) ---------------------------------------
_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "alert_catalogs.json")
_ALERT_CATALOGS = None


def _load_alert_catalogs() -> dict:
    """Load and cache the bundled historical alert catalogs."""
    global _ALERT_CATALOGS
    if _ALERT_CATALOGS is None:
        with open(_CATALOG_PATH) as f:
            _ALERT_CATALOGS = _json.load(f)
    return _ALERT_CATALOGS


def _iso_to_gps(iso: str) -> float:
    from astropy.time import Time
    return float(Time(iso, format="isot", scale="utc").gps)


def check_fermi_gbm(gps_time: float) -> FermiResult:
    """
    Query the Fermi GBM trigger catalog (HEASARC FERMIGBRST) for any gamma-ray
    bursts within FERMI_WINDOW_S seconds of the given GPS time.
    A coincident GRB strongly suggests a neutron star merger (BNS) rather than a glitch.
    GW170817 had a confirmed GRB (bn170817529) 1.7 seconds after the gravitational wave.
    """
    try:
        from astroquery.heasarc import Heasarc
        from astropy.time import Time
        import astropy.units as u

        t_center = Time(gps_time, format="gps", scale="utc")
        t_start = Time(gps_time - FERMI_WINDOW_S, format="gps", scale="utc")
        t_end = Time(gps_time + FERMI_WINDOW_S, format="gps", scale="utc")

        # FERMIGBRST stores trigger_time as MJD (float), not ISO string
        adql = f"""SELECT trigger_name, trigger_time, t90, fluence
                   FROM FERMIGBRST
                   WHERE trigger_time >= {t_start.mjd}
                   AND trigger_time <= {t_end.mjd}"""

        h = Heasarc()
        result = h.query_tap(adql)

        if result is None or len(result) == 0:
            return FermiResult(
                checked=True,
                trigger_found=False,
                trigger_name=None,
                trigger_time_offset_s=None,
                classification=None,
                t90_s=None,
            )

        # Find closest trigger to our GPS time
        best_row = None
        best_offset = None
        for row in result:
            try:
                trigger_t = Time(float(row["trigger_time"]), format="mjd", scale="utc")
                offset = abs((trigger_t - t_center).to(u.second).value)
                if best_offset is None or offset < best_offset:
                    best_offset = offset
                    best_row = row
            except Exception:
                continue

        if best_row is None:
            return FermiResult(
                checked=True,
                trigger_found=False,
                trigger_name=None,
                trigger_time_offset_s=None,
                classification=None,
                t90_s=None,
            )

        t90 = None
        try:
            t90 = round(float(best_row["t90"]), 2)
        except Exception:
            pass

        return FermiResult(
            checked=True,
            trigger_found=True,
            trigger_name=str(best_row["trigger_name"]),
            trigger_time_offset_s=round(best_offset, 2),
            classification=None,  # FERMIGBRST doesn't have a classification column
            t90_s=t90,
        )

    except Exception as e:
        return FermiResult(
            checked=False,
            trigger_found=False,
            trigger_name=None,
            trigger_time_offset_s=None,
            classification=None,
            t90_s=None,
            error=str(e),
            status="failed",
        )


def check_data_quality(gps_time: float, detector: str = "H1") -> DataQualityResult:
    """
    Check LIGO data quality at the given GPS time via GWOSC timeline segments.

    IMPORTANT — flag semantics (VERIFIED July 8, 2026 at GW150914, which is clean
    analyzable data where all CAT segments are present over the event): a timeline
    segment being PRESENT at a time means the data PASSES that category (it is good).
    Therefore:
      - data_usable = data exists AND passes CBC_CAT1
      - cat1_active = a real CAT1 PROBLEM = data exists but does NOT pass CAT1
      - cat2_active = environmental disturbance = data exists but does NOT pass CAT2

    The earlier implementation assumed segment-present = problem (fully inverted) and
    passed FLOAT GPS bounds, which GWOSC rejects with HTTP 400 — so every query
    silently failed via `except: continue` and every record's "usable" was untested.
    """
    try:
        from gwosc.timeline import get_segments

        gps = int(round(gps_time))
        start, end = gps - 16, gps + 16  # integer bounds (float → HTTP 400); wider context window

        def _covers(flag: str) -> bool:
            segs = get_segments(flag, start, end)
            return any(s <= gps_time <= e for s, e in segs)

        def _safe_covers(flag: str):
            try:
                return _covers(flag), None
            except Exception as exc:
                return None, f"{flag}: {exc}"

        has_data, e_data = _safe_covers(f"{detector}_DATA")
        passes_cat1, e_c1 = _safe_covers(f"{detector}_CBC_CAT1")
        passes_cat2, e_c2 = _safe_covers(f"{detector}_CBC_CAT2")
        errors = [e for e in (e_data, e_c1, e_c2) if e]

        # CAT1 is the critical answer. If we couldn't get it, the whole check failed.
        if passes_cat1 is None:
            return DataQualityResult(
                checked=False, status="failed", cat1_active=False, cat2_active=False,
                data_usable=True, flags_found=[], has_data=has_data,
                error="; ".join(errors) or "CBC_CAT1 query failed",
            )

        cat1_active = bool(has_data and not passes_cat1)
        cat2_active = bool(has_data and passes_cat2 is False)
        data_usable = bool(has_data and passes_cat1)

        flags_found = []
        if cat1_active:
            flags_found.append(f"{detector}_CBC_CAT1_FAIL")
        if cat2_active:
            flags_found.append(f"{detector}_CBC_CAT2_FAIL")
        if not has_data:
            flags_found.append(f"{detector}_NO_DATA")

        if flags_found:
            print(f"  DQ: {flags_found}")
        else:
            print("  DQ: clean, usable")

        return DataQualityResult(
            checked=True, status="ok", cat1_active=cat1_active, cat2_active=cat2_active,
            data_usable=data_usable, flags_found=flags_found, has_data=bool(has_data),
            error=("; ".join(errors) or None),
        )

    except Exception as e:
        return DataQualityResult(
            checked=False, status="failed", cat1_active=False, cat2_active=False,
            data_usable=True, flags_found=[], has_data=None, error=str(e),
        )


def check_snews_archive(gps_time: float) -> SnewsResult:
    """
    Look up SNEWS (SuperNova Early Warning System) supernova alerts near the GPS
    time in the bundled historical catalog (alert_catalogs.json).

    Archival design (F4): SNEWS has issued zero real alerts since SN 1987A, so the
    catalog is legitimately empty for the LIGO era — a genuine, COMPLETE negative.
    GPS times outside the catalog's coverage window return status 'skipped'.
    """
    try:
        cat = _load_alert_catalogs()["snews"]
        cov_start = _iso_to_gps(cat["coverage_start_iso"])
        cov_end = _iso_to_gps(cat["coverage_end_iso"])

        if not (cov_start <= gps_time <= cov_end):
            return SnewsResult(
                checked=False, status="skipped", alert_found=False, alert_id=None,
                alert_time_offset_s=None, alert_source=None,
                note=f"GPS outside SNEWS catalog coverage "
                     f"({cat['coverage_start_iso']}..{cat['coverage_end_iso']})",
            )

        for a in cat.get("alerts", []):
            offset = abs(float(a["gps"]) - gps_time)
            if offset <= SNEWS_WINDOW_S:
                print(f"  SNEWS: ALERT in catalog — {a['id']} offset={offset:.1f}s")
                return SnewsResult(
                    checked=True, status="ok", alert_found=True, alert_id=str(a["id"]),
                    alert_time_offset_s=round(offset, 2), alert_source=a.get("source"),
                    note="SNEWS GALACTIC SUPERNOVA ALERT — ESCALATE IMMEDIATELY",
                )

        return SnewsResult(
            checked=True, status="ok", alert_found=False, alert_id=None,
            alert_time_offset_s=None, alert_source=None, note=cat.get("completeness"),
        )

    except Exception as e:
        return SnewsResult(
            checked=False, status="failed", alert_found=False, alert_id=None,
            alert_time_offset_s=None, alert_source=None, error=str(e),
        )


def check_icecube_gcn(gps_time: float) -> IceCubeResult:
    """
    Look up IceCube high-energy neutrino alerts near the GPS time in the bundled
    historical catalog (alert_catalogs.json).

    Archival design (F4): a local lookup, not a live GCN query (the old endpoint
    404'd and never fired — it failed to find even IC-170922A). POPULATED July 10,
    2026 from the GCN/AMON notice archives (~217 alerts: EHE + HESE 2016-2019,
    Gold/Bronze 2019-present) — a 'no alert' within coverage is now an authoritative
    negative for the realtime program (see the catalog's 'completeness' for the two
    remaining caveats). Beyond coverage → 'skipped'.
    """
    try:
        cat = _load_alert_catalogs()["icecube"]
        cov_start = _iso_to_gps(cat["coverage_start_iso"])
        cov_end = _iso_to_gps(cat["coverage_end_iso"])

        if not (cov_start <= gps_time <= cov_end):
            return IceCubeResult(
                checked=False, status="skipped", alert_found=False, event_id=None,
                alert_time_offset_s=None, signalness=None, stream=None,
                note=f"GPS outside IceCube catalog coverage "
                     f"({cat['coverage_start_iso']}..{cat['coverage_end_iso']})",
            )

        best = None
        best_offset = None
        for a in cat.get("alerts", []):
            offset = abs(float(a["gps"]) - gps_time)
            if offset <= ICECUBE_WINDOW_S and (best_offset is None or offset < best_offset):
                best_offset = offset
                best = a

        if best is None:
            return IceCubeResult(
                checked=True, status="ok", alert_found=False, event_id=None,
                alert_time_offset_s=None, signalness=None, stream=None,
                note=cat.get("completeness"),
            )

        print(f"  IceCube: ALERT in catalog — {best['id']} offset={best_offset:.1f}s")
        return IceCubeResult(
            checked=True, status="ok", alert_found=True, event_id=str(best["id"]),
            alert_time_offset_s=round(best_offset, 2),
            signalness=best.get("signalness"), stream=best.get("stream"),
            note=best.get("note"),
        )

    except Exception as e:
        return IceCubeResult(
            checked=False, status="failed", alert_found=False, event_id=None,
            alert_time_offset_s=None, signalness=None, stream=None, error=str(e),
        )
