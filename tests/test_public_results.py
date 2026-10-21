"""Check published evidence and export boundaries without private campaign files."""
import copy
import hashlib
import json
from pathlib import Path
import re

import pytest

from tools.export_campaign_summary import ASSETS, digest, export, public_summary


ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "docs/results"


def published():
    return json.loads((PUBLIC / "summary.json").read_text())


def synthetic_source():
    result = copy.deepcopy(published())
    result.update(demo=False, created_at="2026-01-01T00:00:00+00:00",
                  runtime={"python": "test", "packages": {}, "git_head": "test"},
                  input_snapshot={"truth_values_opened": True, "records_sha256": "opaque",
                                  "manifest_sha256": {}, "source_sha256": {},
                                  "private_path": "/PRIVATE_DO_NOT_EXPORT/"},
                  scored_records=[{"spec_id": "PRIVATE_DO_NOT_EXPORT", "gps_time": 123}],
                  credentials="PRIVATE_DO_NOT_EXPORT")
    result["score_reliability"]["excluded_spec_ids"] = ["PRIVATE_DO_NOT_EXPORT"]
    result["paired_comparison"]["excluded_spec_ids"] = ["PRIVATE_DO_NOT_EXPORT"]
    return result


def test_public_aggregates_have_consistent_denominators_and_bins():
    result = published()
    assert result["counts"]["scored"] == 300
    assert result["counts"]["injections"] == 200
    assert result["counts"]["controls"] == 100
    assert result["counts"]["excluded_invalid_attempts"] == 16
    for metric in result["metrics"].values():
        for name in ("injections", "noise_controls"):
            rate = metric[name]
            assert rate["fraction"] == rate["successes"] / rate["total"]
            assert 0 <= rate["ci95"][0] <= rate["fraction"] <= rate["ci95"][1] <= 1
        curve = metric["efficiency_curve"]
        assert sum(b["total"] for b in curve) == 200
        assert sum(b["successes"] for b in curve) == metric["injections"]["successes"]
        assert [(b["low"], b["high"]) for b in curve] == list(zip(range(4, 24, 2), range(6, 26, 2)))
    assert result["metrics"]["primary"]["snr50"]["value"] is None
    assert result["available_call_comparison"]["injection"]["total"] == 198
    assert result["available_call_comparison"]["noise"]["total"] == 95


def test_public_export_is_checksummed_and_has_no_individual_records():
    names = set()
    for line in (PUBLIC / "SHA256SUMS.txt").read_text().splitlines():
        expected, name = line.split("  ", 1)
        assert Path(name).name == name and digest(PUBLIC / name) == expected
        names.add(name)
    assert names == {"summary.json", *ASSETS}
    text = (PUBLIC / "summary.json").read_text()
    for private in ('"spec_id"', '"gps_time"', '"excluded_spec_ids"', "/Users/", "sk-ant-"):
        assert private not in text


def test_previous_readme_is_preserved_byte_for_byte():
    content = (ROOT / "docs/history/README-July-2026.md").read_bytes()
    blob = b"blob " + str(len(content)).encode() + b"\0" + content
    assert hashlib.sha1(blob).hexdigest() == "83d1fb44fc6929a123e1cd99f7655ee16ca9b51d"


def test_current_documentation_links_resolve():
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if "://" in link or link.startswith("#"):
                continue
            assert (path.parent / link.split("#", 1)[0]).exists(), f"broken link in {path.name}: {link}"


def test_aggregate_export_drops_private_and_per_record_fields():
    public = public_summary(synthetic_source())
    assert "PRIVATE_DO_NOT_EXPORT" not in json.dumps(public)
    with pytest.raises(ValueError, match="real campaign"):
        public_summary(dict(synthetic_source(), demo=True))


def test_export_checks_integrity_before_creating_new_directory(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "results.json").write_text(json.dumps(synthetic_source()))
    (source / "COMPLETE.json").write_text('{"complete":true,"demo":false}')
    for name in ASSETS:
        (source / name).write_bytes(b"invented asset bytes")
    (source / "efficiency.csv").write_bytes(b"invented,header\r\n1,2\r\n")
    (source / "SHA256SUMS.txt").write_text("".join(
        f"{digest(p)}  {p.name}\n" for p in sorted(source.iterdir()) if p.is_file()))
    output = tmp_path / "public"
    export(source, output)
    assert "PRIVATE_DO_NOT_EXPORT" not in (output / "summary.json").read_text()
    assert {p.name for p in output.iterdir()} == {"summary.json", "SHA256SUMS.txt", *ASSETS}
    assert (source / "efficiency.csv").read_bytes() == b"invented,header\r\n1,2\r\n"
    assert (output / "efficiency.csv").read_bytes() == b"invented,header\n1,2\n"
    provenance = json.loads((output / "summary.json").read_text())["provenance"]
    assert provenance["original_asset_sha256"]["efficiency.csv"] == digest(source / "efficiency.csv")
    with pytest.raises(ValueError, match="new output"):
        export(source, output)
    (source / "efficiency.png").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        export(source, tmp_path / "no-output")
    assert not (tmp_path / "no-output").exists()
