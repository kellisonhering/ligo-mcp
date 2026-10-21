"""Exact request preparation is local, paired, answer-free, and bounded in size."""
import json
from pathlib import Path
import socket
import struct

import pytest

import fable_batch_prep as prep


def fake_package(tmp_path):
    package = tmp_path / "package"
    (package / "images").mkdir(parents=True)
    rows = []
    # Header-only image bytes exercise local request assembly, never an API call.
    png = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 1440, 564)
    for i in range(1, 31):
        case = f"case_{i:03d}"
        (package / "images" / f"{case}.png").write_bytes(png)
        for arm in sorted(prep.ARMS):
            rows.append({"case_id": case, "arm": arm, "system_prompt": "Uniform invented test prompt",
                         "summary": {"detectors": {"H1": {"chirp_like": True}, "L1": {"chirp_like": False}}},
                         "image_paths": [f"images/{case}.png"] if arm == "numbers_and_images" else []})
    (package / "model_inputs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (package / "private_answer_key.jsonl").write_text("SECRET_TEST_ANSWER_KEY_DO_NOT_PARSE")
    (package / "COMPLETE.json").write_text('{"complete":true}')
    seal(package)
    return package, rows


def seal(package):
    files = sorted(p for p in package.rglob("*") if p.is_file() and p.name != "SHA256SUMS.txt")
    (package / "SHA256SUMS.txt").write_text("".join(
        f"{prep.digest(p)}  {p.relative_to(package)}\n" for p in files))


def test_groups_cover_all_cases_once_without_cherry_picking(tmp_path):
    _, rows = fake_package(tmp_path)
    groups = [prep.select_group(list(reversed(rows)), i) for i in range(1, 7)]
    assert all(len(g) == 10 for g in groups)
    ids = [(r["case_id"], r["arm"]) for group in groups for r in group]
    assert len(set(ids)) == 60
    assert {r["case_id"] for r in groups[0]} == {f"case_{i:03d}" for i in range(1, 6)}
    for bad in (0, 7):
        with pytest.raises(ValueError, match="between"):
            prep.select_group(rows, bad)
    with pytest.raises(ValueError, match="paired"):
        prep.select_group(rows[:-1], 1)
    with pytest.raises(ValueError, match="duplicate"):
        prep.select_group(rows + [rows[0]], 1)


def test_real_shape_batch_is_local_and_never_parses_answers(tmp_path, monkeypatch):
    package, _ = fake_package(tmp_path)
    read = Path.read_text
    def guarded_read(path, *args, **kwargs):
        assert path.name != "private_answer_key.jsonl"
        return read(path, *args, **kwargs)
    def no_network(*args, **kwargs):
        raise AssertionError("preparation attempted a network request")
    monkeypatch.setattr(Path, "read_text", guarded_read)
    monkeypatch.setattr(socket, "socket", no_network)
    output = tmp_path / "group"
    review = prep.prepare(output, package=package)
    body = json.loads((output / "batch_requests.json").read_text())
    assert len(body["requests"]) == 10
    assert "SECRET_TEST_ANSWER_KEY" not in json.dumps(body)
    assert review["API_calls_made"] == 0 and review["budget_approved"] is False
    assert review["cost_scenarios"][-1]["conservative_batch_usd"] < 1.50
    images = 0
    for r in body["requests"]:
        p = r["params"]
        assert p["model"] == "claude-fable-5-1" and p["max_tokens"] == 4096
        assert p["output_config"]["effort"] == "high"
        assert "temperature" not in p and "tools" not in p
        images += sum(b["type"] == "image" for b in p["messages"][0]["content"])
    assert images == 5
    with pytest.raises(ValueError, match="new output"):
        prep.prepare(output, package=package)


def test_tampered_assets_and_answer_fields_are_rejected(tmp_path):
    package, rows = fake_package(tmp_path)
    (package / "images/case_001.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        prep.prepare(tmp_path / "out", package=package)
    r = rows[0]
    r["summary"]["kind"] = "injection"
    with pytest.raises(ValueError, match="answer"):
        prep.make_request(r, package, 4096)
    r["summary"].pop("kind")
    r["image_paths"] = ["private_answer_key.jsonl"]
    with pytest.raises(ValueError, match="image path"):
        prep.make_request(r, package, 4096)


def test_oversized_group_budget_fails_before_writing_output(tmp_path):
    package, rows = fake_package(tmp_path)
    for row in rows:
        row["system_prompt"] = "x" * 100_000
    (package / "model_inputs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    seal(package)
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="planning budget"):
        prep.prepare(output, package=package)
    assert not output.exists()


def test_paired_input_mismatch_is_not_a_valid_comparison(tmp_path):
    package, rows = fake_package(tmp_path)
    rows[0]["summary"] = {"different": True}
    (package / "model_inputs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    seal(package)
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="identical"):
        prep.prepare(output, package=package)
    assert not output.exists()
