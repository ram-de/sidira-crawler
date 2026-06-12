#!/usr/bin/env python3
"""Honest-test gate — structure-only comparison of two Feature Packs.

Compares a synthesized pack against a reference pack on **normalized graph
shape and screen categories ONLY** — never route names, labels, titles, or
fixture ids. Structural convergence between an independently built product
and a hand-authored reference is the signal; content matching would indicate
circularity leaked into synthesis, so exact-id/name overlap is checked and
FAILS the gate when found.

The comparison is pure evaluation: it runs after synthesis and feeds nothing
back. Like scripts/sandbox_acceptance.py, the judge functions here are pure
and importable for hermetic tests.

Usage:
    pack_structure_compare.py --candidate <pack-dir> --reference <pack-dir>
        [--min-category-similarity 0.5] [--report report.json]

A pack dir is the snapshot directory holding manifest.json + capabilities/.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# Generic product vocabulary -> screen category. These words describe what
# nearly every consumer product calls its surfaces; they are deliberately not
# specific to any one app. Order matters: first matching category wins.
CATEGORY_VOCAB: list[tuple[str, tuple[str, ...]]] = [
    ("auth", ("login", "log in", "sign in", "signin", "sign up", "signup", "register", "password")),
    ("checkout_plan", ("plan", "billing", "subscribe", "upgrade", "pricing", "payment", "checkout")),
    ("confirmation", ("success", "confirm", "done", "complete", "thank")),
    ("search", ("search", "results", "find")),
    ("library", ("my list", "my-list", "library", "saved", "watchlist", "favorites", "history")),
    ("account", ("account", "profile", "settings", "preferences")),
    ("detail", ("detail", "watch", "play", "episode", "item", "product", "view")),
    ("info", ("help", "faq", "support", "about", "terms", "privacy", "legal", "contact")),
    ("browse", ("home", "browse", "discover", "feed", "explore", "catalog", "landing")),
]

DEFAULT_MIN_CATEGORY_SIMILARITY = 0.5
# Sample-scope references describe a slice of the product, so the gate asks
# for containment: most reference categories must reappear in the candidate.
# Not all — a hand-authored sample may include flows behind side effects
# (e.g. a purchase confirmation) that a read-only walker never triggers.
DEFAULT_MIN_CATEGORY_RECALL = 0.75
# Capability counts may differ by this factor before the gate fails — the
# clusterings need not agree, only land in the same order of magnitude.
CAPABILITY_COUNT_FACTOR = 3.0
# Any exact screen-id or screen-name overlap above this many screens is
# treated as content leakage (circularity), not convergence.
MAX_CONTENT_OVERLAP = 0


def load_pack(pack_dir: Path) -> dict:
    """Load manifest + every capability's reverse-figma.json from a pack dir."""
    manifest = json.loads((pack_dir / "manifest.json").read_text())
    capabilities = {}
    for name in manifest["capabilities"]:
        figma_path = pack_dir / "capabilities" / name / "reverse-figma.json"
        capabilities[name] = json.loads(figma_path.read_text())
    return {"manifest": manifest, "capabilities": capabilities}


def categorize_screen(screen: dict) -> str:
    """Bucket one reverse-Figma screen into a generic category.

    Looks at the screen's own words (id, name, component copy) only to map it
    into a product-generic taxonomy — the comparison then happens between
    category histograms, never between the words themselves.
    """
    haystack = " ".join(
        [
            str(screen.get("id", "")),
            str(screen.get("name", "")),
            *(str(c.get("text") or "") for c in screen.get("components", [])),
            *(str(c.get("id") or "") for c in screen.get("components", [])),
        ]
    ).lower()
    for category, words in CATEGORY_VOCAB:
        if any(word in haystack for word in words):
            return category
    return "other"


