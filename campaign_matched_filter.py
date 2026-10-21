"""Truth-template yardstick, invoked only by scoring after unblinding."""
from __future__ import annotations

import math

import numpy as np

from campaign_scoring import ScoringError


def recover_detector(noise, injection, psd, sample_rate=4096.0) -> dict:
    from pycbc.filter import matched_filter, sigma
    from pycbc.psd import interpolate
    from pycbc.types import TimeSeries

    noise, injection = np.asarray(noise), np.asarray(injection)
    if noise.ndim != 1 or noise.shape != injection.shape or not len(noise):
        raise ScoringError("matched-filter arrays have incompatible shapes")
    if not np.isfinite(noise).all() or not np.isfinite(injection).all():
        raise ScoringError("matched-filter arrays contain non-finite samples")
    template = TimeSeries(injection.astype(np.float64), delta_t=1 / sample_rate)
    strain = TimeSeries((noise + injection).astype(np.float64), delta_t=1 / sample_rate)
    spectrum = interpolate(psd, sample_rate / len(noise))
    optimal = float(sigma(template, psd=spectrum, low_frequency_cutoff=20.0))
    if not math.isfinite(optimal) or optimal <= 0:
        raise ScoringError("matched-filter template has zero or invalid norm")
    correlation = matched_filter(template, strain, psd=spectrum, low_frequency_cutoff=20.0)
    # Both arrays already share the injected arrival time; zero lag is index 0.
    recovered = float(abs(correlation[0]))
    if not math.isfinite(recovered):
        raise ScoringError("matched-filter recovery is non-finite")
    return {"optimal_snr": optimal, "recovered_snr_at_injected_time": recovered}


def recover_campaign(rows: list[dict], original_pool, replacement_pool) -> None:
    from gwpy.timeseries import TimeSeries

    from campaign_quality import validate_spec_arrays
    from injections import measured_psd

    for row in rows:
        if row["kind"] != "injection":
            continue
        pool = replacement_pool if row["spec_id"].startswith("replacement_") else original_pool
        with np.load(pool / (row["spec_id"] + ".npz"), allow_pickle=False) as saved:
            arrays = {key: saved[key] for key in ("H1", "L1", "inj_H1", "inj_L1", "gps", "sample_rate")}
        valid, reason = validate_spec_arrays(arrays)
        if not valid:
            raise ScoringError(f"invalid matched-filter spec: {reason}")
        if abs(float(arrays["gps"]) - row["gps_time"]) > 0.00051:
            raise ScoringError("pool GPS disagrees with the recorded experiment")
        detectors = {}
        for detector in ("H1", "L1"):
            noise, injection = arrays[detector], arrays[f"inj_{detector}"]
            psd = measured_psd(TimeSeries(noise, sample_rate=float(arrays["sample_rate"])))
            detectors[detector] = recover_detector(noise, injection, psd)
        optimal = math.sqrt(sum(d["optimal_snr"] ** 2 for d in detectors.values()))
        if not math.isclose(optimal, row["snr"], rel_tol=0, abs_tol=0.002):
            raise ScoringError("recomputed optimal SNR disagrees with the sealed SNR label")
        row["matched_filter"] = {
            "per_detector": detectors, "optimal_network_snr": optimal,
            "recovered_network_snr_at_injected_time": math.sqrt(sum(
                d["recovered_snr_at_injected_time"] ** 2 for d in detectors.values())),
        }
