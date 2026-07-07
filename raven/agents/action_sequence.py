from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from raven.llm.azure_client import AzureGPT4OClient
from raven.models import ActionSequence


ACTION_SYSTEM = """You are Agent #1 in RAVEN.
Infer a deterministic Android emulator action sequence from a GitHub issue and visual evidence.
Return only JSON with keys: actions, assumptions, target_final_state.
Each action must use one of: tap, long_press, swipe, input_text, keyevent, wait, launch_activity, shell.
Use emulator-compatible coordinates when visible. Prefer wait/keyevent/input/tap/swipe actions."""

# LLM synonyms → canonical field names
_KEY_ALIASES: dict[str, str] = {
    "action": "type",
    "time_ms": "duration_ms",
    "delay_ms": "duration_ms",
    "duration": "duration_ms",
    "key": "keycode",
    "keys": "keycode",
    "input": "text",
    "value": "text",
}


def _normalise_action(raw: Any) -> Any:
    """Rename LLM-variant keys to their canonical AndroidAction field names."""
    if not isinstance(raw, dict):
        return raw
    action: dict[str, Any] = {}
    for k, v in raw.items():
        action[_KEY_ALIASES.get(k, k)] = v
    # flatten a nested "params" dict if present
    if isinstance(action.get("params"), dict):
        for k, v in action.pop("params").items():
            canonical = _KEY_ALIASES.get(k, k)
            if canonical not in action:
                action[canonical] = v
    return action


def _normalise_sequence(data: Any) -> Any:
    """Normalise the top-level LLM JSON so ActionSequence.model_validate receives clean input."""
    if not isinstance(data, dict):
        return data
    # some models wrap everything under a top-level key
    if len(data) == 1 and "actions" not in data:
        data = next(iter(data.values()))
    if not isinstance(data, dict):
        return data
    if "actions" in data and isinstance(data["actions"], list):
        data = dict(data)
        data["actions"] = [_normalise_action(a) for a in data["actions"]]
    return data


class ActionSequenceAgent:
    def __init__(self, llm: AzureGPT4OClient):
        self.llm = llm

    def generate(self, issue_text: str, frames: list[Path], output_path: Path) -> ActionSequence:
        prompt = f"""GitHub issue:
{issue_text}

Predict the shortest action sequence that reproduces the reported Android bug.
For every coordinate action include x/y values. Include rationale per action.
"""
        data = self.llm.complete_json(ACTION_SYSTEM, prompt, images=frames)
        sequence = ActionSequence.model_validate(_normalise_sequence(data))
        output_path.write_text(sequence.model_dump_json(indent=2), encoding="utf-8")
        return sequence

    def repair(
        self,
        issue_text: str,
        previous: ActionSequence,
        verification_reason: str,
        frames: list[Path],
        output_path: Path,
    ) -> ActionSequence:
        prompt = f"""GitHub issue:
{issue_text}

Previous action sequence:
{json.dumps(previous.model_dump(mode="json"), indent=2)}

Verification failure:
{verification_reason}

Repair the remaining or ambiguous steps and return a complete replacement sequence.
"""
        data = self.llm.complete_json(ACTION_SYSTEM, prompt, images=frames)
        sequence = ActionSequence.model_validate(_normalise_sequence(data))
        output_path.write_text(sequence.model_dump_json(indent=2), encoding="utf-8")
        return sequence

