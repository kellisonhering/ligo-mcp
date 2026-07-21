import os
import socket
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from dataclasses import dataclass, field

# Global socket timeout so a hung data download can't freeze the loop forever.
# A stalled network read raises after this many seconds instead of hanging, which
# then surfaces as a normal pipeline error and the loop moves on. (This was the
# likely cause of the loop wedging silently for ~14 hours on July 6, 2026.)
DOWNLOAD_SOCKET_TIMEOUT = 180  # seconds
socket.setdefaulttimeout(DOWNLOAD_SOCKET_TIMEOUT)

WINDOW_SECONDS = 4         # width of the ANALYZED slice (the "outseg")
FETCH_SECONDS = 32         # W3: fetch a WIDER window so PSD/whitening see real noise
                           # (4 s is too short to estimate the background; the same 4 s
                           # was both data and noise estimate → unstable normalization).
                           # We analyze only the central 4 s via q_transform(outseg=…)
                           # so the reported metrics still describe the same slice.
QRANGE = (4, 64)
FRANGE = (20, 2048)
# NOTE (W1): "energy contrast" = peak / median Q-transform tile energy. This is a
# LOUDNESS trigger, not matched-filter SNR — the old field name "snr" was wrong and
# is renamed everywhere as of DETECTION_ALGORITHM_VERSION 2.0.0 / schema_version 3.
CONTRAST_THRESHOLD = 5.0              # excess-power trigger (was SNR_THRESHOLD)
COINCIDENCE_CONTRAST_THRESHOLD = 4.0  # second-detector trigger (was COINCIDENCE_SNR_THRESHOLD)
COINCIDENCE_FREQ_TOLERANCE = 0.3  # peak frequency must agree within 30%
CHIRP_SWEEP_THRESHOLD = 10.0      # Hz/s — minimum sweep rate for normal-mass chirp

# W2: time-of-flight coincidence. A real gravitational wave reaches both LIGO sites
# within ~10 ms (light travel time Hanford↔Livingston); Virgo and KAGRA sit ~25-27 ms
# from the LIGO sites. Peaks further apart in time than light could travel between
# the sites are unrelated local artifacts, no matter how loud. Tolerances below =
# max light-travel time + Q-transform tile-timing slack.
COINCIDENCE_DT_TOLERANCE_S = {
    "H1": 0.015, "L1": 0.015,  # 10 ms flight + 5 ms tile slack
    "V1": 0.040, "K1": 0.040,  # ~27 ms flight + tile slack
}

# Sub-solar mass / primordial black hole mode constants.
# Lighter objects orbit faster and merge at higher frequencies — a 0.5+0.3 Msun
# binary merges around 400–800 Hz rather than 100–200 Hz for stellar-mass BBH.
# The sweep also happens faster, so the threshold is lower.
FRANGE_SUBSOLAR = (20, 4096)
CHIRP_SWEEP_THRESHOLD_SUBSOLAR = 5.0  # Hz/s — lower because sweep rate varies more


@dataclass
class CoincidenceResult:
    checked: bool
    # Second detector (H1↔L1)
    second_detector: str | None
    second_energy_contrast: float | None
    second_peak_freq: float | None
    freq_agreement: bool | None
    coincident: bool
    # W2: arrival-time agreement (time-of-flight test)
    second_peak_time_offset_s: float | None = None
    dt_peak_s: float | None = None       # |t_peak(primary) - t_peak(second)|
    dt_agreement: bool | None = None     # dt within light-travel tolerance
    # Virgo third detector
    virgo_checked: bool = False
    virgo_energy_contrast: float | None = None
    virgo_peak_freq: float | None = None
    virgo_freq_agreement: bool | None = None
    virgo_dt_peak_s: float | None = None
    virgo_dt_agreement: bool | None = None
    virgo_coincident: bool | None = None
    triple_coincident: bool = False  # H1 + L1 + Virgo all agree
    # KAGRA fourth detector
    kagra_checked: bool = False
    kagra_energy_contrast: float | None = None
    kagra_peak_freq: float | None = None
    kagra_freq_agreement: bool | None = None
    kagra_dt_peak_s: float | None = None
    kagra_dt_agreement: bool | None = None
    kagra_coincident: bool | None = None
    quad_coincident: bool = False    # H1 + L1 + Virgo + KAGRA all agree
    error: str | None = None


