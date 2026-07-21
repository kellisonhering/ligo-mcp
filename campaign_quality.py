"""Numerical-validity checks shared by campaign generation, scheduling, and audit."""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np


REQUIRED_ARRAYS = ("H1", "L1", "inj_H1", "inj_L1")
EXPECTED_SAMPLE_RATE = 4096.0
EXPECTED_SAMPLES = 32 * int(EXPECTED_SAMPLE_RATE)
REPAIR_DIR = Path(__file__).resolve().parent / "campaign_repair"
EXCLUDED_SPECS_FILE = REPAIR_DIR / "invalid_specs.json"


def load_excluded_spec_ids(path: str | os.PathLike = EXCLUDED_SPECS_FILE) -> set[str]:
    """Load the public, outcome-independent list of unusable original specs."""
    with open(path) as f:
        payload = json.load(f)
    return set(payload["excluded_spec_ids"])


def validate_spec_arrays(arrays: dict) -> tuple[bool, str | None]:
    """Validate one pool spec without consulting campaign truth or outcomes."""
    for key in REQUIRED_ARRAYS:
        if key not in arrays:
            return False, f"missing array {key}"
        value = np.asarray(arrays[key])
        if value.ndim != 1:
            return False, f"{key} is not one-dimensional"
        if len(value) != EXPECTED_SAMPLES:
            return False, f"{key} has {len(value)} samples, expected {EXPECTED_SAMPLES}"
        if not np.isfinite(value).all():
            return False, f"{key} contains non-finite values"

    lengths = {len(np.asarray(arrays[key])) for key in REQUIRED_ARRAYS}
    if len(lengths) != 1:
        return False, "array lengths do not match"

    for key in ("gps", "sample_rate"):
        if key not in arrays:
            return False, f"missing scalar {key}"
        try:
            value = float(np.asarray(arrays[key]))
        except (TypeError, ValueError):
            return False, f"{key} is not numeric"
        if not np.isfinite(value):
            return False, f"{key} is non-finite"

    if float(np.asarray(arrays["sample_rate"])) != EXPECTED_SAMPLE_RATE:
        return False, "unexpected sample rate"
    return True, None


def validate_spec_file(path: str | os.PathLike) -> tuple[bool, str | None]:
    """Load and mechanically validate a compressed campaign spec."""
    try:
        with np.load(path) as z:
            arrays = {key: z[key] for key in z.files}
        return validate_spec_arrays(arrays)
    except Exception as exc:
        return False, f"could not load spec: {exc}"


def require_finite(name: str, value) -> None:
    """Raise before a generated non-finite value can enter a campaign artifact."""
    if not np.isfinite(np.asarray(value)).all():
        raise ValueError(f"{name} contains non-finite values")
