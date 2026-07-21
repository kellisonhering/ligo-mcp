"""Pure tests for §7.5d validation and replacement scheduling."""
import json

import numpy as np

import campaign_quality
import loop
import pipeline


def _arrays():
    n = campaign_quality.EXPECTED_SAMPLES
    return {
        "H1": np.zeros(n),
        "L1": np.zeros(n),
        "inj_H1": np.zeros(n),
        "inj_L1": np.zeros(n),
        "sample_rate": campaign_quality.EXPECTED_SAMPLE_RATE,
        "gps": 1234567890.0,
    }


def _save_spec(path, arrays=None):
    np.savez_compressed(path, **(arrays or _arrays()))


def test_validate_spec_accepts_finite_arrays(tmp_path):
    path = tmp_path / "spec.npz"
    _save_spec(path)
    assert campaign_quality.validate_spec_file(path) == (True, None)


def test_validate_spec_rejects_nan_in_every_array(tmp_path):
    for key in campaign_quality.REQUIRED_ARRAYS:
        arrays = _arrays()
        arrays[key][10] = np.nan
        path = tmp_path / f"{key}.npz"
        _save_spec(path, arrays)
        valid, reason = campaign_quality.validate_spec_file(path)
        assert valid is False
        assert key in reason


def test_pipeline_refuses_invalid_spec(tmp_path):
    arrays = _arrays()
    arrays["H1"][0] = np.inf
    path = tmp_path / "invalid.npz"
    _save_spec(path, arrays)
    try:
        pipeline._load_injection_spec(path)
    except ValueError as exc:
        assert "invalid campaign spec" in str(exc)
    else:
        raise AssertionError("pipeline accepted a non-finite campaign spec")


def test_scheduler_skips_frozen_original_and_uses_replacement(tmp_path, monkeypatch):
    original_pool = tmp_path / "original"
    replacement_pool = tmp_path / "replacement"
    original_pool.mkdir()
    replacement_pool.mkdir()
    _save_spec(original_pool / "spec_0009.npz")
    _save_spec(replacement_pool / "replacement_spec_0009.npz")

    exclusions = tmp_path / "invalid_specs.json"
    exclusions.write_text(json.dumps({"excluded_spec_ids": ["spec_0009"]}))
    monkeypatch.setattr(campaign_quality, "EXCLUDED_SPECS_FILE", exclusions)
    monkeypatch.setattr(
        campaign_quality,
        "load_excluded_spec_ids",
        lambda path=exclusions: {"spec_0009"},
    )
    monkeypatch.setattr(loop, "CAMPAIGN_POOL_DIR", str(original_pool))
    monkeypatch.setattr(loop, "CAMPAIGN_REPLACEMENT_POOL_DIR", str(replacement_pool))
    monkeypatch.setattr(loop, "CAMPAIGN_FILE", str(tmp_path / "records.jsonl"))

    target = loop._next_campaign_target()
    assert target["spec_id"] == "replacement_spec_0009"


def test_exclusion_file_declares_52_unique_specs():
    excluded = campaign_quality.load_excluded_spec_ids()
    assert len(excluded) == 52
    assert all(spec_id.startswith("spec_") for spec_id in excluded)
