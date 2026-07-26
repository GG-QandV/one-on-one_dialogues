"""Tests for drift_20.json fixture validation (D4)."""

import json
from pathlib import Path

from app.translation.prompts import detect_drift
from app.translation.base import TranslationMode, TranslationRequest


def load_drift_cases():
    """Load drift test cases from fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "drift_20.json"
    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["cases"]


def test_drift_20_all_cases():
    """Run all 20 drift test cases from fixture."""
    cases = load_drift_cases()
    
    for case in cases:
        req = TranslationRequest(
            text=case["text"],
            source_language=case["source_language"],
            target_language=case["target_language"],
            mode=TranslationMode.LIVE_LITERAL,  # Mode doesn't matter for detect_drift
        )
        
        changes = detect_drift(req, case["translation"])
        
        # Check if drift was detected
        has_drift = len(changes) > 0 and any(c.type == "lost_entity" for c in changes)
        
        assert has_drift == case["expect_drift"], (
            f"Case {case['id']}: expected drift={case['expect_drift']}, got {has_drift}. "
            f"Changes: {[(c.type, c.original) for c in changes]}. Why: {case['why']}"
        )
        
        # If drift expected, check the originals match
        if case["expect_drift"]:
            lost_originals = {c.original for c in changes if c.type == "lost_entity" and c.original}
            expected_originals = set(case["expect_originals"])
            assert lost_originals == expected_originals, (
                f"Case {case['id']}: expected originals {expected_originals}, "
                f"got {lost_originals}. Why: {case['why']}"
            )


if __name__ == "__main__":
    test_drift_20_all_cases()
    print("All 20 drift cases passed!")