def normalize_pack(pack: dict) -> dict:
    """Reduce a pack to its structural fingerprint — counts and categories."""
    screens: list[dict] = []
    transitions = 0
    per_capability = []
    for name, figma in pack["capabilities"].items():
        cap_screens = figma.get("screens", [])
        cap_transitions = figma.get("transitions", [])
        screens.extend(cap_screens)
        transitions += len(cap_transitions)
        out_degrees: dict[str, int] = {}
        for tr in cap_transitions:
            out_degrees[tr["from_screen"]] = out_degrees.get(tr["from_screen"], 0) + 1
        role_gated = sum(1 for tr in cap_transitions if tr.get("role_gated"))
        per_capability.append(
            {
                "screens": len(cap_screens),
                "transitions": len(cap_transitions),
                "avg_out_degree": (
                    round(sum(out_degrees.values()) / len(cap_screens), 2) if cap_screens else 0.0
                ),
                "role_gated_transition_share": (
                    round(role_gated / len(cap_transitions), 2) if cap_transitions else 0.0
                ),
            }
        )
    histogram: dict[str, int] = {}
    for screen in screens:
        category = categorize_screen(screen)
        histogram[category] = histogram.get(category, 0) + 1
    return {
        "capability_count": len(pack["capabilities"]),
        "total_screens": len(screens),
        "total_transitions": transitions,
        "category_histogram": dict(sorted(histogram.items())),
        "per_capability_shape": sorted(
            per_capability, key=lambda c: (-c["screens"], -c["transitions"])
        ),
    }


def category_similarity(hist_a: dict[str, int], hist_b: dict[str, int]) -> float:
    """Cosine similarity between two category histograms."""
    keys = set(hist_a) | set(hist_b)
    if not keys:
        return 0.0
    dot = sum(hist_a.get(k, 0) * hist_b.get(k, 0) for k in keys)
    norm_a = math.sqrt(sum(v * v for v in hist_a.values()))
    norm_b = math.sqrt(sum(v * v for v in hist_b.values()))
    if not norm_a or not norm_b:
        return 0.0
    return round(dot / (norm_a * norm_b), 4)


def content_overlap(pack_a: dict, pack_b: dict) -> dict:
    """Exact screen-id/name matches between packs — the circularity check.

    Convergent structure with ZERO copied identifiers is the honest outcome:
    two independent descriptions of similar products shouldn't share literal
    ids. Overlap means fixture content leaked into synthesis.
    """

    def screen_keys(pack: dict) -> tuple[set[str], set[str]]:
        ids: set[str] = set()
        names: set[str] = set()
        for figma in pack["capabilities"].values():
            for screen in figma.get("screens", []):
                ids.add(str(screen.get("id", "")).lower())
                names.add(str(screen.get("name", "")).lower())
        return ids, names

    ids_a, names_a = screen_keys(pack_a)
    ids_b, names_b = screen_keys(pack_b)
    return {
        "screen_id_overlap": sorted(ids_a & ids_b),
        "screen_name_overlap": sorted(names_a & names_b),
        "capability_name_overlap": sorted(
            set(pack_a["capabilities"]) & set(pack_b["capabilities"])
        ),
    }


def category_recall(candidate_hist: dict[str, int], reference_hist: dict[str, int]) -> float:
    """Share of reference categories that reappear in the candidate.

    "other" is excluded: it is the absence of a category, so its presence in
    both packs says nothing about convergence.
    """
    wanted = {k for k in reference_hist if k != "other"}
    if not wanted:
        return 1.0
    found = {k for k in candidate_hist if k != "other"}
    return round(len(wanted & found) / len(wanted), 4)


