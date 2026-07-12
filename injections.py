"""
Synthetic gravitational-wave injection engine — Phases A & B
(REVIEW_FINDINGS.md §7.1 plan, §7.3 amendments).

RUNS UNDER THE INJECTION VENV, not system python:
    ~/venvs/ligo-injections/bin/python injections.py

The venv isolates pycbc/lalsuite (which pin older numpy/scipy) from the live
launchd loop. This module GENERATES waveforms and injects them into strain
arrays; it never imports the pipeline. The campaign (Phase D) will precompute
injection arrays here and hand them to the main pipeline as plain .npz files.

Physics summary:
  - get_td_waveform solves the two-body merger (IMRPhenomD) → h+ and h× polarizations
  - Detector(...).project_wave applies each site's antenna response AND the
    light-travel time delay from the geocenter — so H1 and L1 receive the same
    signal a few ms apart, exactly what the pipeline's W2 dt test checks
  - sigma() computes the optimal matched-filter SNR against a PSD, so injections
    can be scaled to a chosen strength; with the PSD measured from the actual
    noise window, that label is the honest "true detectability" of the injection
"""
import argparse
import os

import numpy as np

SAMPLE_RATE = 4096
F_LOWER = 20.0
APPROXIMANT = "IMRPhenomD"
OUT_DIR = os.path.expanduser("~/experiment-data/ligo/injections")


def generate_bbh(mass1: float, mass2: float, distance_mpc: float = 400.0,
                 inclination: float = 0.0, coa_phase: float = 0.0):
    """Generate plus/cross polarizations for a BBH merger. Coalescence at t=0."""
    from pycbc.waveform import get_td_waveform

    hp, hc = get_td_waveform(
        approximant=APPROXIMANT,
        mass1=mass1, mass2=mass2,
        distance=distance_mpc,
        inclination=inclination, coa_phase=coa_phase,
        delta_t=1.0 / SAMPLE_RATE,
        f_lower=F_LOWER,
    )
    return hp, hc


def project_into_window(hp, hc, detector: str, ra: float, dec: float,
                        polarization: float, merger_gps: float,
                        window_start_gps: float, n_samples: int) -> np.ndarray:
    """
    Project the waveform onto one detector (antenna response + light-travel
    delay from geocenter) and place it into a zeros array aligned sample-for-
    sample with the fetched strain window. Returns the injection array
    (add it to the strain elementwise).

    Sub-sample alignment error is ≤ 0.12 ms — negligible against the 15 ms
    W2 timing tolerance.
    """
    from pycbc.detector import Detector

    hp = hp.copy()
    hc = hc.copy()
    # Place coalescence at the requested geocentric GPS time.
    hp.start_time = hp.start_time + merger_gps
    hc.start_time = hc.start_time + merger_gps

    strain = Detector(detector).project_wave(hp, hc, ra, dec, polarization)

    arr = np.zeros(n_samples)
    dt = 1.0 / SAMPLE_RATE
    idx0 = int(round((float(strain.start_time) - window_start_gps) / dt))
    src = strain.numpy()
    # Clip to window bounds
    lo = max(idx0, 0)
    hi = min(idx0 + len(src), n_samples)
    if hi > lo:
        arr[lo:hi] = src[lo - idx0: hi - idx0]
    return arr


def measured_psd(data_gwpy):
    """PSD of the actual noise window (Welch, 4 s segments) as a pycbc FrequencySeries."""
    from pycbc.types import FrequencySeries

    p = data_gwpy.psd(fftlength=4, overlap=2)
    return FrequencySeries(p.value, delta_f=float(p.df.value))


def optimal_snr(injection_arr: np.ndarray, psd, window_seconds: float) -> float:
    """Optimal matched-filter SNR of the injection against the given PSD."""
    from pycbc.types import TimeSeries as PTimeSeries
    from pycbc.psd import interpolate as psd_interpolate
    from pycbc.filter import sigma

    ts = PTimeSeries(injection_arr, delta_t=1.0 / SAMPLE_RATE)
    psd_i = psd_interpolate(psd, 1.0 / window_seconds)
    return float(sigma(ts, psd=psd_i, low_frequency_cutoff=F_LOWER))


def network_snr(per_detector_snrs: dict) -> float:
    return float(np.sqrt(sum(s ** 2 for s in per_detector_snrs.values())))


# ---------------------------------------------------------------------------
# Phase A/B validation (run manually under the venv)
# ---------------------------------------------------------------------------

