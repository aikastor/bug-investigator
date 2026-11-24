"""Run target-owned regressions on isolated, immutable Docker images."""

from datetime import datetime, timezone
import json
import subprocess
from uuid import uuid4

from .models import Incident, Target


def docker(*args, timeout=12, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, errors="replace",
                          timeout=timeout, check=check)


def diagnostic(exc):
    return str(getattr(exc, "stderr", None) or exc)[-1800:]


def cleanup(result, *args):
    try:
        proc = docker(*args, check=False)
        if proc.returncode and "No such" not in proc.stderr:
            raise ValueError(proc.stderr)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result.setdefault("cleanup_warnings", []).append(diagnostic(exc))


def records(run, prefix):
    parsed = []
    for line in run.stdout.splitlines():
        line = line.removeprefix("# ")
        if line.startswith(prefix):
            parsed.append(json.loads(line[len(prefix):]))
    return parsed


def outcome(run, case_id, incident):
    # Compatibility with baseline-only targets using the original TAP command.
    observations = records(run, "BASELINE_RESULT ")
    if len(observations) == 1 and isinstance(observations[0], dict) and observations[0].get("scenario") == case_id:
        observed = observations[0]
        if run.returncode == 0:
            return {"outcome": "passed", "observation": observed}
        if (run.returncode == 1 and observed.get("message") == incident.message
                and observed.get("actual") != observed.get("expected")
                and "ERR_ASSERTION" in run.stdout):
            return {"outcome": "reproduced", "observation": observed}
        return {"outcome": "inconclusive", "observation": observed,
                "detail": "The test failed without matching the incident and expected assertion."}
    return {"outcome": "inconclusive", "detail": (run.stderr or run.stdout)[-1800:]
            or "The runner returned no matching test observation."}


def test_results(run, case_id=None, incident=None):
    tests = {}
    for row in records(run, "TEST_RESULT "):
        if (not isinstance(row, dict) or not isinstance(row.get("name"), str)
                or not row["name"] or row["name"] in tests
                or row.get("outcome") not in ("passed", "failed", "skipped", "inconclusive")):
            raise ValueError("Invalid or duplicate test result.")
        tests[row["name"]] = row
    observations = records(run, "BASELINE_RESULT ")
    if not all(isinstance(row, dict) for row in observations):
        raise ValueError("Invalid runner observation.")
    result = {"tests": tests, "observations": observations, "outcome": "inconclusive"}
    active = [row for row in tests.values() if row["outcome"] != "skipped"]
    failures = [row for row in active if row["outcome"] == "failed"]
    errors = [row for row in active if row["outcome"] == "inconclusive"]
    if (not active or records(run, "TEST_PLAN ") != [len(tests)] or errors
            or run.returncode != int(bool(failures))):
        result["detail"] = (run.stderr or "\n".join(row.get("detail", "") for row in errors)
                            or run.stdout or "Incomplete runner output.")[-1800:]
    elif case_id is None:
        if len(active) != len(tests):
            result["detail"] = "The full suite skipped tests."
        else:
            result["outcome"] = "failed" if failures else "passed"
    elif len(active) != 1:
        result["detail"] = "Expected exactly one selected test."
    else:
        test = active[0]
        result["test_name"] = test["name"]
        matched = [row for row in observations if row.get("scenario") == case_id]
        observed = matched[0] if len(matched) == 1 else {}
        result["observation"] = observed
        if failures and incident is None:
            result.update(outcome="failed", detail=test.get("detail", "Assertion failed."))
        elif not observed:
            result["detail"] = "Missing selected scenario observation."
        elif not failures:
            result["outcome"] = "passed"
        elif observed.get("message") == incident.message and observed.get("actual") != observed.get("expected"):
            result.update(outcome="reproduced", detail=test.get("detail", "Assertion failed."))
        else:
            result["detail"] = "The assertion did not match the incident."
    return result


def run_tests(image, runner, command, app_url, *, case_id=None, incident=None, legacy=False):
    name = "investigator-replay-" + uuid4().hex
    result = {"app_image": image, "runner_image": runner, "command": list(command), "app_url": app_url}
    try:
        docker("network", "create", "--internal", name)
        docker("run", "--detach", "--pull=never", "--network", name, "--network-alias", "app",
               "--name", name + "-app", image)
        run = docker("run", "--pull=never", "--network", name, "--name", name + "-test",
                     "--env", "APP_URL=" + app_url, runner, *command, check=False, timeout=90)
        classify = outcome if legacy else test_results
        result.update(classify(run, case_id, incident))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result.update(outcome="inconclusive", detail=diagnostic(exc))
    finally:
        cleanup(result, "container", "rm", "-f", name + "-test", name + "-app")
        cleanup(result, "network", "rm", name)
    return result


def run_baseline(incident: Incident, target: Target, case_id: str):
    config = target.baseline
    case = config.cases[case_id]
    result = {"scenario": case_id, "description": case.description,
              "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        image = docker("container", "inspect", "--format", "{{.Image}}", target.container).stdout.strip()
        runner = docker("image", "inspect", "--format", "{{.Id}}", config.runner_image).stdout.strip()
        result.update(run_tests(image, runner, case.command, config.app_url, case_id=case_id,
                                incident=incident, legacy=not config.suite_command))
    except (OSError, subprocess.SubprocessError) as exc:
        result.update(outcome="inconclusive", detail=diagnostic(exc))
    return result
