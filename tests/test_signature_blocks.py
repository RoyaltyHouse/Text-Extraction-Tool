"""Smoke tests for the signature-clustering + verdict-derivation pipeline.

Runs as a plain script — no pytest required.
    python tests/test_signature_blocks.py        (from repo root)
    python test_signature_blocks.py              (from inside tests/)

Each scenario exercises cluster_signature_blocks + _reconcile_signatures +
_derive_execution_status end-to-end with synthetic Textract output, including
the real failure modes captured from CloudWatch logs during testing.
"""

import os
import sys

# Allow imports from the repo root regardless of where the script is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY2", "sk-fake-for-import-only")

from signature_blocks import cluster_signature_blocks, _classify_key
from gpt_extractor import _reconcile_signatures, _derive_execution_status


# ── Unit checks on classifier ─────────────────────────────────────────────

def test_classifier_canonical_keys():
    for k in ("By:", "by", "Name:", "Print Name", "Printed Name:", "Signature:",
              "Signed:", "Authorized Signatory"):
        assert _classify_key(k) == "signed_signal", f"{k!r} should be signed_signal"
    for k in ("Title:", "Date:", "Its:"):
        assert _classify_key(k) == "meta", f"{k!r} should be meta"


def test_classifier_filters_docusign_metadata():
    # The DocuSign / Adobe Sign audit-trail fields that broke the earlier
    # word-subset matcher. All must be filtered out.
    for k in ("Document created by", "Document emailed to", "Email viewed by",
              "Signer", "Signature Date", "Adobe Sign", "Signed and dated by"):
        assert _classify_key(k) is None, f"metadata key {k!r} should NOT match"


def test_classifier_filters_unrelated():
    for k in ("Address:", "Phone:", "SSN:", "", None):
        assert _classify_key(k) is None


# ── End-to-end scenarios ──────────────────────────────────────────────────

def _run(name, line_index, annotations, gpt_sigs,
         expected_status, expected_count, expected_signed_count):
    blocks = cluster_signature_blocks(annotations, line_index)
    sigs = _reconcile_signatures(blocks, gpt_sigs)
    status = _derive_execution_status(sigs)
    actual_signed = sum(1 for s in sigs if s["signed"])
    assert status == expected_status, (
        f"{name}: ES={status} (expected {expected_status})\n"
        f"  sigs={sigs}\n  blocks={blocks}")
    assert len(sigs) == expected_count, (
        f"{name}: count={len(sigs)} (expected {expected_count})\n"
        f"  sigs={sigs}")
    assert actual_signed == expected_signed_count, (
        f"{name}: signed={actual_signed} (expected {expected_signed_count})\n"
        f"  sigs={sigs}")


def test_px_bug_one_signed_one_blank():
    """The original bug: PX docs being misclassified as FX because GPT was
    silently dropping blank signature blocks. Cluster sees both."""
    li = {101: {"page": 8, "text": "P", "words": []},
          102: {"page": 8, "text": "L", "words": []}}
    ann = [
        {"type": "form_field", "page": 8, "key": "By:",   "value": "John Smith",       "near_line": 101, "left": 0.15, "top": 0.62},
        {"type": "form_field", "page": 8, "key": "Name:", "value": "John Smith, CEO",  "near_line": 102, "left": 0.15, "top": 0.64},
        {"type": "signature",  "page": 8, "confidence": 98.5, "near_line": 101, "left": 0.15, "top": 0.63},
        {"type": "form_field", "page": 8, "key": "By:",   "value": "",                 "near_line": 101, "left": 0.55, "top": 0.62},
        {"type": "form_field", "page": 8, "key": "Name:", "value": "",                 "near_line": 102, "left": 0.55, "top": 0.64},
    ]
    _run("px_bug", li, ann, [{"value": "P"}],
         expected_status="PX", expected_count=2, expected_signed_count=1)


