"""Minimal runtime-evidence metadata: which revision did the machine actually test?

An Implementer records runtime evidence in a fenced block in the PR body (or a comment):

```vccp-runtime-evidence
{"expected_revision": "<40-hex sha>", "tested_revision": "<40-hex sha>",
 "target": "linux/amd64 host A", "image_digest": "sha256:..."}
```

``image_digest`` is optional. The block is metadata only: no gate, no artifact store, and
no deployment framework are involved. It exists so the Reviewer is told plainly when the
revision that was actually exercised is not the revision under review. Evidence whose
``tested_revision`` differs from ``expected_revision`` — or whose ``expected_revision`` is
not the exact PR head — cannot satisfy a criterion that requires the current revision.

The verdict is called ``revision_match`` on purpose. Every field in the block is the
Implementer's own declaration, so a match proves only that the declared revisions agree
with the exact head. It never proves that a test ran, that it passed, or that the declared
target was the one exercised; the Reviewer still has to judge the real evidence.
"""
from __future__ import annotations

import json
import re

FENCE = "vccp-runtime-evidence"
_BLOCK = re.compile(r"```" + FENCE + r"\s*\n(.*?)```", re.DOTALL)


def parse_runtime_evidence(text: str) -> list[dict]:
    """Return one normalized item per declared evidence object; never raises."""
    items: list[dict] = []
    for match in _BLOCK.finditer(text or ""):
        try:
            value = json.loads(match.group(1))
        except (TypeError, ValueError):
            items.append({"valid": False, "reason": "malformed_json"})
            continue
        for raw in value if isinstance(value, list) else [value]:
            items.append(_normalize(raw))
    return items


def _normalize(raw) -> dict:
    if not isinstance(raw, dict):
        return {"valid": False, "reason": "not_an_object"}
    expected = str(raw.get("expected_revision") or "").strip()
    tested = str(raw.get("tested_revision") or "").strip()
    target = str(raw.get("target") or "").strip()
    digest = raw.get("image_digest")
    missing = [name for name, value in (("expected_revision", expected),
                                        ("tested_revision", tested),
                                        ("target", target)) if not value]
    if missing:
        return {"valid": False, "reason": "missing_" + "_".join(missing)}
    return {
        "valid": True,
        "expected_revision": expected,
        "tested_revision": tested,
        "target": target,
        "image_digest": str(digest) if digest else None,
        "tested_matches_expected": expected.casefold() == tested.casefold(),
    }


def evaluate_runtime_evidence(items: list[dict], review_head: str) -> list[dict]:
    """Judge each declared item against the exact head under review.

    ``revision_match`` is the only field a Reviewer may read as "the declared revisions
    line up with the exact head". It is a statement about declarations, not about whether
    the run happened or passed.
    """
    verdicts = []
    head = str(review_head or "").casefold()
    for item in items or []:
        if not item.get("valid"):
            verdicts.append({**item, "revision_match": False,
                             "reason": item.get("reason") or "invalid_evidence"})
            continue
        expected_matches_head = str(item["expected_revision"]).casefold() == head
        if not item["tested_matches_expected"]:
            reason = "tested_revision_differs_from_expected"
        elif not expected_matches_head:
            reason = "expected_revision_is_not_the_exact_head"
        else:
            reason = "ok"
        verdicts.append({**item, "expected_matches_head": expected_matches_head,
                         "revision_match": reason == "ok", "reason": reason})
    return verdicts


def render_evidence_section(verdicts: list[dict], review_head: str) -> str:
    """Render the reviewer-facing verdicts, empty when the PR declares no evidence."""
    if not verdicts:
        return ""
    lines = [f"Runtime evidence is metadata and never overrides the Frozen Spec. "
             f"The exact head under review is {review_head}."]
    for index, item in enumerate(verdicts, start=1):
        if not item.get("valid"):
            lines.append(f"{index}. UNUSABLE ({item.get('reason')}): the item is not a "
                         "well-formed evidence object with expected_revision, "
                         "tested_revision, and target.")
            continue
        lines.append(
            f"{index}. revision_match={'yes' if item['revision_match'] else 'no'}; "
            f"expected_revision={item['expected_revision']}; "
            f"tested_revision={item['tested_revision']}; target={item['target']}"
            + (f"; image_digest={item['image_digest']}" if item.get("image_digest") else "")
            + f"; reason={item['reason']}")
    lines.append(
        "Every field above is the Implementer's own declaration. revision_match=yes means "
        "only that the declared revisions agree with the exact head; it does not prove that "
        "a test ran, that it passed, or that the declared target was the one exercised. "
        "Judge the actual runtime, browser, or test evidence yourself. If the Frozen Spec "
        "requires verification of the current revision, evidence with revision_match=no "
        "cannot satisfy that criterion; say so explicitly and do not approve on the strength "
        "of a test that exercised a different revision.")
    return "<runtime-evidence>\n" + "\n".join(lines) + "\n</runtime-evidence>\n\n"
