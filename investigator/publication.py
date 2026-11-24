"""Publish an approved, verified source change to its configured GitHub repository."""

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .patches import apply_diff
from .repository import Repository


def command(args, *, cwd=None):
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=45)
    if proc.returncode:
        raise ValueError((proc.stderr or proc.stdout or "Command failed.")[-1200:])
    return proc.stdout.strip()


def publish(target, incident_id, fingerprint, result):
    config = target.github
    verification = result["verification"]
    attempt = verification["attempts"][-1]
    diff = attempt["diff"]
    digest = hashlib.sha256(diff.encode()).hexdigest()
    review = result.get("review", {})
    if (not config or verification["outcome"] != "verified" or review.get("decision") != "approved"
            or review.get("diff_sha256") != digest):
        raise ValueError("Publication requires a configured repository and approval for this verified diff.")
    branch = f"bug-investigator/{incident_id[:16]}-{digest[:12]}"
    git = ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
    gh = ["gh", "pr"]
    existing = json.loads(command([*gh, "list", "--repo", config.repo, "--head", branch, "--base", config.base,
                                   "--state", "all", "--json", "url,isCrossRepository"]))
    for pr in existing:
        if not pr["isCrossRepository"]:
            return pr["url"]
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "app"
        command([*git, "clone", "--quiet", "--single-branch", "--branch", config.base,
                 f"https://github.com/{config.repo}.git", str(root)])
        if Repository.load(root).fingerprint != fingerprint:
            raise ValueError("The GitHub base source differs from the verified diagnosis snapshot.")
        paths = apply_diff(root, diff, target.baseline.patch_sources)
        command([*git, "add", "--", *paths], cwd=root)
        tree = command([*git, "write-tree"], cwd=root)
        ref = "refs/heads/" + branch
        if command([*git, "ls-remote", "--heads", "origin", ref], cwd=root):
            command([*git, "fetch", "--quiet", "origin", ref], cwd=root)
            if command([*git, "rev-parse", "FETCH_HEAD^{tree}"], cwd=root) != tree:
                raise ValueError("The existing candidate branch contains a different change.")
        else:
            command([*git, "-c", "user.name=Bug Investigator", "-c", "user.email=bug-investigator@users.noreply.github.com",
                     "commit", "-m", f"Fix verified regression in {result['baseline']['scenario']}"], cwd=root)
            command([*git, "push", "origin", f"HEAD:{ref}"], cwd=root)
        body = Path(tmp) / "review.md"
        before = result["baseline"]
        after = attempt["candidate"]
        def section(run):
            return json.dumps(run["observation"], ensure_ascii=False, indent=2)
        suite_rows = ["| Test | Baseline | Candidate |", "| --- | --- | --- |"]
        for name, test in verification["baseline_suite"]["tests"].items():
            suite_rows.append(f"| {name} | {test['outcome']} | {attempt['suite']['tests'][name]['outcome']} |")
        body.write_text(f"## Diagnosis\n\n{result['summary']}\n\n## Verified checkout\n\n"
                        f"Baseline\n\n```json\n{section(before)}\n```\n\nCandidate\n\n```json\n{section(after)}\n```\n\n"
                        + "\n".join(suite_rows) + f"\n\nInvestigation `{incident_id}`\n\n"
                        f"Runner `{before['runner_image']}`\n\nApproved {review['decided_at']}.\n")
        return command([*gh, "create", "--repo", config.repo, "--base", config.base, "--head", branch,
                        "--title", f"Fix verified {before['scenario']} regression", "--body-file", str(body)], cwd=root)


async def worker(db, targets):
    while True:
        owner = uuid4().hex
        with db:
            row = db.execute("""UPDATE publications SET status='publishing', owner=?, lease_until=unixepoch()+900
                WHERE incident_id=(SELECT incident_id FROM publications WHERE status='queued' OR
                (status='publishing' AND lease_until <= unixepoch()) ORDER BY rowid LIMIT 1)
                RETURNING incident_id""", (owner,)).fetchone()
        if not row:
            await asyncio.sleep(1)
            continue
        incident_id = row[0]
        url = error = None
        try:
            target_id, fingerprint, raw = db.execute("""SELECT json_extract(i.record,'$.target'), j.fingerprint, j.result
                FROM investigations j JOIN incidents i ON i.id=j.incident_id WHERE j.incident_id=?""",
                (incident_id,)).fetchone()
            url = await asyncio.to_thread(publish, targets[target_id], incident_id, fingerprint, json.loads(raw))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            error = str(exc)[:1500]
        with db:
            db.execute("""UPDATE publications SET status=?, url=?, error=?, owner=NULL, lease_until=NULL
                WHERE incident_id=? AND owner=?""", ("failed" if error else "published", url, error, incident_id, owner))
