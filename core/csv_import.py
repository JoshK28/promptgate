"""Parse and validate an uploaded suite CSV (spec §18.4). Pure: no I/O, no AWS."""

import csv
import io
import re

from core.scorers import SCORER_VERSIONS

# description is the check in plain words ("total is 68.40"), shown on the run page in
# place of the raw pattern. Scoring never reads it, so it can't change a verdict.
COLUMNS = ("input", "expected", "scorer", "pattern", "description")
REQUIRED_COLUMNS = ("input", "scorer")  # the others default to "" when absent
MAX_ROWS = 1_000
MAX_BYTES = 1_000_000
MAX_CELL_CHARS = 8_000


class CsvError(Exception):
    """`errors` holds one "row N: message" per problem; the header is row 1."""

    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def parse_suite_csv(text):
    # Checked first so an oversized upload is never parsed.
    size = len(text.encode("utf-8"))
    if size > MAX_BYTES:
        raise CsvError([f"row 1: file is {size} bytes; the limit is {MAX_BYTES}"])

    # Excel's "CSV UTF-8" export starts with a BOM; newline="" lets the csv module
    # handle CRLF and newlines inside quoted cells.
    reader = csv.DictReader(io.StringIO(text.removeprefix("﻿"), newline=""))
    try:
        names = reader.fieldnames or []  # reading the header can raise csv.Error too
    except csv.Error as exc:
        raise CsvError([f"row 1: {exc}"]) from exc
    header_errors = [f"row 1: unknown column {n!r}" for n in names if n not in COLUMNS]
    header_errors += [f"row 1: duplicate column {n!r}" for n in dict.fromkeys(names) if names.count(n) > 1]
    header_errors += [f"row 1: missing column {c!r}" for c in REQUIRED_COLUMNS if c not in names]
    if header_errors:
        raise CsvError(header_errors)

    rows = []
    try:
        for row in reader:
            rows.append(row)
    except csv.Error as exc:  # e.g. one cell larger than the csv module's own limit
        raise CsvError([f"row {len(rows) + 2}: {exc}"]) from exc
    if not rows:
        raise CsvError(["row 1: the file has no data rows"])
    if len(rows) > MAX_ROWS:
        raise CsvError([f"row 1: {len(rows)} data rows; the limit is {MAX_ROWS}"])

    errors = []
    for number, row in enumerate(rows, start=2):
        errors += [f"row {number}: {problem}" for problem in _row_problems(row)]
    if errors:
        raise CsvError(errors)
    return [_cells(row) for row in rows]


def _cells(row):
    return {column: row.get(column, "") for column in COLUMNS}


def _row_problems(row):
    # DictReader fills a short row with None and puts extra cells under the key None.
    if None in row or None in row.values():
        return ["a row has more or fewer cells than the header"]

    cells = _cells(row)
    problems = [
        f"column {column!r} is longer than {MAX_CELL_CHARS} characters"
        for column in COLUMNS
        if len(cells[column]) > MAX_CELL_CHARS
    ]
    scorer = cells["scorer"]
    name = scorer.removeprefix("ci:")
    if name not in SCORER_VERSIONS:
        return problems + [f"unknown scorer {scorer!r}"]
    if name == "regex":
        if not cells["pattern"]:
            problems.append("regex needs a pattern")
        else:
            try:
                re.compile(cells["pattern"])
            except re.error as exc:
                problems.append(f"bad regex: {exc}")
    elif not cells["expected"]:
        problems.append(f"{name} needs an expected value")
    return problems
