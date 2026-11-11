"""Reproduce an inactive English candidate pack from a pinned, licensed source."""

import argparse
import hashlib
import json
import re
from pathlib import Path

REVISION = "57b21406f092110c18776e39b0bda50d37c945c8"
SOURCE_HASH = "a457564a466120c728361e9c759b6a6ef05c2acc05c7e12d1ba0eb251036f42d"
SOURCE_URL = f"https://raw.githubusercontent.com/codespell-project/codespell/{REVISION}/codespell_lib/data/dictionary.txt"


def prepare(source: Path, output: Path) -> None:
    """Verify the upstream bytes and select 6000 unique, unambiguous pairs."""
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_HASH:
        raise ValueError("Source checksum mismatch; refusing unreviewed upstream changes")
    pairs = set()
    for line in raw.decode("utf-8").splitlines():
        incorrect, separator, correct = line.partition("->")
        if (separator and re.fullmatch(r"[a-z]{3,16}", incorrect)
                and re.fullmatch(r"[a-z]{3,16}", correct) and incorrect != correct):
            pairs.add((incorrect, correct))
    # Hash order samples the full alphabet. It is not a frequency/grade ranking.
    selected = sorted(pairs, key=lambda pair: hashlib.sha256("->".join(pair).encode()).hexdigest())[:6000]
    if len(selected) != 6000:
        raise ValueError("Pinned source does not contain the expected candidate count")
    document = {
        "schemaVersion": 1,
        "reviewStatus": "candidate",
        "provenance": "codespell v2.4.3 dictionary, derived from English Wikipedia; not student or teacher-validated data",
        "license": "CC-BY-SA-3.0",
        "sourceUrl": SOURCE_URL,
        "sourceRevision": REVISION,
        "sourceSha256": SOURCE_HASH,
        "selection": "Unique lowercase ASCII pairs, 3–16 letters, one correction, SHA-256 order, first 6000",
        "eligibleSourcePairs": len(pairs),
        "limitations": ["No age/frequency/domain suitability claim", "Technical words may remain",
                        "Context, names and language variants require review", "Never automatically rewrite OCR"],
        "entries": [{"incorrect": wrong, "correct": right, "language": "en",
                     "explanation": "External spelling candidate; check the original image and context."}
                    for wrong, right in sorted(selected)],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    if output.exists() and output.read_bytes() != data:
        raise ValueError("Output exists with different content; use a new release path")
    output.write_bytes(data)
    print(f"Prepared {len(selected)} inactive candidates; sha256={hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("teaching/third_party/codespell/en-candidates.json"))
    arguments = parser.parse_args()
    prepare(arguments.source, arguments.output)
