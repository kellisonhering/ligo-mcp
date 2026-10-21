"""Development package boundaries, using invented data only."""
import copy
import json

import numpy as np
import pytest

import comparison_prep as prep


def population():
    return [{"spec_id": f"invented_{i:03d}", "kind": "injection" if i < 45 else "noise",
             "snr": (5 if i < 15 else 10 if i < 30 else 24) if i < 45 else None,
             "primary": bool(i % 2), "secondary": bool(i % 3)} for i in range(80)]


def test_selection_balances_strengths_and_never_uses_decisions():
    rows = population()
    first = prep.select_cohort(rows)
    assert len(first) == 30
    assert sum(r["kind"] == "noise" for r in first) == 15
    assert [sum(r["snr"] == strength for r in first) for strength in (5, 10, 24)] == [5, 5, 5]
    changed = copy.deepcopy(rows)
    for row in changed:
        row.update(primary=not row["primary"], secondary=not row["secondary"])
    assert [r["spec_id"] for r in prep.select_cohort(changed)] == [r["spec_id"] for r in first]
    assert [r["spec_id"] for r in prep.select_cohort(list(reversed(rows)))] == [r["spec_id"] for r in first]
    assert len({r["spec_id"] for r in first}) == 30


def test_insufficient_or_duplicate_rosters_are_rejected():
    rows = population()
    with pytest.raises(ValueError, match="duplicate"):
        prep.select_cohort(rows + [rows[0]])
    with pytest.raises(ValueError, match="controls"):
        prep.select_cohort(rows[:50])
    with pytest.raises(ValueError, match="strength"):
        prep.select_cohort(rows[15:])


def measurements():
    return {detector: {"energy_contrast": 20., "signal_detected": True,
                       "peak_frequency_hz": 100., "peak_time_offset_s": t,
                       "freq_early_hz": 50., "freq_late_hz": 100.,
                       "freq_sweep_hz_per_s": 25., "chirp_like": True,
                       "hidden_label": "MUST_NOT_LEAK"}
            for detector, t in (("H1", 0.), ("L1", 0.008))}


def test_model_inputs_use_allowlist_and_equal_context_without_truth():
    record = {"gps_time": "MUST_NOT_LEAK", "injection": {"spec_id": "MUST_NOT_LEAK"},
              "llm_review": {"reasoning": "MUST_NOT_LEAK"},
              "kind": "MUST_NOT_LEAK", "snr": "MUST_NOT_LEAK",
              "coincidence": {"virgo_coincident": "MUST_NOT_LEAK"},
              "crossmatch": {"fermi": "MUST_NOT_LEAK", "data_quality": {
                  "status": "failed", "data_usable": True, "cat1_active": False,
                  "cat2_active": False, "error": "MUST_NOT_LEAK"}}}
    summary = prep.build_summary(measurements(), "H1", record)
    requests = prep.model_requests("case_001", summary)
    assert "MUST_NOT_LEAK" not in json.dumps(requests)
    assert set(summary["detectors"]) == {"H1", "L1"}
    assert summary["data_quality"]["status"] == "failed"
    assert summary["data_quality"]["data_usable"] is None
    assert requests[0]["summary"] == requests[1]["summary"]
    assert requests[0]["system_prompt"] == requests[1]["system_prompt"]
    assert requests[0]["image_paths"] == []
    assert requests[1]["image_paths"] == ["images/case_001.png"]


def test_h1_l1_coincidence_uses_frequency_time_and_power():
    m = measurements()
    assert prep.build_summary(m, "H1", {})["coincidence"]["coincident"]
    m["L1"]["peak_time_offset_s"] = .02
    assert not prep.build_summary(m, "H1", {})["coincidence"]["coincident"]
    m["L1"].update(peak_time_offset_s=.008, peak_frequency_hz=200.)
    assert not prep.build_summary(m, "H1", {})["coincidence"]["coincident"]
    m["L1"].update(peak_frequency_hz=100., energy_contrast=3.)
    assert not prep.build_summary(m, "H1", {})["coincidence"]["coincident"]
    m["H1"]["signal_detected"] = False
    assert prep.build_summary(m, "H1", {})["coincidence"]["checked"] is False
    with pytest.raises(ValueError, match="limited"):
        prep.build_summary(m, "V1", {})


def test_replayed_half_window_heuristic_and_invalid_transforms():
    energy = np.array([[1., 1., 10.], [1., 1., 1.], [1., 1., 1.], [8., 1., 1.]])
    summary = prep.summarize_transform(energy, [50., 100., 150.], [-2., -1., 0., 1.])
    assert summary["freq_sweep_hz_per_s"] == -50.
    assert summary["chirp_like"] is False
    assert summary["peak_time_offset_s"] == -2.
    assert summary["signal_detected"] is True
    energy[0, 0] = np.nan
    with pytest.raises(ValueError, match="invalid"):
        prep.summarize_transform(energy, [50., 100., 150.], [-2., -1., 0., 1.])
    with pytest.raises(ValueError, match="invalid"):
        prep.summarize_transform(np.ones((2, 3)), [50., 100.], [-1., 0.])


def test_preparation_refuses_to_overwrite(tmp_path):
    with pytest.raises(ValueError, match="new output"):
        prep.prepare(tmp_path)
