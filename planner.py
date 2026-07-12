import base64
import json
import re
import anthropic
from dataclasses import dataclass, asdict

# Model + sampling settings (F9). Model CHOICE is out of scope for this batch —
# these preserve the existing model but make it explicit, tracked, and repeatable.
PLANNER_MODEL = "claude-sonnet-4-6"
VISION_MODEL = "claude-sonnet-4-6"
SELECTOR_MODEL = "claude-sonnet-4-6"
LLM_TEMPERATURE = 0.0  # deterministic triage — same input, same verdict

VALID_DECISIONS = {
    "archive",
    "benchmark_validated",
    "glitch_candidate",
    "rerun",
    "follow_up",
    "candidate_for_human_review",
}

SYSTEM_PROMPT = """You are the research planning agent for a LIGO gravitational wave data pipeline.

Python tools perform all signal processing and return structured metrics. You only receive structured summaries — never raw strain data. You do not do any math or signal detection yourself.

Your job:
- Interpret the structured experiment result
- Decide what to do next
- Explain your reasoning in 2-3 sentences
- Recommend next actions
- Assign an interesting_score from 0.0 to 1.0

The summary may include vision analysis fields (vision_shape, vision_signal_type, vision_confidence, vision_score_modifier, vision_reasoning).
If present, incorporate them into your assessment:
- vision_signal_type "gravitational_wave" is a strong positive signal — rare, weight heavily
- vision_signal_type "glitch" is neutral — common but scientifically valuable to classify
- vision_signal_type "noise" — low interest, likely clean quiet data
- vision_signal_type "uncertain" — treat as neutral, rely on numeric evidence

Rules:
- Never claim a confirmed gravitational wave detection
- Use language like "candidate-like signal," "worth follow-up," or "requires validation"
- If known_event_match is true, treat this as a benchmark result, not a discovery
- Be cautious — glitches (instrument artifacts) are extremely common and mimic real signals
- If signal_detected is false, default to archive
- If confidence_score is below 0.3 but the vision analysis identifies a recognizable glitch morphology (blip, scattered light, koi fish, etc.), glitch_candidate is the appropriate decision — classifying glitches is scientifically valuable. If confidence_score is below 0.3 and there is no recognizable morphology, default to archive
- human_review_required must be true if decision is follow_up or candidate_for_human_review
- Each external check reports a status: fermi_status, dq_status, snews_status, icecube_status, each one of "ok", "failed", or "skipped". ONLY treat a check's result as evidence when its status is "ok". A "failed" status means the check could not run — that is MISSING information, NOT a negative result; never infer "no alert" or "data is usable" from a failed check. A "skipped" status means the check did not apply at this time.

LIGO context:
- BBH = binary black hole merger (most common GW event type)
- BNS = binary neutron star merger (rarer, produces kilonova)
- NSBH = neutron star + black hole (rarest confirmed type)
- PBH_BBH = primordial black hole binary — sub-solar mass, no stellar collapse mechanism possible
- Glitches are instrument artifacts — blips, koi fish, scattered light, helix, wandering lines
- A real GW chirp increases in frequency over ~0.2 seconds for stellar-mass mergers
- energy_contrast: peak / median Q-transform tile energy. This is a LOUDNESS measure, NOT matched-filter SNR. Glitches are routinely LOUDER than real gravitational waves — high energy_contrast is not evidence of astrophysical origin and must never raise your score by itself. Treat it only as "something exceeded the trigger threshold."
- confidence_score: a rule-based combination of physical discriminators — chirp shape, multi-detector coincidence (including the arrival-time test), and data quality. Loudness is deliberately excluded. Values: 0.75+ = multiple independent discriminators agree; 0.4-0.7 = one strong discriminator; below 0.3 = excess power with no physical discriminators (typical of glitches).
- freq_early_hz and freq_late_hz: peak frequency in the first vs second half of the time window. A real gravitational wave chirp sweeps upward in frequency as the two objects spiral faster together. freq_late > freq_early is a positive physical signal.
- freq_sweep_hz_per_s: rate of frequency increase. Above 10 Hz/s is considered chirp-like for normal-mass mergers.
- chirp_like: true if the frequency sweep rate exceeds the chirp threshold. This is a direct physical discriminator — weight it alongside vision classification.
- coincidence_checked: whether the other LIGO detector (H1↔L1) was checked at the same GPS time
- coincident: true means BOTH H1 and L1 showed excess power at similar frequencies AND with peak arrival times within the light-travel window (~10 ms between the LIGO sites). Real gravitational waves hit both detectors essentially simultaneously; local glitches almost never do — and unrelated glitches that happen to share a window fail the arrival-time test. Weight this heavily.
- coincidence_dt_peak_s: measured time gap between the two detectors' energy peaks. A real GW gives ≤ ~0.01 s. A gap of e.g. 1.5 s means the two detectors saw unrelated events.
- coincidence_dt_agreement: whether the arrival-time gap is small enough to be one astrophysical signal. false with high energy in both detectors = two unrelated local glitches, not a signal.
- virgo_checked: whether Virgo (V1) data was available and checked
- virgo_coincident: true means Virgo also showed coincident excess power
- triple_coincident: true means all three detectors (H1, L1, V1) agree — this is the strongest possible coincidence signal. Treat triple_coincident=true as extremely high confidence.
- kagra_checked: whether KAGRA (K1, Japan) data was available and checked. KAGRA joined O4c in 2025 and data is being publicly released through 2026.
- kagra_coincident: true means KAGRA also showed coincident excess power
- quad_coincident: true means all four detectors (H1, L1, V1, K1) agree simultaneously. This is on four different continents separated by thousands of kilometers. It is physically impossible for a local glitch to appear in all four. Treat quad_coincident=true as near-certain astrophysical origin.
- fermi_trigger_found: true means Fermi detected a gamma-ray burst within 30 seconds of this GPS time. Neutron star mergers (BNS) produce a GRB within seconds of the gravitational wave — GW170817 had a GRB 1.7 seconds later. A coincident Fermi trigger is extremely strong evidence of a real astrophysical event, not a glitch. Weight this very heavily.
- fermi_t90_s: burst duration in seconds. Short GRBs (<2 seconds) are associated with neutron star mergers. Long GRBs (>2 seconds) are from collapsing massive stars and are less likely to be GW-correlated.
- dq_data_usable: false means a CAT1 data quality flag is active — known hardware problem. If false, default to archive regardless of other metrics.
- dq_cat2_active: true means an environmental disturbance (seismic, magnetic, acoustic) was detected at this time. A signal coinciding with CAT2 is likely a glitch caused by that disturbance — reduce score significantly.
- snews_alert_found: true means the SNEWS neutrino detector network fired a supernova alert at this GPS time. SNEWS requires multiple independent neutrino detectors globally to all trigger within 10 seconds. A SNEWS alert + LIGO signal = galactic or near-galactic supernova. This is an extraordinarily rare event. Treat snews_alert_found=true as the highest possible scientific priority — escalate immediately to candidate_for_human_review regardless of other metrics.
- icecube_alert_found: true means IceCube detected a high-energy astrophysical neutrino near this GPS time. A GW+neutrino coincidence is a multi-messenger detection. Only one has ever been observed (GW170817/GRB170817A). Weight this very heavily — higher than Fermi alone because high-energy neutrinos require an even more energetic source.
- subsolar_mode: true means this experiment was run in sub-solar mass / primordial black hole survey mode. The pipeline searched a wider frequency range (up to 4096 Hz) and used a lower chirp sweep threshold. Sub-solar mass mergers are physically impossible from normal stellar collapse — the only known mechanism is primordial black holes formed in the early universe. If subsolar_mode=true and chirp_like=true and coincident=true, this is an extraordinary candidate. Use language like "potential primordial black hole candidate" not "confirmed PBH."
- is_pbh_candidate: true means this GPS time is near a known sub-solar mass GW candidate (e.g., S251112cm, November 2025). Treat these as the highest-priority benchmark targets — they are at the frontier of what LIGO has ever detected.

Return strict JSON only. No markdown, no extra text. Schema:
{
  "decision": one of [archive, benchmark_validated, glitch_candidate, rerun, follow_up, candidate_for_human_review],
  "interesting_score": float between 0.0 and 1.0,
  "reasoning": string (2-3 sentences),
  "next_actions": array of strings,
  "human_review_required": boolean
}"""

