"""Scorers for suite cases (spec §18.4). Pure functions: no I/O, no AWS."""

import re
from typing import NamedTuple

# Stored on every run so an old result can be traced to the rule that scored it.
SCORER_VERSIONS = {
    "exact": "exact-v1",
    "contains": "contains-v1",
    "not_contains": "not_contains-v1",
    "regex": "regex-v1",
}


class Verdict(NamedTuple):
    passed: bool
    reason: str


def _clip(text, limit=80):
    # Reasons are stored with each result; model output can be long.
    return text if len(text) <= limit else text[:limit] + "..."


def _verdict(ok, failure_reason):
    return Verdict(ok, "" if ok else failure_reason)


def score(scorer, output, expected, pattern):
    name = scorer.removeprefix("ci:")
    if name not in SCORER_VERSIONS:
        raise ValueError(f"unknown scorer {scorer!r}")
    ci = name != scorer
    norm = str.casefold if ci else (lambda text: text)

    if name == "regex":
        try:
            found = re.search(pattern, output, re.IGNORECASE if ci else 0)
        except re.error as exc:
            return Verdict(False, f"invalid regex: {exc}")
        return _verdict(found is not None, f"no match for /{pattern}/ in {_clip(output)!r}")

    if name == "exact":
        want, got = expected.strip(), output.strip()
        return _verdict(norm(got) == norm(want), f"expected {_clip(want)!r}, got {_clip(got)!r}")

    present = norm(expected) in norm(output)
    if name == "contains":
        return _verdict(present, f"{_clip(expected)!r} not found in {_clip(output)!r}")
    return _verdict(not present, f"forbidden text {_clip(expected)!r} found in output")