def test_fx_both_signed():
    li = {101: {"page": 8, "text": "P", "words": []}}
    ann = [
        {"type": "form_field", "page": 8, "key": "By:", "value": "John Smith", "near_line": 101, "left": 0.15, "top": 0.62},
        {"type": "signature",  "page": 8, "confidence": 98.5,                   "near_line": 101, "left": 0.15, "top": 0.63},
        {"type": "form_field", "page": 8, "key": "By:", "value": "Sarah Lee",  "near_line": 101, "left": 0.55, "top": 0.62},
        {"type": "signature",  "page": 8, "confidence": 97.2,                   "near_line": 101, "left": 0.55, "top": 0.63},
    ]
    _run("fx_both_signed", li, ann, [],
         expected_status="FX", expected_count=2, expected_signed_count=2)


def test_nx_no_evidence():
    li = {1: {"page": 1, "text": "DRAFT", "words": []}}
    _run("nx_empty", li, [], [], expected_status="NX", expected_count=0, expected_signed_count=0)


def test_fx_gpt_lies_about_signed_flag():
    """Cluster verdict is authoritative — GPT's `signed` field is discarded."""
    li = {101: {"page": 8, "text": "P", "words": []}}
    ann = [
        {"type": "form_field", "page": 8, "key": "By:", "value": "John Smith", "near_line": 101, "left": 0.15, "top": 0.62},
        {"type": "signature",  "page": 8, "confidence": 98.5,                   "near_line": 101, "left": 0.15, "top": 0.63},
        {"type": "form_field", "page": 8, "key": "By:", "value": "Sarah Lee",  "near_line": 101, "left": 0.55, "top": 0.62},
        {"type": "signature",  "page": 8, "confidence": 97.2,                   "near_line": 101, "left": 0.55, "top": 0.63},
    ]
    _run("fx_lying_gpt", li, ann,
         [{"value": "X", "signed": False}, {"value": "Y", "signed": False}],
         expected_status="FX", expected_count=2, expected_signed_count=2)


def test_px_gpt_undercount_restored():
    """GPT emits fewer signatures than there are blocks — cluster pads back."""
    li = {101: {"page": 8, "text": "P", "words": []},
          102: {"page": 8, "text": "L", "words": []}}
    ann = [
        {"type": "form_field", "page": 8, "key": "By:", "value": "John Smith", "near_line": 101, "left": 0.15, "top": 0.62},
        {"type": "signature",  "page": 8, "confidence": 98.5,                   "near_line": 101, "left": 0.15, "top": 0.63},
        {"type": "form_field", "page": 8, "key": "By:", "value": "",           "near_line": 101, "left": 0.55, "top": 0.62},
    ]
    _run("px_gpt_undercount", li, ann, [{"value": "Only one"}],
         expected_status="PX", expected_count=2, expected_signed_count=1)


def test_fx_lod_block_excluded():
    """SoundExchange / LOD blocks must be excluded from the FX/PX/NX count."""
    li = {101: {"page": 8, "text": "P", "words": []},
          120: {"page": 9, "text": "SOUNDEXCHANGE LETTER OF DIRECTION (LOD)", "words": []}}
    ann = [
        {"type": "form_field", "page": 8, "key": "By:", "value": "John Smith",         "near_line": 101, "left": 0.15, "top": 0.62},
        {"type": "signature",  "page": 8, "confidence": 98.5,                           "near_line": 101, "left": 0.15, "top": 0.63},
        {"type": "form_field", "page": 9, "key": "By:", "value": "SoundExchange Rep",  "near_line": 120, "left": 0.15, "top": 0.50},
        {"type": "signature",  "page": 9, "confidence": 94.0,                           "near_line": 120, "left": 0.15, "top": 0.51},
    ]
    _run("fx_lod_excluded", li, ann, [{"value": "P"}],
         expected_status="FX", expected_count=1, expected_signed_count=1)


def test_nx_gpt_hallucinates_signature():
    li = {1: {"page": 1, "text": "DRAFT", "words": []}}
    _run("nx_gpt_hallucinates", li, [], [{"value": "Phantom", "signed": True}],
         expected_status="NX", expected_count=0, expected_signed_count=0)