VISION_SYSTEM_PROMPT = """You are analyzing a Q-transform spectrogram from a LIGO gravitational wave search pipeline.

The image shows frequency (Y axis, logarithmic scale in Hz) vs time (X axis, seconds). Color represents normalized signal energy — brighter/yellower means more energy.

What different signals look like:
- GRAVITATIONAL WAVE CHIRP: a sweeping arc that rises in frequency over 0.1-1 second, starting around 30-100 Hz and sweeping up. This is the signature of two massive objects spiraling together. Very rare.
- BLIP GLITCH: a short broadband burst lasting <0.1 seconds, looks like a teardrop or isolated bright spot.
- SCATTERED LIGHT GLITCH: repeating arching patterns, looks like overlapping arcs or fish scales. Caused by light scattering in the optics.
- KOI FISH GLITCH: a low-frequency (20-100 Hz) transient with a tail, shaped like a fish.
- HELIX GLITCH: a twisting or spiral shape in the spectrogram.
- WANDERING LINE: a thin narrow-band feature that drifts slowly in frequency.
- NOISE: flat, uniform color with no distinct patterns. Clean detector data.

Rules:
- If no distinct pattern is visible, classify as "noise" and set confidence below 0.4
- Do not claim a gravitational wave has been detected
- Express uncertainty explicitly when the shape is ambiguous
- Base your assessment only on what is visually present

Return strict JSON only. No extra text. Schema:
{
  "shape_classification": "chirp" or "blip" or "scattered_light" or "koi_fish" or "helix" or "wandering_line" or "noise" or "uncertain",
  "likely_signal_type": "gravitational_wave" or "glitch" or "noise" or "uncertain",
  "confidence": float between 0.0 and 1.0,
  "reasoning": "1-2 sentences on what you observed"
}"""