@dataclass
class PipelineResult:
    gps_time: float
    detector: str
    signal_detected: bool
    peak_frequency_hz: float | None
    peak_time_offset_s: float | None
    energy_contrast: float | None   # peak/median tile energy — loudness, NOT matched-filter SNR
    classification_hint: str | None
    # Frequency sweep — physical chirp signature
    freq_early_hz: float | None
    freq_late_hz: float | None
    freq_sweep_hz_per_s: float | None
    chirp_like: bool
    # Sub-solar mass mode
    subsolar_mode: bool = False
    plot_path: str | None = None
    coincidence: CoincidenceResult | None = None
    error: str | None = None


def _load_injection_spec(spec_path: str) -> dict:
    """
    Load a pre-generated injection spec .npz. Returns {H1, L1, inj_H1, inj_L1, gps}.
    inj_H1 / inj_L1 are numpy arrays sampled at pipeline SAMPLE_RATE, aligned to a
    32 s window centered on `gps`. They are all-zeros for noise-only controls.
    NOTE: this is called from run_pipeline when an injection is requested; the
    pipeline SUMS these into the fetched strain and never tells the planner.
    """
    from campaign_quality import validate_spec_arrays

    with np.load(spec_path) as z:
        pack = {key: z[key] for key in z.files}
    valid, reason = validate_spec_arrays(pack)
    if not valid:
        raise ValueError(f"invalid campaign spec: {reason}")
    return {"H1": pack["H1"], "L1": pack["L1"],
            "inj_H1": pack["inj_H1"], "inj_L1": pack["inj_L1"],
            "gps": float(pack["gps"])}


