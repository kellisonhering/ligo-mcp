"""
Public unit tests for the LIGO-MCP campaign's grading + blinding logic.

These tests are PURE: they import `loop` and exercise its decision/blinding
functions on synthetic in-memory summaries. They do NOT read the campaign
pool, campaign_truth.jsonl, pool_gen.log, or any .npz — nothing here touches
the sealed answer key. Pool-count / hash-manifest checks live separately in
tools/audit_campaign.py (a LOCAL audit tool), per the sealing constraints.

Run:  python -m pytest tests/ -q
"""
import pytest

loop = pytest.importorskip("loop", reason="loop.py + its deps must be importable")


# --- §7.5 rule 4: PRIMARY 'caught' = chirp_like OR coincident ---------------

@pytest.mark.parametrize("chirp,coinc,expected", [
    (False, False, False),
    (True,  False, True),
    (False, True,  True),
    (True,  True,  True),
])
def test_primary_caught_is_locked_or_rule(chirp, coinc, expected):
    """physics_only_decision must grade 'caught' as chirp_like OR coincident."""
    _, caught = loop.physics_only_decision(
        {"chirp_like": chirp, "coincident": coinc, "signal_detected": True}
    )
    assert caught is expected


def test_caught_ignores_stricter_legacy_conditions():
    """
    The old (buggy) rule also required dt_agreement and !dq_cat1_active. Under the
    locked OR rule, a chirp alone is caught even with dt_agreement False and a CAT1
    flag present — proving we are on rule 2, not the legacy rule 1.
    """
    _, caught = loop.physics_only_decision({
        "chirp_like": True, "coincident": False,
        "coincidence_dt_agreement": False, "dq_cat1_active": True,
        "signal_detected": True,
    })
    assert caught is True


def test_baseline_rule_version_is_two():
    assert loop.BASELINE_RULE_VERSION == 2


def test_decision_mapping_matches_caught():
    """Caught + known event -> benchmark_validated; caught + unknown -> human review."""
    d1, c1 = loop.physics_only_decision(
        {"chirp_like": True, "coincident": True, "known_event_match": True, "signal_detected": True})
    assert c1 is True and d1 == "benchmark_validated"
    d2, c2 = loop.physics_only_decision(
        {"chirp_like": True, "coincident": False, "signal_detected": True})
    assert c2 is True and d2 == "candidate_for_human_review"
    d3, c3 = loop.physics_only_decision(
        {"chirp_like": False, "coincident": False, "signal_detected": False})
    assert c3 is False and d3 == "archive"


# --- Blinding: _assert_blind rejects any injection-shaped key ----------------

def test_assert_blind_allows_clean_summary():
    loop._assert_blind({
        "gps_time": 1.0, "detector": "H1", "signal_detected": True,
        "chirp_like": False, "coincident": False, "energy_contrast": 3.2,
        "vision_signal_type": "noise",
    })


@pytest.mark.parametrize("bad_key", ["injection", "injected", "spec_id",
                                     "mass1", "target_network_snr", "kind"])
def test_assert_blind_rejects_truth_shaped_keys(bad_key):
    with pytest.raises(RuntimeError):
        loop._assert_blind({"gps_time": 1.0, bad_key: "leak"})


# --- A planner summary must never carry truth fields ------------------------

def test_summary_allowlist_excludes_truth_fields():
    """The allowlist that gates the planner summary must not admit truth fields."""
    prefixes = loop._ALLOWED_SUMMARY_KEY_PREFIXES
    for truth_field in ("injection", "injected", "mass1", "mass2",
                        "target_network_snr", "achieved_network_snr", "kind", "seed"):
        assert not any(truth_field.startswith(p) for p in prefixes), (
            f"{truth_field!r} must not be admissible into the planner summary"
        )
