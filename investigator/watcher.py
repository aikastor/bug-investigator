import hashlib
import json
import re
import subprocess
from pathlib import Path

from .models import Incident, SourceFrame, Target

# Match Node file paths and browser script URLs, including Firefox-style frames.
FRAME = re.compile(r"(?:file://)?(/[^\s():]+):(\d+)(?::\d+)?")
BROWSER_FRAME = re.compile(r"https?://[^/\s]+(/[^\s():?#]+):(\d+)(?::\d+)?")


def find_frame(stack: str, target: Target, *, browser: bool) -> SourceFrame | None:
    root = target.local_source_root.resolve()
    for match in (BROWSER_FRAME if browser else FRAME).finditer(stack):
        path, line = match.groups()
        if browser:
            relative = Path(target.browser_source_root) / path.lstrip("/")
        elif Path(path).is_relative_to(target.container_source_root):
            relative = Path(path).relative_to(target.container_source_root)
        else:
            continue
        local = (root / relative).resolve()
        if local.is_relative_to(root) and local.is_file() and int(line) > 0:
            return SourceFrame(path=local.relative_to(root).as_posix(), line=int(line))
    return None


def docker(*args: str) -> str:
    proc = subprocess.run(
        ["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=8, encoding="utf-8", errors="replace", check=False,
    )
    if proc.returncode:
        raise RuntimeError(proc.stdout.strip() or "Docker command failed")
    return proc.stdout


def collect(name: str, target: Target) -> tuple[str, list[Incident]]:
    template = "{{.Id}} {{.State.Status}} {{.State.ExitCode}} {{.State.StartedAt}} {{.State.FinishedAt}}"
    state = docker("container", "inspect", "--format", template, target.container)
    cid, status, exit_code, started, finished = state.split()
    if status not in {"running", "exited", "restarting", "dead"}:
        return status, []
    logs = docker("logs", "--since", started, "--timestamps", "--tail", "250", cid)
    incidents = []
    raw_lines = []
    for line in logs.splitlines():
        timestamp, _, entry = line.partition(" ")
        raw_lines.append(entry)
        if not entry.startswith("BROWSER_ERROR "):
            continue
        try:
            report = json.loads(entry.removeprefix("BROWSER_ERROR "))
            msg, stack = report["message"], report["stack"]
            if not isinstance(msg, str) or not isinstance(stack, str):
                continue
            incidents.append(Incident(
                id=hashlib.sha256(f"{name}/{cid}/{line}".encode()).hexdigest(),
                target=name, kind="browser_error", occurred_at=timestamp,
                message=msg, evidence=stack, frame=find_frame(stack, target, browser=True),
            ))
        except (KeyError, ValueError, TypeError):
            continue

    if status in {"exited", "dead", "restarting"} and int(exit_code):
        evidence = "\n".join(raw_lines)[-16000:]
        message = next((s for s in raw_lines if re.match(r"^[\w.]*Error:", s)), None)
        incidents.append(Incident(
            id=hashlib.sha256(f"{name}/{cid}/{started}/exit".encode()).hexdigest(),
            target=name, kind="container_exit", occurred_at=finished,
            message=message or f"Container exited with code {exit_code}",
            evidence=evidence, frame=find_frame(evidence, target, browser=False),
            exit_code=int(exit_code),
        ))
    return status, incidents
