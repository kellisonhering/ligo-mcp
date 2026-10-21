"""Analytic signal checks; also runnable via unittest in the injection venv."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

import numpy as np

from campaign_matched_filter import recover_campaign, recover_detector
from campaign_scoring import ScoringError


@unittest.skipUnless(importlib.util.find_spec("pycbc"), "requires the injection venv's PyCBC")
class MatchedFilterTests(unittest.TestCase):
    def setUp(self):
        from pycbc.types import FrequencySeries
        self.sample_rate = 4096.0
        time = np.arange(32768) / self.sample_rate
        self.injection = np.sin(2 * np.pi * 100 * time) * np.exp(-((time - 4) / 0.1) ** 2)
        self.psd = FrequencySeries(np.ones(16385), delta_f=1 / 8)

    def test_zero_lag_recovers_exact_signal_norm(self):
        from pycbc.filter import sigma
        from pycbc.types import TimeSeries
        expected = float(sigma(TimeSeries(self.injection, delta_t=1 / self.sample_rate),
                               psd=self.psd, low_frequency_cutoff=20))
        result = recover_detector(np.zeros_like(self.injection), self.injection, self.psd)
        self.assertAlmostEqual(result["optimal_snr"], expected, places=10)
        self.assertAlmostEqual(result["recovered_snr_at_injected_time"], expected, places=10)

    def test_noise_component_is_present_in_recovery(self):
        result = recover_detector(0.5 * self.injection, self.injection, self.psd)
        self.assertAlmostEqual(result["recovered_snr_at_injected_time"],
                               1.5 * result["optimal_snr"], places=10)

    def test_recovery_matches_independent_frequency_domain_inner_product(self):
        from pycbc.types import FrequencySeries

        rng = np.random.default_rng(17)
        noise = rng.normal(0, 0.3, len(self.injection))
        dt = 1 / self.sample_rate
        df = self.sample_rate / len(noise)
        frequencies = np.fft.rfftfreq(len(noise), dt)
        density = 1 + frequencies / 150
        # PyCBC's positive-frequency inner product omits DC and Nyquist.
        band = (frequencies >= 20) & (frequencies < self.sample_rate / 2)
        template = np.fft.rfft(self.injection)[band] * dt
        data = np.fft.rfft(noise + self.injection)[band] * dt
        optimal = np.sqrt(4 * df * np.sum(abs(template) ** 2 / density[band]))
        recovered = abs(4 * df * np.sum(template.conj() * data / density[band])) / optimal
        result = recover_detector(noise, self.injection, FrequencySeries(density, delta_f=df))
        self.assertAlmostEqual(result["optimal_snr"], optimal, places=11)
        self.assertAlmostEqual(result["recovered_snr_at_injected_time"], recovered, places=11)

    def test_zero_template_and_nonfinite_samples_are_rejected(self):
        with self.assertRaises(ScoringError):
            recover_detector(self.injection, np.zeros_like(self.injection), self.psd)
        bad = self.injection.copy()
        bad[10] = np.nan
        with self.assertRaises(ScoringError):
            recover_detector(bad, self.injection, self.psd)

    def test_full_saved_window_uses_measured_psd_and_both_pool_locations(self):
        from gwpy.timeseries import TimeSeries
        from injections import measured_psd, optimal_snr

        rng = np.random.default_rng(123)
        noise = rng.normal(0, 1e-21, 32 * 4096)
        time = np.arange(len(noise)) / 4096
        signal = 1e-21 * np.sin(2 * np.pi * 100 * time) * np.exp(-((time - 16) / 0.1) ** 2)
        spectrum = measured_psd(TimeSeries(noise, sample_rate=4096))
        per_detector_snr = optimal_snr(signal, spectrum, 32)
        signal *= 8 / (np.sqrt(2) * per_detector_snr)
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original"
            replacement = Path(directory) / "replacement"
            original.mkdir()
            replacement.mkdir()
            for sid, pool in (("spec_0000", original), ("replacement_spec_0000", replacement)):
                with self.subTest(sid=sid):
                    np.savez_compressed(pool / f"{sid}.npz", H1=noise, L1=noise, inj_H1=signal,
                                        inj_L1=signal, sample_rate=4096, gps=1234567890.0)
                    rows = [{"spec_id": sid, "kind": "injection", "snr": 8.0, "gps_time": 1234567890.0}]
                    recover_campaign(rows, original, replacement)
                    self.assertAlmostEqual(rows[0]["matched_filter"]["optimal_network_snr"], 8, places=8)
                    rows[0]["snr"] = 10
                    with self.assertRaises(ScoringError):
                        recover_campaign(rows, original, replacement)


if __name__ == "__main__":
    unittest.main()
