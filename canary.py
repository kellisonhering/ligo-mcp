"""
Canary tests for the pipeline's external "senses" (F5).

Each sense (Fermi, IceCube, data-quality, SNEWS) is replayed against a moment in
history where its answer is KNOWN. If a sense can no longer produce the known
answer, it has silently broken — exactly the failure that hid behind `checked:
true` on every record before July 2026. These run at loop startup and via the
`check_health` MCP tool. Read-only: they make real external calls but write nothing.

A canary distinguishes two outcomes:
  - "FAIL"  : the sense ran but returned the WRONG answer (it is lying)
  - "error" : the sense could not run (network/transient) — retry, not necessarily a bug
"""
from crossmatch import (
    check_fermi_gbm, check_icecube_gcn, check_data_quality, check_snews_archive,
)

# Known-positive replay targets
GW170817_GPS = 1187008882.4    # Fermi MUST find GRB bn170817529 (~1.7-2 s offset)
IC170922A_GPS = 1190148888.43  # IceCube MUST find the TXS 0506+056 neutrino
GW150914_GPS = 1126259462.4    # data-quality MUST complete and report clean/usable data


def _fermi_canary() -> dict:
    try:
        r = check_fermi_gbm(GW170817_GPS)
        if r.status == "failed":
            return {"status": "error", "detail": f"Fermi check errored: {r.error}"}
        ok = bool(r.trigger_found and r.trigger_name and "170817" in str(r.trigger_name))
        return {
            "status": "pass" if ok else "FAIL",
            "detail": (f"expected GRB bn170817529 near GW170817; got found={r.trigger_found} "
                       f"name={r.trigger_name} offset={r.trigger_time_offset_s}s"),
        }
    except Exception as e:
        return {"status": "error", "detail": f"canary raised: {e}"}


def _icecube_canary() -> dict:
    try:
        r = check_icecube_gcn(IC170922A_GPS)
        if r.status == "failed":
            return {"status": "error", "detail": f"IceCube check errored: {r.error}"}
        ok = bool(r.alert_found)
        return {
            "status": "pass" if ok else "FAIL",
            "detail": f"expected IC-170922A found in catalog; got found={r.alert_found} id={r.event_id}",
        }
    except Exception as e:
        return {"status": "error", "detail": f"canary raised: {e}"}


def _dq_canary() -> dict:
    try:
        r = check_data_quality(GW150914_GPS, "H1")
        if r.status == "failed":
            return {"status": "error", "detail": f"data-quality check errored: {r.error}"}
        # GW150914 is famously clean, analyzable data → must be usable, no CAT1 problem.
        ok = bool(r.status == "ok" and r.data_usable is True and r.cat1_active is False)
        return {
            "status": "pass" if ok else "FAIL",
            "detail": (f"expected clean usable data at GW150914; got status={r.status} "
                       f"usable={r.data_usable} cat1_problem={r.cat1_active} has_data={r.has_data}"),
        }
    except Exception as e:
        return {"status": "error", "detail": f"canary raised: {e}"}


def _snews_canary() -> dict:
    # The SNEWS catalog is legitimately empty for the LIGO era. The canary proves the
    # catalog LOADS and the lookup runs (status ok/skipped), correctly reporting no alert.
    try:
        r = check_snews_archive(GW150914_GPS)
        if r.status == "failed":
            return {"status": "error", "detail": f"SNEWS check errored: {r.error}"}
        ok = bool(r.status in ("ok", "skipped") and r.alert_found is False)
        return {
            "status": "pass" if ok else "FAIL",
            "detail": f"expected catalog to load, no alert; got status={r.status} found={r.alert_found}",
        }
    except Exception as e:
        return {"status": "error", "detail": f"canary raised: {e}"}


def run_all_canaries() -> dict:
    checks = {
        "fermi": _fermi_canary(),
        "icecube": _icecube_canary(),
        "data_quality": _dq_canary(),
        "snews": _snews_canary(),
    }
    n_fail = sum(1 for c in checks.values() if c["status"] == "FAIL")
    n_error = sum(1 for c in checks.values() if c["status"] == "error")
    n_pass = len(checks) - n_fail - n_error
    summary = f"{n_pass}/{len(checks)} senses healthy"
    if n_fail:
        summary += f" — {n_fail} FAILING (returning wrong answers)"
    if n_error:
        summary += f" — {n_error} could not run (transient/network)"
    return {
        "summary": summary,
        "all_healthy": n_fail == 0 and n_error == 0,
        "any_lying": n_fail > 0,
        "checks": checks,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run_all_canaries(), indent=2))