def run_pipeline(
    gps_time: float,
    detector: str = "H1",
    plot_path: str | None = None,
    subsolar_mode: bool = False,
    injection_spec_path: str | None = None,
) -> PipelineResult:
    frange = FRANGE_SUBSOLAR if subsolar_mode else FRANGE
    chirp_threshold = CHIRP_SWEEP_THRESHOLD_SUBSOLAR if subsolar_mode else CHIRP_SWEEP_THRESHOLD

    try:
        from gwpy.timeseries import TimeSeries

        # W3: fetch a WIDER window for background estimation, analyze the central 4 s.
        fetch_start = gps_time - FETCH_SECONDS / 2
        fetch_end   = gps_time + FETCH_SECONDS / 2
        start = gps_time - WINDOW_SECONDS / 2  # analyzed slice — unchanged
        end   = gps_time + WINDOW_SECONDS / 2

        print(f"  Downloading {detector} strain data: GPS {gps_time:.1f} ({FETCH_SECONDS}s window) {'[PBH MODE]' if subsolar_mode else ''}")
        data = TimeSeries.fetch_open_data(detector, fetch_start, fetch_end, cache=True)

        if data is None or len(data) == 0:
            return PipelineResult(
                gps_time=gps_time, detector=detector,
                signal_detected=False, peak_frequency_hz=None,
                peak_time_offset_s=None, energy_contrast=None,
                classification_hint=None, freq_early_hz=None, freq_late_hz=None,
                freq_sweep_hz_per_s=None, chirp_like=False, subsolar_mode=subsolar_mode,
                error="No data returned from LIGO Open Science Center",
            )
        if not np.isfinite(np.asarray(data.value)).all():
            raise ValueError(f"{detector} strain contains non-finite values")

        # PHASE C: if this run is part of an injection campaign, add the
        # pre-generated waveform into BOTH the primary and coincidence strains.
        # Injection specs pair a 32 s H1+L1 noise window with matching per-detector
        # arrays already scaled to a target network SNR; the "kind: noise" specs
        # have zero-arrays and act as pure-noise controls.
        injection_pack = None
        if injection_spec_path is not None:
            injection_pack = _load_injection_spec(injection_spec_path)
            inj = injection_pack.get(f"inj_{detector}")
            if inj is not None and len(inj) == len(data.value):
                arr = np.array(data.value, dtype=np.float64) + inj
                data = TimeSeries(arr, t0=data.t0, sample_rate=data.sample_rate,
                                  name=data.name, channel=data.channel)

        print(f"  Computing Q-transform (frange={frange[0]}-{frange[1]} Hz), analyzing central {WINDOW_SECONDS}s...")
        qgram = data.q_transform(qrange=QRANGE, frange=frange, outseg=(start, end))

        energy = np.array(qgram.value)
        freqs = qgram.frequencies.value

        peak_energy = float(np.max(energy))
        background = float(np.median(energy))
        energy_contrast = peak_energy / (background + 1e-10)

        peak_idx = np.unravel_index(np.argmax(energy), energy.shape)
        peak_freq = float(freqs[peak_idx[1]])
        peak_time_gps = float(qgram.times[peak_idx[0]].value)
        peak_time_offset = peak_time_gps - gps_time

        signal_detected = energy_contrast >= CONTRAST_THRESHOLD

        if peak_freq < 100:
            classification_hint = "low_frequency"
        elif peak_freq < 500:
            classification_hint = "mid_frequency"
        elif peak_freq < 1500:
            classification_hint = "high_frequency"
        else:
            classification_hint = "very_high_frequency"  # subsolar merger range

        # Frequency sweep — split window in half, compare peak frequencies.
        # A real chirp sweeps upward as the objects spiral faster together.
        # Sub-solar mass binaries sweep faster and into higher frequencies.
        n_times = energy.shape[0]
        first_half = energy[:n_times // 2, :]
        second_half = energy[n_times // 2:, :]
        freq_early = float(freqs[np.argmax(np.max(first_half, axis=0))])
        freq_late = float(freqs[np.argmax(np.max(second_half, axis=0))])
        sweep_rate = (freq_late - freq_early) / (WINDOW_SECONDS / 2)
        chirp_like = sweep_rate >= chirp_threshold

        if plot_path:
            _generate_plot(
                qgram=qgram, gps_time=gps_time, detector=detector,
                energy_contrast=energy_contrast, chirp_like=chirp_like, plot_path=plot_path,
                subsolar_mode=subsolar_mode,
            )

        coincidence = None
        if signal_detected:
            coincidence = _check_coincidence(
                gps_time=gps_time,
                primary_detector=detector,
                primary_peak_freq=peak_freq,
                primary_peak_time_offset=peak_time_offset,
                frange=frange,
                injection_pack=injection_pack,
            )

        return PipelineResult(
            gps_time=gps_time,
            detector=detector,
            signal_detected=signal_detected,
            peak_frequency_hz=round(peak_freq, 2),
            peak_time_offset_s=round(peak_time_offset, 4),
            energy_contrast=round(energy_contrast, 2),
            classification_hint=classification_hint,
            freq_early_hz=round(freq_early, 2),
            freq_late_hz=round(freq_late, 2),
            freq_sweep_hz_per_s=round(sweep_rate, 2),
            chirp_like=chirp_like,
            subsolar_mode=subsolar_mode,
            plot_path=plot_path if plot_path and os.path.exists(plot_path) else None,
            coincidence=coincidence,
        )

    except Exception as e:
        return PipelineResult(
            gps_time=gps_time, detector=detector,
            signal_detected=False, peak_frequency_hz=None,
            peak_time_offset_s=None, energy_contrast=None,
            classification_hint=None, freq_early_hz=None, freq_late_hz=None,
            freq_sweep_hz_per_s=None, chirp_like=False, subsolar_mode=subsolar_mode,
            error=str(e),
        )


def _detector_peak(detector: str, start: float, end: float, frange: tuple, gps_time: float,
                   injection_pack: dict | None = None):
    """
    Download one detector's strain and return (energy_contrast, peak_freq_hz,
    peak_time_offset_s), or None if no data.

    W3: `start`/`end` here bound the ANALYZED slice (typically 4 s around gps_time);
    the actual fetch pulls FETCH_SECONDS around gps_time so the PSD sees enough noise.
    Phase C: if an injection_pack is given, its `inj_{detector}` array is summed into
    the fetched strain BEFORE the Q-transform — same signal in both detectors, offset
    by the correct light-travel delay already baked into the pre-generated arrays.
    """
    from gwpy.timeseries import TimeSeries

    fetch_start = gps_time - FETCH_SECONDS / 2
    fetch_end   = gps_time + FETCH_SECONDS / 2
    data = TimeSeries.fetch_open_data(detector, fetch_start, fetch_end, cache=True)
    if data is None or len(data) == 0:
        return None
    if not np.isfinite(np.asarray(data.value)).all():
        raise ValueError(f"{detector} strain contains non-finite values")

    if injection_pack is not None:
        inj = injection_pack.get(f"inj_{detector}")
        if inj is not None and len(inj) == len(data.value):
            arr = np.array(data.value, dtype=np.float64) + inj
            data = TimeSeries(arr, t0=data.t0, sample_rate=data.sample_rate,
                              name=data.name, channel=data.channel)

    qgram = data.q_transform(qrange=QRANGE, frange=frange, outseg=(start, end))
    energy = np.array(qgram.value)
    peak_energy = float(np.max(energy))
    background = float(np.median(energy))
    contrast = peak_energy / (background + 1e-10)
    peak_idx = np.unravel_index(np.argmax(energy), energy.shape)
    peak_freq = float(qgram.frequencies[peak_idx[1]].value)
    peak_time_offset = float(qgram.times[peak_idx[0]].value) - gps_time
    return contrast, peak_freq, peak_time_offset


def _coincidence_tests(
    detector: str,
    contrast: float,
    peak_freq: float,
    peak_time_offset: float,
    primary_peak_freq: float,
    primary_peak_time_offset: float,
):
    """
    The three coincidence discriminators for one detector pair:
    loud enough, compatible frequency, arrival times within light travel (W2).
    Returns (freq_agreement, dt_peak_s, dt_agreement, coincident).
    """
    freq_ratio = abs(peak_freq - primary_peak_freq) / (primary_peak_freq + 1e-10)
    freq_agreement = freq_ratio <= COINCIDENCE_FREQ_TOLERANCE
    dt_peak = abs(peak_time_offset - primary_peak_time_offset)
    dt_agreement = dt_peak <= COINCIDENCE_DT_TOLERANCE_S.get(detector, 0.040)
    coincident = (
        contrast >= COINCIDENCE_CONTRAST_THRESHOLD and freq_agreement and dt_agreement
    )
    return freq_agreement, round(dt_peak, 4), dt_agreement, coincident


def _check_coincidence(
    gps_time: float,
    primary_detector: str,
    primary_peak_freq: float,
    primary_peak_time_offset: float,
    frange: tuple = FRANGE,
    injection_pack: dict | None = None,
) -> CoincidenceResult:
    """
    Checks H1↔L1, Virgo (V1), and KAGRA (K1) at the same GPS time.
    A real gravitational wave hits all active detectors within light travel time
    (~10 ms H1↔L1, ~27 ms to Virgo/KAGRA) at compatible frequencies. Local
    glitches almost never appear in more than one detector — and when unrelated
    glitches do line up by chance, the arrival-time test (W2) rejects them.
    Quad coincidence (H1+L1+V1+K1) is the strongest possible validation.
    """
    second_detector = "L1" if primary_detector == "H1" else "H1"
    start = gps_time - WINDOW_SECONDS / 2
    end = gps_time + WINDOW_SECONDS / 2

    # --- Second detector (H1 ↔ L1) ---
    second_contrast = None
    second_peak_freq = None
    second_peak_time_offset = None
    freq_agreement = None
    dt_peak = None
    dt_agreement = None
    coincident = False
    error = None

    try:
        print(f"  Coincidence check: downloading {second_detector}...")
        peak = _detector_peak(second_detector, start, end, frange, gps_time, injection_pack)
        if peak is not None:
            second_contrast, second_peak_freq, second_peak_time_offset = peak
            freq_agreement, dt_peak, dt_agreement, coincident = _coincidence_tests(
                second_detector, second_contrast, second_peak_freq,
                second_peak_time_offset, primary_peak_freq, primary_peak_time_offset,
            )
            print(f"  {second_detector}: contrast={second_contrast:.1f} freq={second_peak_freq:.0f}Hz "
                  f"dt={dt_peak * 1000:.0f}ms coincident={coincident}")

    except Exception as e:
        error = str(e)

    # --- Virgo (V1) ---
    virgo_checked = False
    virgo_contrast = None
    virgo_peak_freq = None
    virgo_freq_agreement = None
    virgo_dt_peak = None
    virgo_dt_agreement = None
    virgo_coincident = None

    try:
        print(f"  Coincidence check: downloading V1 (Virgo)...")
        peak = _detector_peak("V1", start, end, frange, gps_time, injection_pack)
        if peak is not None:
            virgo_checked = True
            virgo_contrast, virgo_peak_freq, v_peak_time_offset = peak
            virgo_freq_agreement, virgo_dt_peak, virgo_dt_agreement, virgo_coincident = _coincidence_tests(
                "V1", virgo_contrast, virgo_peak_freq,
                v_peak_time_offset, primary_peak_freq, primary_peak_time_offset,
            )
            print(f"  V1: contrast={virgo_contrast:.1f} freq={virgo_peak_freq:.0f}Hz "
                  f"dt={virgo_dt_peak * 1000:.0f}ms coincident={virgo_coincident}")

    except Exception:
        # Virgo data only available for part of O2 and all of O3 — skip silently if missing
        pass

    # --- KAGRA (K1) ---
    # KAGRA is underground in the Kamioka mine, Japan. Joined O4c in 2025.
    # O4 data is releasing publicly through 2026. KAGRA data is sparse — fail gracefully.
    kagra_checked = False
    kagra_contrast = None
    kagra_peak_freq = None
    kagra_freq_agreement = None
    kagra_dt_peak = None
    kagra_dt_agreement = None
    kagra_coincident = None

    try:
        print(f"  Coincidence check: downloading K1 (KAGRA)...")
        peak = _detector_peak("K1", start, end, frange, gps_time, injection_pack)
        if peak is not None:
            kagra_checked = True
            kagra_contrast, kagra_peak_freq, k_peak_time_offset = peak
            kagra_freq_agreement, kagra_dt_peak, kagra_dt_agreement, kagra_coincident = _coincidence_tests(
                "K1", kagra_contrast, kagra_peak_freq,
                k_peak_time_offset, primary_peak_freq, primary_peak_time_offset,
            )
            print(f"  K1: contrast={kagra_contrast:.1f} freq={kagra_peak_freq:.0f}Hz "
                  f"dt={kagra_dt_peak * 1000:.0f}ms coincident={kagra_coincident}")

    except Exception:
        # KAGRA data is only available for O4 periods — skip silently if missing
        pass

    triple_coincident = bool(coincident and virgo_checked and virgo_coincident)
    quad_coincident = bool(triple_coincident and kagra_checked and kagra_coincident)

    return CoincidenceResult(
        checked=True,
        second_detector=second_detector,
        second_energy_contrast=round(second_contrast, 2) if second_contrast is not None else None,
        second_peak_freq=round(second_peak_freq, 2) if second_peak_freq is not None else None,
        freq_agreement=freq_agreement,
        second_peak_time_offset_s=round(second_peak_time_offset, 4) if second_peak_time_offset is not None else None,
        dt_peak_s=dt_peak,
        dt_agreement=dt_agreement,
        coincident=coincident,
        virgo_checked=virgo_checked,
        virgo_energy_contrast=round(virgo_contrast, 2) if virgo_contrast is not None else None,
        virgo_peak_freq=round(virgo_peak_freq, 2) if virgo_peak_freq is not None else None,
        virgo_freq_agreement=virgo_freq_agreement,
        virgo_dt_peak_s=virgo_dt_peak,
        virgo_dt_agreement=virgo_dt_agreement,
        virgo_coincident=virgo_coincident,
        triple_coincident=triple_coincident,
        kagra_checked=kagra_checked,
        kagra_energy_contrast=round(kagra_contrast, 2) if kagra_contrast is not None else None,
        kagra_peak_freq=round(kagra_peak_freq, 2) if kagra_peak_freq is not None else None,
        kagra_freq_agreement=kagra_freq_agreement,
        kagra_dt_peak_s=kagra_dt_peak,
        kagra_dt_agreement=kagra_dt_agreement,
        kagra_coincident=kagra_coincident,
        quad_coincident=quad_coincident,
        error=error,
    )


def compute_confidence_score(
    signal_detected: bool,
    chirp_like: bool,
    coincidence: CoincidenceResult | None,
    dq_usable: bool | None = None,
    dq_cat2_active: bool | None = None,
) -> float:
    """
    Discriminator-based confidence (W1 v2 — replaces the loudness-derived score).

    Loudness (energy contrast) is deliberately EXCLUDED: glitches are routinely
    louder than real astrophysical signals, so "how loud" carries no information
    about "how real". The old score, min(1, (loudness-3)/15), saturated at 1.0
    for every loud glitch in the dataset. What actually discriminates:
      chirp_like   upward frequency sweep — the physical merger signature
      coincident   second site sees compatible frequency AND arrival time
                   within light travel (W2) — glitches are local
      triple/quad  more continents agreeing — exponentially harder to fake
      data quality CAT flags mark known hardware/environment problems
    Max possible is 0.95 — this pipeline never claims certainty.
    """
    if not signal_detected:
        return 0.0

    score = 0.10  # excess power alone, with no discriminators, is weak evidence
    if chirp_like:
        score += 0.30
    if coincidence is not None and coincidence.coincident:
        score += 0.35
        if coincidence.triple_coincident:
            score += 0.10
        if coincidence.quad_coincident:
            score += 0.05
    if dq_usable:
        score += 0.05
    if dq_cat2_active:
        score -= 0.15
    return round(min(1.0, max(0.0, score)), 3)


def _generate_plot(
    qgram, gps_time: float, detector: str, energy_contrast: float,
    chirp_like: bool, plot_path: str, subsolar_mode: bool = False,
) -> None:
    os.makedirs(os.path.dirname(plot_path), exist_ok=True)

    times_rel = qgram.times.value - gps_time
    freqs = qgram.frequencies.value
    energy = qgram.value.T

    mode_label = "  [PBH MODE]" if subsolar_mode else ""
    chirp_label = "  [CHIRP-LIKE]" if chirp_like else ""
    fig, ax = plt.subplots(figsize=(10, 5))
    mesh = ax.pcolormesh(times_rel, freqs, energy, cmap="viridis", shading="auto")
    ax.set_yscale("log")
    ax.set_ylim(qgram.frequencies.value[[0, -1]])
    ax.set_xlim(times_rel[0], times_rel[-1])
    ax.set_ylabel("Frequency [Hz]")
    ax.set_xlabel(f"Time [s] relative to GPS {gps_time:.1f}")
    ax.set_title(f"{detector} Q-transform  |  GPS {gps_time:.1f}  |  Contrast={energy_contrast:.1f}{chirp_label}{mode_label}")
    plt.colorbar(mesh, ax=ax, label="Normalized Energy")
    plt.tight_layout()
    plt.savefig(plot_path, dpi=100)
    plt.close(fig)
