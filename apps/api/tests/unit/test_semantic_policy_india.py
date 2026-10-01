"""Built-in floor rules for Indian identifiers and the shipped policy template."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.semantic.loader import load_semantic_layer
from app.semantic.policy import BUILTIN_PATTERNS, SensitiveFieldPolicy

PRODUCT_LAYER = Path(__file__).resolve().parents[4] / "semantic-layer"


@pytest.mark.parametrize(
    "field",
    [
        "gst",
        "gstin",
        "GSTIN",
        "company_gstin",
        "gst_no",
        "gst_number",
        "pan_no",
        "PanNumber",
        "pan_card",
        "aadhaar",
        "aadhar_number",
        "pan",
    ],
)
def test_indian_government_ids_are_forbidden_even_with_an_empty_policy(field: str) -> None:
    assert SensitiveFieldPolicy(BUILTIN_PATTERNS).is_forbidden(field)


@pytest.mark.parametrize(
    "field",
    [
        "company",
        "company_name",
        "panel_id",
        "expansion",
        "suggested",
        "gst_amount",
        "gst_rate",
        "cgst",
        "igst_value",
    ],
)
def test_ordinary_names_and_tax_amounts_are_not_caught(field: str) -> None:
    assert not SensitiveFieldPolicy(BUILTIN_PATTERNS).is_forbidden(field)


@pytest.mark.parametrize("field", ["whatsapp", "linkedin_url", "pincode", "pin_code"])
def test_template_masks_messaging_social_and_pincode(field: str) -> None:
    policy = load_semantic_layer(PRODUCT_LAYER).policy
    assert isinstance(policy, SensitiveFieldPolicy)
    match = policy.classify(field)
    assert match is not None and match.action == "mask"
