"""Generate a repeatable synthetic suite CSV. Local only, no AWS."""

import argparse
import csv
import io


def generate(n: int, seed: int) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("input", "expected", "scorer", "pattern"))
    for i in range(n):
        writer.writerow((
            f"Synthetic load case {i:04d} (seed {seed}): reply with only the number {i}.",
            str(i),
            "contains",
            "",
        ))
    return output.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=300)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    with open(args.out, "w", encoding="utf-8", newline="") as output:
        output.write(generate(args.cases, args.seed))


if __name__ == "__main__":
    main()
