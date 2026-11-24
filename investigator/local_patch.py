"""Install the exact verified change in the target working tree after review."""

import asyncio
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from .patches import apply_diff, diff_files
from .repository import Repository


def checked_file(root, name):
    path = root / name
    if path.resolve() != path or not path.is_file():
        raise ValueError(f"Expected a regular source file inside the checkout: {name}")
    return path


def changes(target, result):
    verified = result["verification"]
    attempt = verified["attempts"][-1]
    diff = attempt["diff"]
    review = result["review"]
    if (verified["outcome"] != "verified" or review.get("decision") != "approved"
            or review.get("diff_sha256") != hashlib.sha256(diff.encode()).hexdigest()):
        raise ValueError("The diff does not match a verified, approved candidate.")
    return diff_files(diff, target.baseline.patch_sources)


def patched_bytes(temp, name, data, diff):
    path = temp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    apply_diff(temp, diff, [name])
    return path.read_bytes()


def legacy_hashes(root, fingerprint, sections):
    # Older reports recorded one hash for the whole diagnosis snapshot. Derive
    # file hashes only from an exact match and persist them before any write.
    if Repository.load(root).fingerprint != fingerprint:
        raise ValueError("The local checkout differs from the original diagnosis snapshot.")
    hashes = {}
    with TemporaryDirectory() as tmp:
        for name, diff in sections.items():
            data = checked_file(root, name).read_bytes()
            fixed = patched_bytes(Path(tmp), name, data, diff)
            hashes[name] = {"before": hashlib.sha256(data).hexdigest(), "after": hashlib.sha256(fixed).hexdigest()}
    return hashes


def install(root, sections, hashes):
    if sections.keys() != hashes.keys():
        raise ValueError("The verified file list differs from the approved diff.")
    pending = {}
    with TemporaryDirectory() as tmp:
        for name, diff in sections.items():
            path = checked_file(root, name)
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest == hashes[name]["after"]:
                continue
            if digest != hashes[name]["before"]:
                raise ValueError(f"Local edits conflict with the verified version of {name}.")
            fixed = patched_bytes(Path(tmp), name, data, diff)
            if hashlib.sha256(fixed).hexdigest() != hashes[name]["after"]:
                raise ValueError(f"The patched bytes differ from the verified candidate for {name}.")
            pending[name] = (data, fixed)
    # Preflight every file, then replace each atomically. On restart, exact
    # postimages are skipped, including after an interrupted multi-file edit.
    for name, (original, fixed) in pending.items():
        path = checked_file(root, name)
        with NamedTemporaryFile(dir=path.parent, prefix=".investigator-", delete=False) as output:
            dest = Path(output.name)
            try:
                output.write(fixed)
                output.flush()
                os.fchmod(output.fileno(), stat.S_IMODE(path.stat().st_mode))
                if checked_file(root, name).read_bytes() != original:
                    raise ValueError(f"Source changed while applying the fix to {name}.")
                os.replace(dest, path)
            finally:
                dest.unlink(missing_ok=True)


def apply_next(db, targets):
    row = db.execute("""SELECT j.incident_id, j.fingerprint, j.result, json_extract(i.record,'$.target')
        FROM investigations j JOIN incidents i ON i.id=j.incident_id
        WHERE json_extract(j.result,'$.local_patch.status')='queued' ORDER BY j.rowid LIMIT 1""").fetchone()
    if not row:
        return
    incident_id, fingerprint, raw, target_id = row
    result = json.loads(raw)
    try:
        target = targets[target_id]
        root = target.local_source_root.resolve(strict=True)
        sections = changes(target, result)
        hashes = result["verification"]["attempts"][-1].get("file_hashes") or result["local_patch"].get("file_hashes")
        if not hashes:
            hashes = legacy_hashes(root, fingerprint, sections)
            with db:
                db.execute("""UPDATE investigations SET result=json_set(result,'$.local_patch.file_hashes',json(?))
                    WHERE incident_id=? AND json_extract(result,'$.local_patch.status')='queued'""",
                    (json.dumps(hashes), incident_id))
        with db:
            # This short write lock also serializes different approvals against
            # the same checkout when several investigator processes are running.
            db.execute("BEGIN IMMEDIATE")
            status = db.execute("SELECT json_extract(result,'$.local_patch.status') FROM investigations WHERE incident_id=?",
                                (incident_id,)).fetchone()
            if status != ("queued",):
                return
            install(root, sections, hashes)
            db.execute("""UPDATE investigations SET result=json_set(result,'$.local_patch',json(?)) WHERE incident_id=?""",
                       (json.dumps({"status": "applied", "files": sorted(sections)}), incident_id))
    except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
        with db:
            db.execute("""UPDATE investigations SET result=json_set(result,'$.local_patch.status','failed',
                '$.local_patch.error',?) WHERE incident_id=? AND json_extract(result,'$.local_patch.status')='queued'""",
                (str(exc)[:1200], incident_id))


async def worker(db, targets):
    while True:
        apply_next(db, targets)
        await asyncio.sleep(1)
