"""Apply source-only proposals to a stopped copy of the baseline image."""

import hashlib
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

from .baseline import cleanup, diagnostic, docker, run_tests


def diff_files(diff, sources):
    # Only ordinary text hunks editing existing configured files. Exclude Git
    # extensions such as renames, mode changes, and binary patches.
    lines = diff.splitlines()
    sections = {}
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- a/"):
            raise ValueError("Expected a unified diff starting with --- a/path.")
        start = i
        path = lines[i][6:]
        if (path not in sources or Path(path).is_absolute() or any(p.startswith(".") for p in Path(path).parts)
                or lines[i + 1:i + 2] != ["+++ b/" + path]):
            raise ValueError("The diff must edit an existing configured application source file.")
        i += 2
        if i == len(lines) or not lines[i].startswith("@@ "):
            raise ValueError("Missing unified diff hunk.")
        while i < len(lines) and not lines[i].startswith("--- a/"):
            if lines[i].startswith(("--- ", "+++ ")):
                raise ValueError("Unexpected file header inside a patch hunk.")
            if not lines[i].startswith((" ", "+", "-", "@@ ", "\\ No newline at end of file")):
                raise ValueError("Unexpected unified diff metadata.")
            i += 1
        sections[path] = sections.get(path, "") + "\n".join(lines[start:i]) + "\n"
    if not sections:
        raise ValueError("Empty patch.")
    return sections


def apply_diff(root, diff, sources):
    paths = diff_files(diff, sources)
    proc = subprocess.run(["git", "apply", "--no-index", "--recount", "--whitespace=nowarn", "-"],
                          input=diff.rstrip("\n") + "\n", text=True, capture_output=True,
                          cwd=root, timeout=8)
    if proc.returncode:
        raise ValueError("Cannot apply proposed diff: " + proc.stderr.strip())
    return sorted(paths)


def compare(attempt, baseline, before):
    selected, after = attempt["candidate"], attempt["suite"]
    if selected["outcome"] == "inconclusive" or after["outcome"] == "inconclusive":
        return {"outcome": "inconclusive", "detail": selected.get("detail") if
                selected["outcome"] == "inconclusive" else after.get("detail")}
    old, new = before["tests"], after["tests"]
    name = baseline["test_name"]
    if (old.keys() != new.keys() or name not in old or old[name]["outcome"] != "failed"
            or selected["test_name"] != name):
        return {"outcome": "inconclusive", "detail": "Selected test or suite inventory differs between runs."}
    if selected.get("observation") and selected["observation"].get("inputs") != baseline["observation"].get("inputs"):
        return {"outcome": "inconclusive", "detail": "Selected test inputs differ between runs."}
    regressions = sorted(k for k in old if old[k]["outcome"] == "passed" and new[k]["outcome"] == "failed")
    assertions = []
    if selected["outcome"] == "failed":
        assertions.append(selected["detail"])
    for k in [name, *regressions]:
        if new[k]["outcome"] == "failed":
            assertions.append(k + "\n" + new[k]["detail"])
    return {"outcome": "failed" if assertions else "verified", "new_failures": regressions,
            "detail": "\n\n".join(assertions)}


def verify_candidate(target, repo, diff, baseline, before):
    config = target.baseline
    name = "investigator-patch-" + uuid4().hex
    result = {"diff": diff}
    try:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            docker("create", "--pull=never", "--network", "none", "--name", name, baseline["app_image"])
            for source in config.patch_sources:
                if source not in repo.file_hashes:
                    raise ValueError("Source missing from diagnosis snapshot: " + source)
                dest = root / source
                dest.parent.mkdir(parents=True, exist_ok=True)
                docker("cp", f"{name}:{target.container_source_root}/{source}", str(dest))
                if (dest.is_symlink() or not dest.is_file()
                        or hashlib.sha256(dest.read_bytes()).hexdigest() != repo.file_hashes[source]):
                    raise ValueError("Baseline image differs from diagnosis snapshot: " + source)
            changed = apply_diff(root, diff, config.patch_sources)
            result["file_hashes"] = {
                p: {"before": repo.file_hashes[p], "after": hashlib.sha256((root / p).read_bytes()).hexdigest()}
                for p in changed
            }
            if not any(hashlib.sha256((root / p).read_bytes()).hexdigest() != repo.file_hashes[p] for p in changed):
                raise ValueError("Proposed diff did not change the source.")
            for source in changed:
                docker("cp", str(root / source), f"{name}:{target.container_source_root}/{source}")
            image = docker("commit", name, name, timeout=60).stdout.strip()
            result["app_image"] = image
        runner, url = baseline["runner_image"], baseline["app_url"]
        result["candidate"] = run_tests(image, runner, baseline["command"], url, case_id=baseline["scenario"])
        if result["candidate"]["outcome"] == "inconclusive":
            result.update(outcome="inconclusive", detail=result["candidate"]["detail"])
        else:
            result["suite"] = run_tests(image, runner, before["command"], url)
            result.update(compare(result, baseline, before))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result.update(outcome="inconclusive", detail=diagnostic(exc))
    finally:
        cleanup(result, "container", "rm", "-f", name)
        cleanup(result, "image", "rm", name)
    return result
