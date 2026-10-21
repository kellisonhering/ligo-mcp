"""Synthetic scorer tests. No real campaign files, answer keys, or API calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import types

import pytest

import analyze_injections as cli
import campaign_scoring as scoring
from scoring_report import plain_report, write_outputs


def record(sid="spec_0000", chirp=False, coincident=False, score=0.2):
    return {
        "experiment_id": f"experiment_{sid}", "mode": "campaign", "schema_version": 4,
        "gps_time": 1234567890.0, "injection": {"spec_id": sid, "injected": True},
        "detection": {"chirp_like": chirp, "signal_detected": True,
                      "confidence_score": 0.45, "pipeline_error": None},
        "coincidence": {"coincident": coincident, "checked": True, "dt_agreement": False},
        "catalog": {"known_event_match": False}, "vision": {"error": None},
        "llm_review": {"decision": "archive", "interesting_score": score, "planner_error": None},
        "physics_baseline": {"caught": not chirp, "baseline_rule_version": 1},
        "versions": {"pipeline": "1.2.0", "planner_prompt": "1.2.0", "vision_prompt": "1.0.0",
                     "detection_algorithm": "2.1.0", "planner_model": "fake-model",
                     "vision_model": "fake-model", "llm_temperature": 0},
    }


def row(sid, kind="injection", snr=9, **changes):
    out = dict(scoring.grade_record(record(sid)), kind=kind, snr=snr if kind == "injection" else None)
    out.update(changes)
    return out


@pytest.mark.parametrize("chirp,coinc,expected", [(False, False, False), (False, True, True),
                                                 (True, False, True), (True, True, True)])
def test_recomputes_locked_or_rule_ignoring_legacy_baseline(chirp, coinc, expected):
    graded = scoring.grade_record(record(chirp=chirp, coincident=coinc))
    assert graded["primary"] is expected
    assert graded["baseline_primary"] is expected


def test_unchecked_coincidence_is_not_a_detection():
    r = record()
    r["coincidence"].update(coincident=None, checked=False)
    assert scoring.grade_record(r)["primary"] is False
    r["coincidence"]["checked"] = True
    with pytest.raises(scoring.ScoringError, match="unchecked"):
        scoring.grade_record(r)


@pytest.mark.parametrize("decision,caught", [(d, d in scoring.ESCALATIONS) for d in sorted(scoring.DECISIONS)])
def test_secondary_is_only_the_two_locked_escalation_decisions(decision, caught):
    r = record()
    r["llm_review"]["decision"] = decision
    assert scoring.grade_record(r)["secondary"] is caught


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.1, "0.5", True])
def test_invalid_scores_cannot_be_silently_counted(bad):
    with pytest.raises(scoring.ScoringError):
        scoring.grade_record(record(score=bad))


def test_missing_measurement_does_not_default_to_a_miss():
    r = record()
    del r["detection"]["chirp_like"]
    with pytest.raises(scoring.ScoringError):
        scoring.grade_record(r)


def test_wilson_intervals_cover_boundary_cases_and_empty_groups():
    assert scoring.fraction(0, 0)["fraction"] is None
    assert scoring.fraction(0, 100)["ci95"][1] == pytest.approx(0.0369934982)
    assert scoring.fraction(100, 100)["ci95"][0] == pytest.approx(0.9630065018)
    assert scoring.fraction(50, 100)["ci95"] == pytest.approx([0.40383153, 0.59616847])


def test_snr_edges_have_no_double_counting_and_include_24():
    rows = [row(str(i), snr=snr) for i, snr in enumerate((4, 6, 8, 22, 24))]
    curve = scoring.snr_curve(rows, "primary")
    assert sum(b["total"] for b in curve) == 5
    assert curve[0]["total"] == 1
    assert curve[1]["total"] == 1
    assert curve[-1]["total"] == 2
    assert curve[3]["fraction"] is None


def test_snr50_interpolation_gaps_multiple_crossings_and_bounds():
    def bins(values):
        return [{"center": 5 + 2 * i, "fraction": value} for i, value in enumerate(values)]
    assert scoring.snr50(bins([0.2, 0.6]))["value"] == pytest.approx(6.5)
    assert scoring.snr50(bins([0.2, None, 0.8]))["value"] is None
    assert scoring.snr50(bins([0.2, 0.8, 0.2, 0.8]))["status"] == "ambiguous_nonmonotonic_or_flat"
    assert scoring.snr50(bins([0.8, 1]))["value"] is None
    assert scoring.snr50(bins([0.1, 0.2]))["value"] is None
    assert scoring.snr50(bins([0.5, 0.8]))["value"] == 5


@pytest.mark.parametrize("values", [
    [0.8, 0.5, 0.2], [0.8, 0.5], [0.5, 0.2],
    [0.2, 0.5, 0.2], [0.8, 0.5, 0.8],
    [0.2, 0.5, 0.5, 0.8], [None, 0.5, None], [0.5],
    [0.2, 0.8, None, 0.5],
])
def test_exact_half_cannot_create_a_misleading_snr50(values):
    curve = [{"center": 5 + 2 * i, "fraction": value} for i, value in enumerate(values)]
    assert scoring.snr50(curve)["value"] is None


def test_exact_half_upward_crossing_and_downward_diagnostic():
    def curve(values):
        return [{"center": 5 + 2 * i, "fraction": p} for i, p in enumerate(values)]
    assert scoring.snr50(curve([0.2, 0.5, 0.8]))["value"] == 7
    assert scoring.snr50(curve([0.2, 0.5]))["value"] == 7
    assert scoring.snr50(curve([0.8, 0.5, 0.2]))["downward_crossings"] == [7]


def test_truth_join_applies_frozen_replacement_and_ignores_injected_boolean():
    records = [record("spec_0000"), record("spec_0001"), record("replacement_spec_0001")]
    originals = [{"spec_id": "spec_0000", "kind": "noise", "gps_time": 1234567890.0},
                 {"spec_id": "spec_0001", "kind": "injection", "gps_time": 1234567890.0,
                  "achieved_network_snr": 8, "target_network_snr": 8}]
    replacement = [{**originals[1], "spec_id": "replacement_spec_0001", "replaces_spec_id": "spec_0001"}]
    rows, invalid = scoring.join_truth(records, originals, replacement, {"spec_0001"},
                                      {"spec_0000", "spec_0001"}, 1, 1)
    assert len(rows) == 2 and len(invalid) == 1
    assert next(r for r in rows if r["spec_id"] == "spec_0000")["kind"] == "noise"
    assert {r["spec_id"] for r in rows} == {"spec_0000", "replacement_spec_0001"}


@pytest.mark.parametrize("problem", ["duplicate", "missing", "unknown", "gps", "kind", "snr", "truth_duplicate"])
def test_truth_join_refuses_misleading_inputs(problem):
    records = [record("spec_0000"), record("spec_0001")]
    truth = [{"spec_id": "spec_0000", "kind": "injection", "achieved_network_snr": 8,
              "gps_time": 1234567890.0},
             {"spec_id": "spec_0001", "kind": "noise", "gps_time": 1234567890.0}]
    if problem == "duplicate": records.append(copy.deepcopy(records[0]))
    if problem == "missing": records.pop()
    if problem == "unknown": records[0]["injection"]["spec_id"] = "oops"
    if problem == "gps": records[0]["gps_time"] += 1
    if problem == "kind": truth[0]["kind"] = "noise"
    if problem == "snr": truth[0]["achieved_network_snr"] = float("nan")
    if problem == "truth_duplicate": truth.append(copy.deepcopy(truth[0]))
    with pytest.raises(scoring.ScoringError):
        scoring.join_truth(records, truth, [], set(), {"spec_0000", "spec_0001"}, 1, 1)


def test_api_failure_remains_in_operational_total_but_not_paired_or_calibration():
    good = row("good", kind="noise", primary=True, secondary=True)
    broken = record("broken", chirp=True)
    broken["llm_review"]["planner_error"] = "invented API outage"
    bad = dict(scoring.grade_record(broken), kind="noise", snr=None)
    report = scoring.score_rows([good, bad], [])
    assert report["metrics"]["secondary"]["noise_controls"]["total"] == 2
    assert report["metrics"]["secondary"]["noise_controls"]["successes"] == 1
    assert report["paired_comparison"]["noise"]["total"] == 1
    assert report["paired_comparison"]["excluded_api_error_spec_ids"] == ["broken"]
    assert report["score_reliability"]["eligible"] == 1


def test_vision_errors_and_score_one_and_empty_bins():
    failed = record("failed", score=1.0)
    failed["vision"]["error"] = "invented failure"
    rows = [dict(scoring.grade_record(failed), kind="injection", snr=8),
            row("high", kind="noise", interesting_score=1.0)]
    report = scoring.score_rows(rows, [])
    assert report["score_reliability"]["bins"][-1]["injection_fraction"]["total"] == 1
    assert report["score_reliability"]["bins"][0]["injection_fraction"]["fraction"] is None
    assert report["counts"]["vision_errors"] == 1


def test_paired_comparison_counts_both_directions_on_identical_rows():
    rows = [row("a", primary=True, secondary=False), row("b", primary=False, secondary=True),
            row("c", primary=False, secondary=True)]
    result = scoring.paired_comparison(rows)["injection"]
    assert result["physics_only"] == 1 and result["llm_only"] == 2
    assert result["llm_minus_physics"] == pytest.approx(1 / 3)


def test_skipped_ai_is_disclosed_without_changing_locked_denominators():
    broken = record("skipped")
    broken["detection"].update(pipeline_error="invented processing failure", energy_contrast=None)
    broken["vision"] = None
    skipped = dict(scoring.grade_record(broken), kind="injection", snr=8)
    good = row("good", primary=True, secondary=True)
    result = dict(scoring.score_rows([good, skipped], []), demo=True)
    assert result["metrics"]["secondary"]["injections"]["total"] == 2
    assert result["paired_comparison"]["injection"]["total"] == 2
    assert result["paired_comparison"]["included_skipped_call_spec_ids"] == ["skipped"]
    assert result["available_call_comparison"]["injection"]["total"] == 1
    assert result["available_call_comparison"]["excluded_spec_ids"] == ["skipped"]
    assert result["counts"]["llm_skipped"] == 1
    assert "They are not AI judgments" in plain_report(result, [good, skipped])
    broken["detection"]["energy_contrast"] = 3.0
    assert scoring.grade_record(broken)["available_call_eligible"] is True


def fake_campaign(tmp_path, monkeypatch):
    """Full-size invented filesystem: truth bytes are opaque to the readiness check."""
    import campaign_quality
    paths = cli.CampaignPaths(tmp_path / "repo", tmp_path / "data")
    exclusions = {f"spec_{i:04d}" for i in range(52)}
    replacement_ids = {f"replacement_{sid}" for sid in exclusions}
    (paths.repo / "campaign_repair").mkdir(parents=True)
    (paths.repo / "campaign_repair/invalid_specs.json").write_text(json.dumps({"excluded_spec_ids": sorted(exclusions)}))
    for root, ids, seal_dir, extra in (
        (paths.original, cli.ORIGINAL_IDS, "campaign_seal",
         ["campaign_truth.jsonl", "pool_index.json", "pool_gen.log"]),
        (paths.replacement, replacement_ids, "campaign_replacement_seal",
         ["campaign_truth_replacements.jsonl", "pool_index_replacements.json", "pool_gen_replacements.log"]),
    ):
        (root / "pool").mkdir(parents=True)
        names = [f"pool/{sid}.npz" for sid in sorted(ids)] + extra
        lines = []
        for name in names:
            content = b"OPAQUE INVENTED TEST BYTES -- MUST NOT PARSE"
            (root / name).write_bytes(content)
            lines.append(f"{hashlib.sha256(content).hexdigest()}  {name}\n")
        (paths.repo / seal_dir).mkdir()
        (paths.repo / seal_dir / "SHA256SUMS.txt").write_text("".join(lines))
    records = [record(sid) for sid in sorted(cli.ORIGINAL_IDS | replacement_ids)]
    paths.records.write_text("".join(json.dumps(r) + "\n" for r in records))
    monkeypatch.setattr(cli, "SOURCE_FILES", ())
    monkeypatch.setattr(campaign_quality, "validate_spec_file", lambda p: (p.stem not in exclusions, None))
    return paths


def test_blind_readiness_never_parses_either_answer_key(tmp_path, monkeypatch):
    paths = fake_campaign(tmp_path, monkeypatch)
    read = cli.read_jsonl
    def guarded(path):
        assert path == paths.records, "readiness tried to parse a truth file"
        return read(path)
    monkeypatch.setattr(cli, "read_jsonl", guarded)
    snapshot, _, _ = cli.check_campaign(paths)
    assert snapshot["active_records"] == 300
    assert snapshot["excluded_attempts"] == 52
    assert snapshot["truth_values_opened"] is False


def test_tampered_seal_and_incomplete_campaign_block_readiness(tmp_path, monkeypatch):
    paths = fake_campaign(tmp_path, monkeypatch)
    original = paths.records.read_text()
    paths.records.write_text("\n".join(original.splitlines()[1:]) + "\n")
    # First sorted row is a replacement, hence required for completion.
    with pytest.raises(scoring.ScoringError, match="incomplete"):
        cli.check_campaign(paths)
    paths.records.write_text(original)
    (paths.original / "campaign_truth.jsonl").write_text("TAMPERED SYNTHETIC KEY")
    with pytest.raises(scoring.ScoringError, match="changed"):
        cli.check_campaign(paths)


def test_explicit_unblind_switch_required_before_any_real_data_access(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("accessed real data without explicit unblinding")
    monkeypatch.setattr(cli, "check_campaign", forbidden)
    with pytest.raises(SystemExit) as exc:
        cli.main(["score", "--receipt", str(tmp_path / "receipt.json"), "--output", str(tmp_path / "out")])
    assert exc.value.code == 2


def test_demo_builds_reports_without_accessing_campaign(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("demo touched the real campaign")
    monkeypatch.setattr(cli, "check_campaign", forbidden)
    output = tmp_path / "demo"
    assert cli.main(["demo", "--output", str(output)]) == 0
    result = json.loads((output / "results.json").read_text())
    assert result["counts"]["scored"] == 300
    assert result["demo"] is True
    assert "invented" in (output / "REPORT.md").read_text()
    assert (output / "efficiency.png").stat().st_size > 1000
    assert json.loads((output / "COMPLETE.json").read_text())["complete"] is True
    for line in (output / "SHA256SUMS.txt").read_text().splitlines():
        checksum, name = line.split("  ")
        assert checksum == cli.digest(output / name)
    with pytest.raises(SystemExit):
        cli.main(["demo", "--output", str(output)])


def test_real_output_cannot_silently_omit_matched_filter(tmp_path):
    rows, invalid = cli.make_demo()
    result = dict(scoring.score_rows(rows, invalid), demo=False)
    with pytest.raises(scoring.ScoringError, match="yardstick"):
        write_outputs(tmp_path / "out", result, rows, invalid)
    assert not (tmp_path / "out").exists()


def test_report_does_not_treat_primary_as_an_independent_ai_arm():
    rows, invalid = cli.make_demo()
    report = plain_report(dict(scoring.score_rows(rows, invalid), demo=True), rows)
    assert "identical to the recomputed physics-only baseline" in report
    assert "ranking" in report and "200:100" in report


def prepare_synthetic_score(tmp_path, monkeypatch):
    import campaign_matched_filter

    paths = fake_campaign(tmp_path, monkeypatch)
    originals = [{"spec_id": sid, "kind": "injection" if int(sid[-4:]) < 200 else "noise",
                  "gps_time": 1234567890.0, "achieved_network_snr": 8, "target_network_snr": 8}
                 for sid in sorted(cli.ORIGINAL_IDS)]
    replacements = [{**r, "spec_id": "replacement_" + r["spec_id"], "replaces_spec_id": r["spec_id"]}
                    for r in originals[:52]]
    (paths.original / "campaign_truth.jsonl").write_text("".join(json.dumps(r) + "\n" for r in originals))
    (paths.replacement / "campaign_truth_replacements.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in replacements))
    for root, name in ((paths.original, "campaign_seal"), (paths.replacement, "campaign_replacement_seal")):
        manifest = paths.repo / name / "SHA256SUMS.txt"
        names = [line.split("  ", 1)[1] for line in manifest.read_text().splitlines()]
        manifest.write_text("".join(f"{cli.digest(root / p)}  {p}\n" for p in names))
    monkeypatch.setattr(cli, "CampaignPaths", lambda: paths)
    # The analytic tests exercise real PyCBC; this integration isolates CLI/file flow.
    monkeypatch.setitem(sys.modules, "pycbc", types.ModuleType("pycbc"))
    monkeypatch.setitem(sys.modules, "gwpy", types.ModuleType("gwpy"))
    def recover(rows, original_pool, replacement_pool):
        assert original_pool == paths.original / "pool"
        assert replacement_pool == paths.replacement / "pool"
        for r in rows:
            if r["kind"] == "injection":
                r["matched_filter"] = {"recovered_network_snr_at_injected_time": 8.0}
    monkeypatch.setattr(campaign_matched_filter, "recover_campaign", recover)
    return paths


def test_whole_score_path_on_invented_campaign_and_receipt(tmp_path, monkeypatch):
    paths = prepare_synthetic_score(tmp_path, monkeypatch)
    receipt = tmp_path / "ready.json"
    assert cli.main(["check", "--receipt", str(receipt)]) == 0
    output = tmp_path / "score"
    assert cli.main(["score", "--unblind", "--receipt", str(receipt), "--output", str(output)]) == 0
    result = json.loads((output / "results.json").read_text())
    assert result["demo"] is False
    assert result["counts"]["injections"] == 200 and result["counts"]["controls"] == 100
    assert result["input_snapshot"]["truth_values_opened"] is True
    assert (output / "matched_filter.png").exists()
    assert (output / "COMPLETE.json").exists()


def test_changed_receipt_stops_before_parsing_synthetic_truth(tmp_path, monkeypatch):
    paths = prepare_synthetic_score(tmp_path, monkeypatch)
    receipt = tmp_path / "ready.json"
    assert cli.main(["check", "--receipt", str(receipt)]) == 0
    saved = json.loads(receipt.read_text())
    saved["snapshot"]["records_sha256"] = "changed"
    receipt.write_text(json.dumps(saved))
    read = cli.read_jsonl
    def guarded(path):
        assert path == paths.records, "opened truth despite receipt mismatch"
        return read(path)
    monkeypatch.setattr(cli, "read_jsonl", guarded)
    assert cli.main(["score", "--unblind", "--receipt", str(receipt),
                     "--output", str(tmp_path / "score")]) == 1
    assert not (tmp_path / "score").exists()


def test_inputs_changed_during_synthetic_scoring_cannot_produce_results(tmp_path, monkeypatch):
    paths = prepare_synthetic_score(tmp_path, monkeypatch)
    receipt = tmp_path / "ready.json"
    assert cli.main(["check", "--receipt", str(receipt)]) == 0
    import campaign_matched_filter
    recover = campaign_matched_filter.recover_campaign
    def change_records(rows, original, replacement):
        recover(rows, original, replacement)
        paths.records.write_text(paths.records.read_text() + "\n")
    monkeypatch.setattr(campaign_matched_filter, "recover_campaign", change_records)
    assert cli.main(["score", "--unblind", "--receipt", str(receipt),
                     "--output", str(tmp_path / "out")]) == 1
    assert not (tmp_path / "out").exists()


def test_failed_chart_generation_never_marks_report_complete(tmp_path, monkeypatch):
    import scoring_report
    rows, invalid = cli.make_demo()
    result = dict(scoring.score_rows(rows, invalid), demo=True)
    def fail(*args):
        raise OSError("invented disk failure")
    monkeypatch.setattr(scoring_report, "draw_figures", fail)
    output = tmp_path / "partial"
    with pytest.raises(OSError, match="disk failure"):
        write_outputs(output, result, rows, invalid)
    assert (output / "REPORT.md").exists()
    assert not (output / "COMPLETE.json").exists()