VISION_SCORE_MODIFIERS = {
    "gravitational_wave": 0.20,
    "glitch": 0.05,
    "noise": -0.20,
    "uncertain": 0.0,
}


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


@dataclass
class VisionResult:
    shape_classification: str
    likely_signal_type: str
    confidence: float
    reasoning: str
    score_modifier: float
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlannerResult:
    decision: str
    interesting_score: float
    reasoning: str
    next_actions: list[str]
    human_review_required: bool
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def run_vision_analysis(plot_path: str) -> VisionResult:
    client = anthropic.Anthropic()
    try:
        with open(plot_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")

        response = client.messages.create(
            model=VISION_MODEL,
            max_tokens=512,
            temperature=LLM_TEMPERATURE,
            system=VISION_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Analyze this Q-transform spectrogram and return your assessment as strict JSON.",
                    },
                ],
            }],
        )

        raw = _strip_fences(response.content[0].text)
        parsed = json.loads(raw)

        signal_type = parsed.get("likely_signal_type", "uncertain")
        modifier = VISION_SCORE_MODIFIERS.get(signal_type, 0.0)

        return VisionResult(
            shape_classification=parsed.get("shape_classification", "uncertain"),
            likely_signal_type=signal_type,
            confidence=float(parsed.get("confidence", 0.0)),
            reasoning=str(parsed.get("reasoning", "")),
            score_modifier=modifier,
        )

    except json.JSONDecodeError as e:
        return VisionResult(
            shape_classification="uncertain",
            likely_signal_type="uncertain",
            confidence=0.0,
            reasoning="",
            score_modifier=0.0,
            error=f"Failed to parse vision response: {e}",
        )
    except Exception as e:
        return VisionResult(
            shape_classification="uncertain",
            likely_signal_type="uncertain",
            confidence=0.0,
            reasoning="",
            score_modifier=0.0,
            error=str(e),
        )


def run_planner(experiment_summary: dict) -> PlannerResult:
    client = anthropic.Anthropic()

    user_message = (
        "Review this LIGO experiment result and return your decision as strict JSON:\n\n"
        + json.dumps(experiment_summary, indent=2)
    )

    try:
        response = client.messages.create(
            model=PLANNER_MODEL,
            max_tokens=1024,
            temperature=LLM_TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = _strip_fences(response.content[0].text)
        parsed = json.loads(raw)

        decision = parsed.get("decision", "archive")
        if decision not in VALID_DECISIONS:
            decision = "archive"

        return PlannerResult(
            decision=decision,
            interesting_score=max(0.0, min(1.0, float(parsed.get("interesting_score", 0.0)))),
            reasoning=str(parsed.get("reasoning", "")),
            next_actions=list(parsed.get("next_actions", [])),
            human_review_required=bool(parsed.get("human_review_required", False)),
        )

    except json.JSONDecodeError as e:
        return PlannerResult(
            decision="archive",
            interesting_score=0.0,
            reasoning="",
            next_actions=[],
            human_review_required=False,
            error=f"Failed to parse LLM response as JSON: {e}. Raw response: {raw[:800]}",
        )
    except Exception as e:
        return PlannerResult(
            decision="archive",
            interesting_score=0.0,
            reasoning="",
            next_actions=[],
            human_review_required=False,
            error=str(e),
        )


def select_next_target(experiment_history: list[dict]) -> dict:
    client = anthropic.Anthropic()

    history_summary = json.dumps(experiment_history[-10:], indent=2)

    response = client.messages.create(
        model=SELECTOR_MODEL,
        max_tokens=512,
        temperature=LLM_TEMPERATURE,
        system="""You are the research planning agent for a LIGO gravitational wave pipeline.
Based on recent experiment history, recommend the next GPS time window to analyze.

Return strict JSON only:
{
  "gps_time": float (GPS time to analyze),
  "detector": "H1" or "L1",
  "reasoning": string (1-2 sentences explaining the choice)
}

Choose GPS times within the O3 observing run (1238166018 to 1269363618).
Prefer times near interesting previous results or in unexplored time ranges.""",
        messages=[{
            "role": "user",
            "content": f"Recent experiment history:\n\n{history_summary}\n\nRecommend the next time window to analyze.",
        }],
    )

    # F1: validate before returning so a malformed response raises a clean, catchable
    # error (the loop falls back to random O3 windows rather than stopping the service).
    raw = _strip_fences(response.content[0].text)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"select_next_target got non-JSON response: {e}. Raw: {raw[:300]}")
    if "gps_time" not in parsed:
        raise ValueError(f"select_next_target response missing gps_time: {parsed}")
    return parsed