def phase_a(mass1=30.0, mass2=30.0):
    """Generate one waveform, verify it chirps upward, save a plot."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    hp, hc = generate_bbh(mass1, mass2)
    h = hp.numpy()
    t = hp.sample_times.numpy()

    # The FD approximant pads the buffer with near-zero samples; measure the
    # REAL signal = samples above 1% of peak amplitude.
    loud = np.abs(h) > 0.01 * np.max(np.abs(h))
    sig_duration = float((loud.sum()) / SAMPLE_RATE)

    # Instantaneous frequency from zero crossings, on the loud part only,
    # split into successive thirds — a real chirp must rise.
    idx = np.where(loud)[0]
    seg = h[idx[0]: idx[-1] + 1]

    def freq_in(chunk):
        crossings = np.where(np.diff(np.sign(chunk)))[0]
        if len(crossings) < 2:
            return float("nan")
        return len(crossings) / 2.0 / (len(chunk) / SAMPLE_RATE)

    n = len(seg)
    f_start, f_mid, f_end = (freq_in(seg[: n // 3]),
                             freq_in(seg[n // 3: 2 * n // 3]),
                             freq_in(seg[2 * n // 3:]))

    print(f"Phase A — {mass1}+{mass2} Msun {APPROXIMANT}")
    print(f"  signal duration (above 1% of peak): {sig_duration:.2f} s  (must fit the 4 s window)")
    print(f"  frequency thirds of the audible part: {f_start:.0f} -> {f_mid:.0f} -> {f_end:.0f} Hz")
    chirps = f_start < f_mid < f_end
    print(f"  chirps upward: {chirps}")

    os.makedirs(OUT_DIR, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6))
    ax1.plot(t, h, lw=0.5)
    ax1.set_title(f"Phase A: {mass1}+{mass2} Msun BBH ({APPROXIMANT}) — full buffer")
    ax1.set_ylabel("strain h+")
    zoom = (t >= -0.30) & (t <= 0.05)
    ax2.plot(t[zoom], h[zoom], lw=0.8)
    ax2.set_title("final 0.30 s before coalescence — inspiral, merger, ringdown")
    ax2.set_xlabel("time relative to coalescence [s]")
    ax2.set_ylabel("strain h+")
    plt.tight_layout()
    path = os.path.join(OUT_DIR, "phase_a_waveform.png")
    plt.savefig(path, dpi=100)
    plt.close(fig)
    print(f"  plot: {path}")
    return chirps


def phase_b(target_snrs=(25.0, 6.0), mass1=30.0, mass2=30.0):
    """Inject into real O3 noise at loud + faint strength; save spectrograms."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from gwpy.timeseries import TimeSeries

    candidate_gps = [1240000000.0, 1243000000.0, 1249000000.0, 1256500000.0]
    window = 32.0  # W3-style wide window: PSD needs more than 4 s

    data = {}
    center = None
    for gps in candidate_gps:
        try:
            start, end = gps - window / 2, gps + window / 2
            print(f"Phase B — trying noise window at GPS {gps}...")
            for det in ("H1", "L1"):
                data[det] = TimeSeries.fetch_open_data(det, start, end, cache=True)
            center = gps
            break
        except Exception as e:
            print(f"  no data at {gps} ({e}); trying next")
            data = {}
    if center is None:
        raise RuntimeError("no usable noise window found")

    n = len(data["H1"])
    window_start = center - window / 2
    ra, dec, pol = 1.7, -1.2, 0.6  # fixed sky position for the validation

    hp, hc = generate_bbh(mass1, mass2)

    # Raw injection arrays (same signal, each detector's own response + delay)
    raw = {det: project_into_window(hp, hc, det, ra, dec, pol, center,
                                    window_start, n) for det in ("H1", "L1")}

    # Measured arrival-time difference between sites (sanity: must be < ~10 ms)
    peaks = {det: np.argmax(np.abs(a)) / SAMPLE_RATE for det, a in raw.items()}
    dt_ms = abs(peaks["H1"] - peaks["L1"]) * 1000
    print(f"  H1/L1 injected arrival-time difference: {dt_ms:.1f} ms (light travel: <10 ms)")

    # Scale to target network SNR using the MEASURED PSD of this exact noise
    psds = {det: measured_psd(data[det]) for det in ("H1", "L1")}
    snr1 = {det: optimal_snr(raw[det], psds[det], window) for det in ("H1", "L1")}
    net1 = network_snr(snr1)
    print(f"  unscaled network SNR in this noise: {net1:.1f} "
          f"(H1={snr1['H1']:.1f}, L1={snr1['L1']:.1f})")

    os.makedirs(OUT_DIR, exist_ok=True)
    for target in target_snrs:
        scale = target / net1
        for det in ("H1", "L1"):
            injected = data[det] + raw[det] * scale
            q = injected.q_transform(qrange=(4, 64), frange=(20, 1024),
                                     outseg=(center - 2, center + 2))
            fig, ax = plt.subplots(figsize=(10, 5))
            times_rel = q.times.value - center
            mesh = ax.pcolormesh(times_rel, q.frequencies.value, q.value.T,
                                 cmap="viridis", shading="auto")
            ax.set_yscale("log")
            ax.set_ylabel("Frequency [Hz]")
            ax.set_xlabel(f"Time [s] relative to GPS {center}")
            ax.set_title(f"{det} + injected {mass1}+{mass2} BBH at network SNR {target:g} "
                         f"(measured-PSD scaling)")
            plt.colorbar(mesh, ax=ax, label="Normalized Energy")
            plt.tight_layout()
            path = os.path.join(OUT_DIR, f"phase_b_{det}_snr{target:g}.png")
            plt.savefig(path, dpi=100)
            plt.close(fig)
            print(f"  plot: {path}")

        # Also verify the achieved per-detector SNR after scaling
        achieved = {det: optimal_snr(raw[det] * scale, psds[det], window)
                    for det in ("H1", "L1")}
        print(f"  target net SNR {target:g} -> achieved net {network_snr(achieved):.1f} "
              f"(H1={achieved['H1']:.1f}, L1={achieved['L1']:.1f})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["a", "b", "ab"], default="ab")
    args = parser.parse_args()
    if args.phase in ("a", "ab"):
        ok = phase_a()
        if not ok:
            raise SystemExit("Phase A FAILED: waveform does not chirp upward")
    if args.phase in ("b", "ab"):
        phase_b()
