import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from openai import APIError, AsyncOpenAI

from .graph import investigate
from .models import Incident, Target
from .observability import InvestigationTrace
from .repository import Repository
from . import review

logger = logging.getLogger(__name__)


def create_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS publications (
        incident_id TEXT PRIMARY KEY REFERENCES investigations(incident_id),
        status TEXT NOT NULL DEFAULT 'queued', url TEXT, error TEXT, owner TEXT, lease_until INTEGER
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS investigations (
        incident_id TEXT PRIMARY KEY REFERENCES incidents(id),
        status TEXT NOT NULL DEFAULT 'queued',
        owner TEXT, lease_until INTEGER, fingerprint TEXT, result TEXT, error TEXT,
        tool_calls TEXT NOT NULL DEFAULT '[]'
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS investigation_executions (
        execution_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES investigations(incident_id),
        trace_status TEXT, trace_url TEXT, usage TEXT
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS executions_by_incident
                  ON investigation_executions(incident_id)""")


def save_incidents(db, incidents):
    with db:
        for incident in incidents:
            inserted = db.execute("INSERT OR IGNORE INTO incidents VALUES (?, ?, ?)",
                                  (incident.id, incident.occurred_at.isoformat(), incident.model_dump_json()))
            if inserted.rowcount:
                db.execute("INSERT INTO investigations (incident_id) VALUES (?)", (incident.id,))


def claim(db, owner):
    with db:
        row = db.execute("""UPDATE investigations
            SET owner=?, status='running', lease_until=unixepoch()+180, tool_calls='[]'
            WHERE incident_id = (SELECT incident_id FROM investigations
                WHERE status='queued' OR (status='running' AND lease_until <= unixepoch())
                ORDER BY rowid LIMIT 1)
            RETURNING incident_id""", (owner,)).fetchone()
        if row:
            db.execute("INSERT INTO investigation_executions(execution_id, incident_id) VALUES (?,?)", (owner, row[0]))
    return row[0] if row else None


async def renew_lease(db, incident_id, owner):
    while True:
        await asyncio.sleep(45)
        with db:
            db.execute("""UPDATE investigations SET lease_until=unixepoch()+180
                WHERE incident_id=? AND owner=?""", (incident_id, owner))


async def work(db: sqlite3.Connection, targets: dict[str, Target], env_file: Path, review_graph=None):
    while True:
        config = {**dotenv_values(env_file, interpolate=False), **os.environ}
        key = config.get("OPENAI_API_KEY")
        owner = str(uuid4())
        incident_id = claim(db, owner) if key else None
        if incident_id is None:
            await asyncio.sleep(2)
            continue

        trace = InvestigationTrace(incident_id, owner)

        def save_trace():
            db.execute("""UPDATE investigation_executions SET trace_status=?, trace_url=?, usage=?
                          WHERE execution_id=?""", (trace.status, trace.url, json.dumps(trace.usage()), owner))

        def record_tool(name, arguments, output):
            event = json.dumps({"tool": name, "arguments": arguments, "output": output})
            with db:
                updated = db.execute("""UPDATE investigations
                    SET tool_calls=json_insert(tool_calls, '$[#]', json(?)), lease_until=unixepoch()+180
                    WHERE incident_id=? AND owner=?""", (event, incident_id, owner))
                save_trace()
            if not updated.rowcount:
                raise RuntimeError("Run lease expired.")

        result = error = None
        status = "failed"
        heartbeat = asyncio.create_task(renew_lease(db, incident_id, owner))
        try:
            await trace.start(config)
            with db:
                save_trace()
            row = db.execute("SELECT record FROM incidents WHERE id=?", (incident_id,)).fetchone()
            incident = Incident.model_validate_json(row[0])
            repo = await asyncio.to_thread(Repository.load_target, targets[incident.target])
            with db:
                db.execute("UPDATE investigations SET fingerprint=? WHERE incident_id=? AND owner=?",
                           (repo.fingerprint, incident_id, owner))
            async with AsyncOpenAI(api_key=key, timeout=40, max_retries=1) as client:
                result = await investigate(incident, repo, client, record_tool, targets[incident.target], trace)
            status = result["status"]
        except asyncio.CancelledError:
            status = "cancelled"
            with db:
                db.execute("""UPDATE investigations SET status='queued', owner=NULL, lease_until=NULL
                    WHERE incident_id=? AND owner=?""", (incident_id, owner))
            raise
        except APIError as exc:
            # Provider messages can echo credentials or request contents.
            status = "failed"
            error = f"OpenAI {type(exc).__name__} (HTTP {getattr(exc, 'status_code', None)})."
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {str(exc)[:350]}"
            logger.exception("Investigation failed for %s", incident_id)
        finally:
            await trace.finish(status)
            with db:
                save_trace()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        with db:
            finished = db.execute("""UPDATE investigations SET status=?, result=?, error=?, owner=NULL, lease_until=NULL
                WHERE incident_id=? AND owner=?""",
                       (status, json.dumps(result) if result else None, error, incident_id, owner))
        if finished.rowcount and review_graph is not None and result and result.get("verification", {}).get("outcome") == "verified":
            review.advance(db, review_graph, incident_id)
