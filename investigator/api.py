import asyncio
import json
import os
import sqlite3
import subprocess
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from . import jobs, local_patch, publication, reports, review
from .models import Incident, Target
from .watcher import collect


async def watch(name, target, db, statuses):
    while True:
        try:
            state, incidents = await asyncio.to_thread(collect, name, target)
            jobs.save_incidents(db, incidents)
            statuses[name] = {"container": target.container, "state": state}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as err:
            statuses[name] = {"container": target.container, "state": "unavailable", "detail": str(err)}
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = Path(os.getenv("INVESTIGATOR_CONFIG", "targets.toml")).resolve()
    with config.open("rb") as f:
        targets = {name: Target(**fields) for name, fields in tomllib.load(f)["targets"].items()}
    db_path = Path(os.getenv("INVESTIGATOR_DB", ".local/incidents.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, occurred TEXT, record TEXT)")
    jobs.create_tables(db)
    checkpoints = sqlite3.connect(str(db_path) + ".reviews", check_same_thread=False)
    app.state.review_graph = review.build_graph(checkpoints)
    app.state.configured_targets = targets
    for (incident_id,) in db.execute("""SELECT incident_id FROM investigations
            WHERE json_extract(result,'$.verification.outcome')='verified' AND json_type(result,'$.review') IS NULL""").fetchall():
        review.advance(db, app.state.review_graph, incident_id)
    app.state.db = db
    app.state.targets = {}
    tasks = []
    for name, target in targets.items():
        target.local_source_root = (config.parent / target.local_source_root).resolve()
        app.state.targets[name] = {"container": target.container, "state": "starting"}
        tasks.append(asyncio.create_task(watch(name, target, db, app.state.targets)))
    tasks.append(asyncio.create_task(jobs.work(db, targets, config.parent / ".env", app.state.review_graph)))
    tasks.append(asyncio.create_task(publication.worker(db, targets)))
    tasks.append(asyncio.create_task(local_patch.worker(db, targets)))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        db.close()
        checkpoints.close()


app = FastAPI(title="Bug Investigator", lifespan=lifespan)


@app.get("/targets")
async def targets():
    return app.state.targets


@app.get("/incidents", response_model=list[Incident])
async def incidents():
    rows = app.state.db.execute("SELECT record FROM incidents ORDER BY occurred DESC, id LIMIT 100")
    return [Incident.model_validate_json(row[0]) for row in rows]


def wants_report(request: Request):
    return request.query_params.get("format") != "json" and "text/html" in request.headers.get("accept", "")


@app.get("/investigations")
async def investigations(request: Request, response: Response):
    response.headers["Vary"] = "Accept"
    if wants_report(request):
        rows = app.state.db.execute("""SELECT j.incident_id, j.status, i.record
            FROM investigations j JOIN incidents i ON i.id=j.incident_id ORDER BY j.rowid DESC LIMIT 100""")
        items = []
        for run_id, status, record in rows:
            incident = Incident.model_validate_json(record)
            items.append({"id": run_id, "status": status, "message": incident.message,
                          "target": incident.target, "date": incident.occurred_at.strftime("%d %b %Y, %H:%M %Z")})
        return reports.render("reports.html", items=items)
    rows = app.state.db.execute("""SELECT j.incident_id, j.status, j.error, json_extract(i.record,'$.message')
        FROM investigations j JOIN incidents i ON i.id=j.incident_id ORDER BY j.rowid DESC LIMIT 100""")
    return [dict(zip(("incident_id", "status", "error", "message"), row)) for row in rows]


@app.get("/investigations/{incident_id}")
async def investigation(incident_id: str, request: Request, response: Response):
    response.headers["Vary"] = "Accept"
    row = app.state.db.execute("""SELECT status, fingerprint, tool_calls, result, error
        FROM investigations WHERE incident_id=?""", (incident_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No investigation for this incident.")
    status, fingerprint, calls, result, error = row
    calls = json.loads(calls)
    result = json.loads(result) if result else None
    pub = app.state.db.execute("SELECT status,url,error FROM publications WHERE incident_id=?", (incident_id,)).fetchone()
    publication_info = dict(zip(("status", "url", "error"), pub)) if pub else None
    github = github_target(incident_id)
    if wants_report(request):
        record = app.state.db.execute("SELECT record FROM incidents WHERE id=?", (incident_id,)).fetchone()[0]
        return reports.render("report.html", incident=Incident.model_validate_json(record), status=status,
                              findings=reports.sections(result["summary"]) if result else [],
                              files=reports.evidence(result["citations"], calls) if result else {},
                              calls=calls, error=error, baseline=result.get("baseline") if result else None,
                              verification=result.get("verification") if result else None,
                              review=result.get("review") if result else None, publication=publication_info,
                              local_patch=result.get("local_patch") if result else None,
                              review_ui=os.getenv("INVESTIGATOR_REVIEW_UI", "http://127.0.0.1:8514"))
    execution = app.state.db.execute("""SELECT execution_id, trace_status, trace_url, usage
        FROM investigation_executions WHERE incident_id=? ORDER BY rowid DESC LIMIT 1""", (incident_id,)).fetchone()
    trace = None
    usage = None
    if execution:
        execution_id, trace_status, url, counts = execution
        trace = {"execution_id": execution_id, "status": trace_status, "url": url}
        usage = json.loads(counts) if counts else None
    return {"incident_id": incident_id, "status": status, "source_fingerprint": fingerprint,
            "trace": trace, "usage": usage,
            "tool_calls": calls, "result": result,
            "github_target": github.model_dump() if github else None, "publication": publication_info, "error": error}


def github_target(incident_id):
    row = app.state.db.execute("SELECT json_extract(record,'$.target') FROM incidents WHERE id=?", (incident_id,)).fetchone()
    target = app.state.configured_targets.get(row[0]) if row else None
    return target.github if target else None


@app.post("/investigations/{incident_id}/review/{token}/{decision}")
async def decide(incident_id: str, token: str, decision: Literal["approved", "rejected"], request: Request):
    if request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
        raise HTTPException(403, "Review must originate from this investigator.")
    try:
        outcome = review.advance(app.state.db, app.state.review_graph, incident_id, decision, token,
                                 publishable=github_target(incident_id) is not None)
    except KeyError:
        raise HTTPException(404, "Unknown investigation.")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if wants_report(request):
        return RedirectResponse(f"/investigations/{incident_id}", status_code=303)
    return outcome


@app.post("/investigations/{incident_id}/publication/{token}/retry")
async def retry_publication(incident_id: str, token: str, request: Request):
    if request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
        raise HTTPException(403, "Retry must originate from this investigator.")
    db = app.state.db
    with db:
        updated = db.execute("""UPDATE publications SET status='queued', error=NULL
            WHERE status='failed' AND incident_id=? AND EXISTS (SELECT 1 FROM investigations
            WHERE incident_id=? AND json_extract(result,'$.review.decision')='approved'
            AND json_extract(result,'$.review.token')=?)""", (incident_id, incident_id, token))
    if not updated.rowcount:
        raise HTTPException(409, "No matching failed publication.")
    if wants_report(request):
        return RedirectResponse(f"/investigations/{incident_id}", status_code=303)
    return {"status": "queued"}