def test_px_stacked_same_column():
    """Two parties stacked vertically in one column — split by Y gap, not X."""
    li = {101: {"page": 8, "text": "A", "words": []},
          110: {"page": 8, "text": "B", "words": []}}
    ann = [
        {"type": "form_field", "page": 8, "key": "By:", "value": "Alice", "near_line": 101, "left": 0.15, "top": 0.40},
        {"type": "signature",  "page": 8, "confidence": 98.0,             "near_line": 101, "left": 0.15, "top": 0.41},
        {"type": "form_field", "page": 8, "key": "By:", "value": "",      "near_line": 110, "left": 0.15, "top": 0.60},
    ]
    _run("px_stacked", li, ann, [],
         expected_status="PX", expected_count=2, expected_signed_count=1)


def test_fx_minimal_block_single_by():
    """A single `By:` form field + Textract SIGNATURE → one signed block."""
    li = {30: {"page": 4, "text": "Sincerely", "words": []}}
    ann = [
        {"type": "form_field", "page": 4, "key": "By:", "value": "Jake Troth", "near_line": 30, "left": 0.15, "top": 0.70},
        {"type": "signature",  "page": 4, "confidence": 95.0,                   "near_line": 30, "left": 0.15, "top": 0.71},
    ]
    _run("fx_minimal", li, ann, [],
         expected_status="FX", expected_count=1, expected_signed_count=1)


def test_nx8_typed_name_without_signature():
    """Real NX 8 from CloudWatch: typed name spatially paired as `By:` value
    but no SIGNATURE annotation — must NOT count as signed."""
    li = {319: {"page": 8, "text": "AGREED AND ACCEPTED.", "words": []},
          322: {"page": 8, "text": "Aaron Lockhart p/k/a", "words": []}}
    ann = [
        {"type": "form_field", "page": 8, "key": "By:", "value": "",                                "near_line": 319, "left": 0.411, "top": 0.40},
        {"type": "form_field", "page": 8, "key": "By:", "value": 'Aaron Lockhart p/k/a "Dubba-AA"', "near_line": 322, "left": 0.117, "top": 0.50},
    ]
    _run("nx8_typed_name", li, ann, [],
         expected_status="NX", expected_count=2, expected_signed_count=0)


def test_nx5_printed_name_alone():
    """Standalone `Printed Name:` filled but no SIGNATURE → unsigned."""
    li = {200: {"page": 6, "text": "Print Name", "words": []}}
    ann = [
        {"type": "form_field", "page": 6, "key": "Printed Name", "value": "Karrah Schuster", "near_line": 200, "left": 0.10, "top": 0.30},
    ]
    _run("nx5_printed_name", li, ann, [],
         expected_status="NX", expected_count=1, expected_signed_count=0)


def test_px7_email_style_signature_without_sig():
    """`By:` value like "Name (email@x.com)" with no SIGNATURE → unsigned."""
    li = {300: {"page": 7,  "text": "P1", "words": []},
          350: {"page": 16, "text": "P2", "words": []}}
    ann = [
        {"type": "form_field", "page": 7,  "key": "By:", "value": "Miguel Angel",                 "near_line": 300, "left": 0.15, "top": 0.40},
        {"type": "signature",  "page": 7,  "confidence": 97.0,                                     "near_line": 300, "left": 0.15, "top": 0.41},
        {"type": "form_field", "page": 16, "key": "By:", "value": "Ateara (a@b.com)",             "near_line": 350, "left": 0.10, "top": 0.55},
    ]
    _run("px7_email_sig", li, ann, [],
         expected_status="PX", expected_count=2, expected_signed_count=1)


