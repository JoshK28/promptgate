"""Built-in sample suites, shared read-only under the owner `public`."""

from pathlib import Path

from core.csv_import import parse_suite_csv

PUBLIC_OWNER = "public"
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

SAMPLE_SUITES = {
    "pub_capitals": ("Capital cities", SAMPLES_DIR / "capitals.csv"),
    "pub_format": ("Format rules", SAMPLES_DIR / "format_rules.csv"),
}


def ensure_sample_suites(store) -> int:
    # Only a missing version 1 is imported, so every API task can call this at start-up.
    created = 0
    for suite_id, (name, path) in SAMPLE_SUITES.items():
        if store.get_suite_version(PUBLIC_OWNER, suite_id, 1) is None:
            cases = parse_suite_csv(path.read_text(encoding="utf-8"))
            store.put_suite_version(PUBLIC_OWNER, suite_id, name, cases)
            created += 1
    return created
