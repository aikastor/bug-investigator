"""Checkpoint the human decision separately from the expensive verification run."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class ReviewState(TypedDict, total=False):
    token: str
    diff_sha256: str
    decision: Literal["approved", "rejected"]
    decided_at: str


def build_graph(connection):
    def human_review(state):
        decision = interrupt({"diff_sha256": state["diff_sha256"]})
        if decision not in ("approved", "rejected"):
            raise ValueError("Invalid review decision.")
        return {"decision": decision, "decided_at": datetime.now(timezone.utc).isoformat()}

    graph = StateGraph(ReviewState)
    graph.add_node("human_review", human_review)
    graph.add_edge(START, "human_review")
    graph.add_edge("human_review", END)
    return graph.compile(checkpointer=SqliteSaver(connection))


def advance(db, graph, incident_id, decision=None, token=None, *, publishable=False):
    # Serialize competing decisions across processes. Checkpoints use a second
    # database so that a completed resume can be reconciled after a process crash.
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT result FROM investigations WHERE incident_id=?", (incident_id,)).fetchone()
        if not row:
            raise KeyError(incident_id)
        result = json.loads(row[0] or "{}")
        verification = result.get("verification") or {}
        if verification.get("outcome") != "verified":
            raise ValueError("Only a verified candidate can be reviewed.")
        diff = verification["attempts"][-1]["diff"]
        digest = hashlib.sha256(diff.encode()).hexdigest()
        config = {"configurable": {"thread_id": f"{incident_id}/{digest}"}}
        state = graph.get_state(config).values
        if not state:
            graph.invoke({"diff_sha256": digest, "token": uuid4().hex}, config)
            state = graph.get_state(config).values
        if decision:
            if state["token"] != token:
                raise ValueError("This review link is stale.")
            if state.get("decision", decision) != decision:
                raise ValueError("A different decision has already been recorded.")
            if not state.get("decision"):
                graph.invoke(Command(resume=decision), config)
                state = graph.get_state(config).values
        review = {**state, "status": state.get("decision", "pending")}
        result["review"] = review
        local = result.get("local_patch", {})
        if decision == "approved" and local.get("status") in (None, "failed"):
            local.pop("error", None)
            result["local_patch"] = {**local, "status": "queued"}
        db.execute("UPDATE investigations SET result=? WHERE incident_id=?", (json.dumps(result), incident_id))
        if review["status"] == "approved" and publishable:
            db.execute("INSERT OR IGNORE INTO publications(incident_id) VALUES (?)", (incident_id,))
        return review