def test_fx_audit_report_block_excluded():
    """DocuSign 'Final Audit Report' page with `By: <creator>` metadata
    must be excluded so it doesn't tip FX → PX (FX 5/8/10 from testing)."""
    li = {
        51:  {"page": 6,  "text": "AGREED AND ACCEPTED:",                                    "words": []},
        52:  {"page": 6,  "text": "Cheeze Beatz, LLC",                                       "words": []},
        100: {"page": 13, "text": "Offset - Cheeze Beatz Producer Agreement EXE 01-01-25",  "words": []},
        101: {"page": 13, "text": "Final Audit Report",                                      "words": []},
        103: {"page": 13, "text": "By: Bernie Lawrence-Watkins (bernie@blwapc.com)",         "words": []},
    }
    ann = [
        {"type": "form_field", "page": 6,  "key": "By:", "value": "Darryl McCorkell",                          "near_line": 52,  "left": 0.10, "top": 0.65},
        {"type": "signature",  "page": 6,  "confidence": 97.0,                                                  "near_line": 52,  "left": 0.10, "top": 0.66},
        {"type": "form_field", "page": 13, "key": "By:", "value": "Bernie Lawrence-Watkins (bernie@blwapc.com)", "near_line": 103, "left": 0.15, "top": 0.30},
    ]
    _run("fx_audit_excluded", li, ann, [{"value": "Producer"}],
         expected_status="FX", expected_count=1, expected_signed_count=1)


def test_nx6_lod_header_far_from_signature():
    """Real NX 6 from CloudWatch: 'Form Producer Letter of Direction' header
    at top of page, standalone signature near bottom — whole-page scan
    (not ±N-line window) catches it."""
    li = {
        245: {"page": 5,  "text": "Accepted and Agreed:",                  "words": []},
        252: {"page": 6,  "text": "By: ____ Aaron Lockhart",               "words": []},
        250: {"page": 8,  "text": "Form Producer Letter of Direction",     "words": []},
        253: {"page": 8,  "text": "Atlantic Recording Corporation",        "words": []},
        260: {"page": 8,  "text": "Ladies and Gentlemen:",                 "words": []},
        275: {"page": 8,  "text": "I have engaged This Is The Sound...",   "words": []},
        282: {"page": 8,  "text": "Kentrell Gaulden",                      "words": []},
        380: {"page": 11, "text": "Signature: _______ Kentrell Gaulden",   "words": []},
        381: {"page": 11, "text": "SoundExchange LOD",                     "words": []},
    }
    ann = [
        {"type": "form_field", "page": 5,  "key": "Authorized Signatory", "value": "",                "near_line": 245, "left": 0.034, "top": 0.50},
        {"type": "form_field", "page": 6,  "key": "By:",                  "value": "",                "near_line": 252, "left": 0.034, "top": 0.50},
        {"type": "signature",  "page": 8,  "confidence": 92.0,                                          "near_line": 282, "left": 0.392, "top": 0.70},
        {"type": "form_field", "page": 11, "key": "Signature:",           "value": "",                "near_line": 380, "left": 0.04,  "top": 0.30},
        {"type": "form_field", "page": 11, "key": "Printed Name:",       "value": "Kentrell Gaulden", "near_line": 381, "left": 0.04,  "top": 0.35},
        {"type": "signature",  "page": 11, "confidence": 94.0,                                          "near_line": 381, "left": 0.04,  "top": 0.36},
    ]
    _run("nx6_page_spanning_lod", li, ann, [],
         expected_status="NX", expected_count=2, expected_signed_count=0)


# ── Defensive: malformed GPT output must not crash ───────────────────────

def test_reconciles_none_signatures():
    li = {101: {"page": 8, "text": "P", "words": []}, 102: {"page": 8, "text": "L", "words": []}}
    ann = [
        {"type": "form_field", "page": 8, "key": "By:", "value": "X", "near_line": 101, "left": 0.15, "top": 0.62},
        {"type": "signature",  "page": 8, "confidence": 98.0,          "near_line": 101, "left": 0.15, "top": 0.63},
        {"type": "form_field", "page": 8, "key": "By:", "value": "",   "near_line": 101, "left": 0.55, "top": 0.62},
    ]
    _run("gpt_returns_none", li, ann, None,
         expected_status="PX", expected_count=2, expected_signed_count=1)
    _run("gpt_returns_garbage", li, ann, "not a list",
         expected_status="PX", expected_count=2, expected_signed_count=1)
    _run("gpt_returns_wrong_shape", li, ann, ["str", 42, {"value": None}],
         expected_status="PX", expected_count=2, expected_signed_count=1)


# ── Runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in tests:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"[FAIL] {fn.__name__}\n  {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
    raise SystemExit(0 if passed == len(tests) else 1)
