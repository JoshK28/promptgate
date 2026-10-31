"""PromptGate CLI. Real sign-in and deployed API requests reach AWS."""

import argparse
from getpass import getpass
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx

from cli.session import Session, SessionError
from core.gate import gate_exit_code


class CommandError(Exception):
    """A fixed, safe message for a failed command."""


def _rate(value):
    rate = float(value)
    if not 0 <= rate <= 1:
        raise argparse.ArgumentTypeError("Use a rate between 0 and 1.")
    return rate


def _parser():
    parser = argparse.ArgumentParser(prog="promptgate")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Sign in and save tokens for interactive use.")

    run = commands.add_parser("run", help="Start a run, wait and return its gate exit code.")
    run.add_argument("--config", required=True, metavar="ID[@VERSION]")
    run.add_argument("--suite", required=True, metavar="ID@VERSION")
    run.add_argument("--prompt-file", type=Path)
    run.add_argument("--min-pass", type=_rate, default=0.9)
    run.add_argument("--mode", choices=("fresh", "auto"), default="auto")
    run.add_argument("--tolerance", type=_rate, default=0.0)

    compare = commands.add_parser("compare", help="Print changed case verdicts.")
    compare.add_argument("run_a")
    compare.add_argument("run_b")
    baseline = commands.add_parser("baseline", help="Mark a run as the baseline.")
    baseline.add_argument("run_id")
    return parser


def _versioned(value, *, required):
    identifier, separator, version = value.rpartition("@")
    if not separator:
        if required or not value:
            raise CommandError("The suite must include a version: ID@VERSION.")
        return value, None
    if not identifier or "@" in identifier or not version.isdecimal() or int(version) < 1:
        raise CommandError("Use an ID with a positive integer version: ID@VERSION.")
    return identifier, int(version)


def _checked_json(response):
    if not response.is_success:
        # Do not echo response bodies or raw exceptions: they may contain tokens.
        raise CommandError(f"API request failed (HTTP {response.status_code}).")
    return response.json()


def _api(session, method, path, json=None):
    return _checked_json(session.request(method, path, json=json))


def _run(session, args):
    config_id, config_version = _versioned(args.config, required=False)
    suite_id, suite_version = _versioned(args.suite, required=True)
    if config_version is None:
        configs = _api(session, "GET", "/api/configs")
        versions = [cfg["version"] for cfg in configs if cfg["cfgId"] == config_id]
        if not versions:
            raise CommandError("Config not found.")
        config_version = max(versions)

    if args.prompt_file is not None:
        template = args.prompt_file.read_text(encoding="utf-8")
        config_path = f"/api/configs/{quote(config_id, safe='')}/versions"
        config = _api(session, "GET", f"{config_path}/{config_version}")
        if template != config["template"]:
            # Stored metadata (cfgId, version, created, ...) is not a ConfigBody field.
            fields = (
                "name", "modelId", "system", "template", "inferenceConfig",
                "additionalModelRequestFields",
            )
            body = {key: config[key] for key in fields if key in config}
            body["template"] = template
            config_version = _api(session, "POST", config_path, json=body)["version"]

    body = {
        "configId": config_id, "configVersion": config_version,
        "suiteId": suite_id, "suiteVersion": suite_version,
        "mode": args.mode, "idempotencyKey": str(uuid4()),
    }
    # One initial POST and at most three retries, always with the same body/key.
    for attempt in range(4):
        response = session.request("POST", "/api/runs", json=body)
        if response.status_code != 503 or attempt == 3:
            break
        time.sleep(2)
    run_id = _checked_json(response)["runId"]
    run_path = f"/api/runs/{quote(run_id, safe='')}"
    while True:
        run = _api(session, "GET", run_path)
        if run["status"] in ("COMPLETE", "INCOMPLETE"):
            break
        time.sleep(2)

    print(
        f"Run {run_id}: {run['status']}; total={run['total']} "
        f"passed={run['passed']} errored={run['errored']}"
    )
    code, reason = gate_exit_code(
        status=run["status"], total=run["total"], passed=run["passed"],
        errored=run["errored"], min_pass=args.min_pass,
        baseline_rate=run["baselineRate"], tolerance=args.tolerance,
    )
    print(reason)
    return code


def _results(session, run_path):
    verdicts = {}
    path = run_path + "/results"
    while True:
        page = _api(session, "GET", path)
        # B4: results carry a verdict string (PASS, FAIL or ERROR), not a passed flag.
        verdicts.update({item["caseId"]: item["verdict"] for item in page["items"]})
        if page["cursor"] is None:
            return verdicts
        path = run_path + "/results?" + urlencode({"cursor": page["cursor"]})


def _verdict(verdict):
    return "MISSING" if verdict is None else verdict


def _compare(session, args):
    path_a = f"/api/runs/{quote(args.run_a, safe='')}"
    path_b = f"/api/runs/{quote(args.run_b, safe='')}"
    run_a = _api(session, "GET", path_a)
    run_b = _api(session, "GET", path_b)
    if (run_a["suiteId"], run_a["suiteVersion"]) != (run_b["suiteId"], run_b["suiteVersion"]):
        raise CommandError("Cannot compare runs from different suite IDs or versions.")
    verdicts_a = _results(session, path_a)
    verdicts_b = _results(session, path_b)
    print("case | A | B")
    for case_id in sorted(verdicts_a.keys() | verdicts_b.keys()):
        a, b = verdicts_a.get(case_id), verdicts_b.get(case_id)
        if a != b:
            print(f"{case_id} | {_verdict(a)} | {_verdict(b)}")
    return 0


def main(argv=None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code
    try:
        base_url = os.environ.get("PROMPTGATE_URL")
        client_id = os.environ.get("PROMPTGATE_CLIENT_ID")
        if not base_url or not client_id:
            raise CommandError("Set PROMPTGATE_URL and PROMPTGATE_CLIENT_ID first.")
        session = Session(
            base_url, client_id,
            os.environ.get("PROMPTGATE_REGION", "ap-southeast-2"),
        )
        username = os.environ.get("PROMPTGATE_USERNAME")
        password = os.environ.get("PROMPTGATE_PASSWORD")
        if args.command == "login" and username is None and password is None:
            username = input("Username: ")
            password = getpass("Password: ")
        if username is not None or password is not None:
            if not username or not password:
                raise CommandError("Provide both PROMPTGATE_USERNAME and PROMPTGATE_PASSWORD.")
            session.login(username, password)
        if args.command == "login":
            print("Signed in.")
            return 0
        if args.command == "run":
            return _run(session, args)
        if args.command == "compare":
            return _compare(session, args)
        run_path = f"/api/runs/{quote(args.run_id, safe='')}"
        _api(session, "PUT", run_path + "/baseline")
        print("Baseline updated.")
        return 0
    except (SessionError, CommandError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (httpx.HTTPError, httpx.InvalidURL):
        print("HTTP request failed; check the API URL and connection.", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print("Cannot read the prompt file or API response; check the command and server.", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("Command cancelled.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