def judge(
    candidate: dict,
    reference: dict,
    min_category_similarity: float = DEFAULT_MIN_CATEGORY_SIMILARITY,
    reference_scope: str = "full",
    min_category_recall: float = DEFAULT_MIN_CATEGORY_RECALL,
) -> dict:
    """Run the full structure-only comparison. Returns the verdict report.

    reference_scope declares what the reference pack claims to cover:
    - "full": the whole product. Gate on symmetric category similarity and
      capability-count ratio — the two descriptions should agree.
    - "sample": a hand-authored slice. Symmetric measures are meaningless
      (the candidate legitimately covers far more), so gate on containment:
      the reference's categories must reappear in the candidate. Similarity
      and count ratio are still reported, as information.
    """
    if reference_scope not in ("full", "sample"):
        raise ValueError(f"unknown reference_scope {reference_scope!r}")
    norm_candidate = normalize_pack(candidate)
    norm_reference = normalize_pack(reference)
    similarity = category_similarity(
        norm_candidate["category_histogram"], norm_reference["category_histogram"]
    )
    recall = category_recall(
        norm_candidate["category_histogram"], norm_reference["category_histogram"]
    )
    counts = (norm_candidate["capability_count"], norm_reference["capability_count"])
    count_ratio = max(counts) / max(1, min(counts))
    overlap = content_overlap(candidate, reference)
    # Gate on screen-id overlap only: synthesized ids carry a structural
    # digest, so an exact match with a hand-authored id can't be coincidence.
    # Name overlap is reported (two products may both name a screen "Sign In")
    # but doesn't fail the gate on its own.
    leaked = len(overlap["screen_id_overlap"]) > MAX_CONTENT_OVERLAP

    checks = {
        "no_content_leakage": {
            "value": overlap,
            "pass": not leaked,
        },
    }
    info = {}
    if reference_scope == "full":
        checks["category_similarity"] = {
            "value": similarity,
            "threshold": min_category_similarity,
            "pass": similarity >= min_category_similarity,
        }
        checks["capability_count_ratio"] = {
            "value": round(count_ratio, 2),
            "threshold": CAPABILITY_COUNT_FACTOR,
            "pass": count_ratio <= CAPABILITY_COUNT_FACTOR,
        }
        info["category_recall"] = recall
    else:
        checks["category_recall"] = {
            "value": recall,
            "threshold": min_category_recall,
            "pass": recall >= min_category_recall,
        }
        info["category_similarity"] = similarity
        info["capability_count_ratio"] = round(count_ratio, 2)
    return {
        "verdict": "PASS" if all(c["pass"] for c in checks.values()) else "FAIL",
        "reference_scope": reference_scope,
        "checks": checks,
        "info": info,
        "candidate": norm_candidate,
        "reference": norm_reference,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidate", required=True, type=Path, help="Synthesized pack dir")
    parser.add_argument("--reference", required=True, type=Path, help="Reference pack dir")
    parser.add_argument(
        "--min-category-similarity",
        type=float,
        default=DEFAULT_MIN_CATEGORY_SIMILARITY,
    )
    parser.add_argument(
        "--reference-scope",
        choices=("full", "sample"),
        default="full",
        help="What the reference covers: the full product, or a hand-authored slice.",
    )
    parser.add_argument(
        "--min-category-recall",
        type=float,
        default=DEFAULT_MIN_CATEGORY_RECALL,
        help="Sample scope only: minimum share of reference categories found in candidate.",
    )
    parser.add_argument("--report", type=Path, default=None, help="Write full JSON report here")
    args = parser.parse_args(argv)

    report = judge(
        load_pack(args.candidate),
        load_pack(args.reference),
        min_category_similarity=args.min_category_similarity,
        reference_scope=args.reference_scope,
        min_category_recall=args.min_category_recall,
    )
    if args.report:
        args.report.write_text(json.dumps(report, indent=2))

    print(f"candidate:  {args.candidate}")
    print(f"reference:  {args.reference} (scope: {args.reference_scope})")
    for name, check in report["checks"].items():
        status = "PASS" if check["pass"] else "FAIL"
        value = check["value"] if name != "no_content_leakage" else {
            k: len(v) for k, v in check["value"].items()
        }
        print(f"  [{status}] {name}: {value}")
    for name, value in report["info"].items():
        print(f"  [info] {name}: {value}")
    print(f"candidate categories: {report['candidate']['category_histogram']}")
    print(f"reference categories: {report['reference']['category_histogram']}")
    print(f"VERDICT: {report['verdict']}")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
