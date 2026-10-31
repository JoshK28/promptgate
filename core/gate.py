"""CI gate: turn a finished run into the CLI's exit code. Pure: no I/O, no AWS."""

# A defensive allowance: a rate that equals its limit on paper must not fail by a
# floating-point rounding error (e.g. a baseline minus a tolerance for other values).
EPSILON = 1e-9


def pass_rate(passed: int, total: int) -> float:
    return passed / total if total else 0.0


def gate_exit_code(
    *, status, total, passed, errored, min_pass, baseline_rate=None, tolerance=0.0
) -> tuple[int, str]:
    # Exit 2 means "couldn't decide": a service problem isn't a prompt regression, so
    # these checks come before any comparison of the pass rate.
    if status != "COMPLETE":
        return 2, f"run is {status}, not COMPLETE; cannot decide"
    if errored:
        return 2, f"{errored} case(s) errored; cannot decide"

    rate = pass_rate(passed, total)
    note = "; no baseline, the threshold alone decides" if baseline_rate is None else ""
    if rate + EPSILON < min_pass:
        return 1, f"pass rate {rate:.1%} is below --min-pass {min_pass:.1%}{note}"
    if baseline_rate is not None and rate + EPSILON < baseline_rate - tolerance:
        return 1, (
            f"pass rate {rate:.1%} is below baseline {baseline_rate:.1%} "
            f"minus tolerance {tolerance:.1%}"
        )
    return 0, f"pass rate {rate:.1%} meets --min-pass {min_pass:.1%}{note}"
