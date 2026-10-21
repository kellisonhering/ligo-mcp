"""Prepare one small Fable batch locally. This module has no network client."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct


PACKAGE = Path.home() / "experiment-data/ligo/analysis/campaign-comparison-prep-v1"
MODEL = "claude-fable-5-1"
ARMS = {"numbers_only", "numbers_and_images"}
OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["archive", "glitch_candidate", "follow_up",
                                                   "candidate_for_human_review"]},
        "interesting_score": {"type": "number"},
        "reasoning": {"type": "string"},
        "human_review_required": {"type": "boolean"},
    },
    "required": ["decision", "interesting_score", "reasoning", "human_review_required"],
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_package(package):
    for line in (package / "SHA256SUMS.txt").read_text().splitlines():
        expected, name = line.split("  ", 1)
        path = package / name
        if (Path(name).is_absolute() or ".." in Path(name).parts
                or path.resolve() != package.resolve() / name or digest(path) != expected):
            raise ValueError("prepared comparison failed its integrity check")
    if not json.loads((package / "COMPLETE.json").read_text()).get("complete"):
        raise ValueError("comparison package is incomplete")


def select_group(rows, group):
    if group not in range(1, 7):
        raise ValueError("group must be between 1 and 6")
    indexed = {}
    for row in rows:
        key = row["case_id"], row["arm"]
        if key in indexed:
            raise ValueError("duplicate case/arm")
        indexed[key] = row
    expected = {(f"case_{i:03d}", arm) for i in range(1, 31) for arm in ARMS}
    if set(indexed) != expected:
        raise ValueError("expected exactly 30 paired cases")
    start = (group - 1) * 5 + 1
    return [indexed[f"case_{i:03d}", arm]
            for i in range(start, start + 5) for arm in sorted(ARMS)]


def assert_blind(value):
    forbidden = {"kind", "spec_id", "snr", "gps_time", "injection", "versions",
                 "original_primary", "original_secondary", "llm_review", "vision"}
    if isinstance(value, dict):
        if forbidden & value.keys():
            raise ValueError("answer or historical decision field in model input")
        for nested in value.values():
            assert_blind(nested)
    elif isinstance(value, list):
        for nested in value:
            assert_blind(nested)


def make_request(row, package, max_tokens):
    if set(row) != {"case_id", "arm", "system_prompt", "summary", "image_paths"}:
        raise ValueError("unexpected model input fields")
    assert_blind(row["summary"])
    expected_images = [f"images/{row['case_id']}.png"] if row["arm"] == "numbers_and_images" else []
    if row["image_paths"] != expected_images:
        raise ValueError("unexpected image path or arm")
    content, visual_tokens = [], 0
    for name in expected_images:
        path = package / name
        if path.resolve() != package.resolve() / name:
            raise ValueError("image must remain in the prepared package")
        image = path.read_bytes()
        if image[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("expected PNG image")
        width, height = struct.unpack(">II", image[16:24])
        tokens = math.ceil(width / 28) * math.ceil(height / 28)
        if max(width, height) > 2576 or tokens > 4784:
            raise ValueError("image exceeds the model's native resolution; re-estimate first")
        visual_tokens += tokens
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.b64encode(image).decode("ascii")}})
    text = f"Assess {row['case_id']} using the supplied evidence.\n" + json.dumps(row["summary"], indent=2)
    content.append({"type": "text", "text": text})
    params = {
        "model": MODEL, "max_tokens": max_tokens, "inference_geo": "global",
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        "system": row["system_prompt"], "messages": [{"role": "user", "content": content}],
    }
    # Count text/schema bytes separately: base64 image bytes are not text tokens.
    text_bytes = len((row["system_prompt"] + text + json.dumps(OUTPUT_SCHEMA)).encode("utf-8"))
    estimates = {
        "custom_id": f"{row['case_id']}_{row['arm']}", "image_visual_tokens": visual_tokens,
        "text_bytes": text_bytes,
        "nominal_input_tokens": math.ceil(text_bytes / 4 * 1.3) + 512 + visual_tokens,
        "conservative_input_tokens": text_bytes + 512 + visual_tokens,
    }
    return {"custom_id": estimates["custom_id"], "params": params}, estimates


def prepare(output, group=1, max_tokens=4096, package=PACKAGE):
    if output.exists():
        raise ValueError("choose a new output directory")
    if max_tokens != 4096:
        raise ValueError("this reviewed preparation uses a 4096-token output limit")
    verify_package(package)
    rows = [json.loads(line) for line in (package / "model_inputs.jsonl").read_text().splitlines() if line.strip()]
    chosen = select_group(rows, group)
    requests, estimates = [], []
    for row in chosen:
        request, estimate = make_request(row, package, max_tokens)
        requests.append(request)
        estimates.append(estimate)
    for i in range(0, len(chosen), 2):
        if chosen[i]["summary"] != chosen[i + 1]["summary"] or chosen[i]["system_prompt"] != chosen[i + 1]["system_prompt"]:
            raise ValueError("paired examples must have identical numbers and prompt")
    n = len(requests)
    input_nominal = sum(e["nominal_input_tokens"] for e in estimates)
    input_conservative = sum(e["conservative_input_tokens"] for e in estimates)
    costs = [{"output_tokens_per_request_including_thinking": tokens,
              "nominal_batch_usd": (input_nominal * 5 + n * tokens * 25) / 1e6,
              "conservative_batch_usd": (input_conservative * 5 + n * tokens * 25) / 1e6}
             for tokens in (512, 1536, max_tokens)]
    review = {
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "prepared_not_submitted",
        "model": MODEL, "group": group, "examples": 5, "requests": n,
        "max_output_tokens_per_request": max_tokens, "effort": "high",
        "batch_rate_per_million_tokens": {"input": 5, "output": 25},
        "proposed_one_group_budget_usd_before_tax": 1.50,
        "budget_approved": False, "weekly_budget_agreed": False,
        "account_access_or_credits_checked": False, "API_calls_made": 0,
        "input_tokens": {"nominal_estimate": input_nominal, "conservative_planning_estimate": input_conservative},
        "cost_scenarios": costs,
        "notes": ["Estimates, not a server-side token count or enforced spending cap.",
                  "Nominal text tokens use bytes/4*1.3; conservative uses one token per text byte, plus 512 overhead per request.",
                  "All thinking counts toward the proposed output allowance. Truncated responses must be treated as incomplete.",
                  "No retries, tool usage, taxes, cache savings, or paid submission are included.",
                  "This is an exploratory group from an already open development sample; inspect aggregate results after all six groups.",
                  "Before paid submission, count input tokens for the exact requests, verify current rates and available model access, and check the agreed budget.",
                  "Create a durable submission record and disable automatic creation retries to avoid accidental duplicate paid batches."],
        "source_model_inputs_sha256": digest(package / "model_inputs.jsonl"),
        "preparation_script_sha256": digest(Path(__file__)), "request_estimates": estimates,
        "documentation": ["https://platform.claude.com/docs/en/models/fable-5-1/overview",
                          "https://platform.claude.com/docs/en/api/messages/batches/create",
                          "https://platform.claude.com/docs/en/build-with-claude/vision"],
    }
    if costs[-1]["conservative_batch_usd"] > review["proposed_one_group_budget_usd_before_tax"]:
        raise ValueError("prepared group exceeds the proposed $1.50 planning budget")
    output.mkdir(parents=True, exist_ok=False)
    (output / "batch_requests.json").write_text(json.dumps({"requests": requests}, allow_nan=False) + "\n")
    (output / "cost_review.json").write_text(json.dumps(review, indent=2, allow_nan=False) + "\n")
    (output / "README.md").write_text(
        f"# Fable group {group}: ready for review\n\n"
        "Five examples, each with numbers alone and with numbers plus images: ten requests total. "
        "The model is Claude Fable 5.1 at high effort. No answer key is included in the requests.\n\n"
        "The exact API request body is in batch_requests.json. It contains images as base64. "
        "cost_review.json records the cost scenarios and the proposed $1.50 pre-tax budget for this one group. "
        "That amount is not yet approved or enforced. No weekly spending schedule is configured.\n\n"
        "Nothing has been submitted, no credentials were loaded, and no API calls were made. "
        "This tool only prepares local files. Before submitting, verify exact input token counts, model access, "
        "current rates, and an agreed spending limit. Disable automatic creation retries and save the returned "
        "batch ID so future checks resume the same batch instead of creating a duplicate.\n\n"
        "Use a new directory for each group. Groups 1-6 cover the fixed 30-example sample in order. "
        "Do not selectively skip later groups after seeing early results. Treat API errors, refusals, "
        "and length-limited answers as unavailable decisions. Development results require fresh hidden examples "
        "for independent validation. The original hourly experiment stays off.\n")
    manifest = "".join(f"{digest(path)}  {path.name}\n" for path in sorted(output.iterdir()) if path.is_file())
    (output / "SHA256SUMS.txt").write_text(manifest)
    return review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--group", type=int, default=1)
    args = parser.parse_args()
    try:
        review = prepare(args.output.expanduser(), args.group)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"Preparation stopped: {exc}\n")
    high = review["cost_scenarios"][-1]["conservative_batch_usd"]
    print(f"Prepared {review['requests']} requests. Conservative full-output batch estimate: ${high:.2f}.")
    print("Not submitted. No API calls or charges.")


if __name__ == "__main__":
    main()
