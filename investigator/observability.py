"""Explicit LangSmith telemetry. Only operational fields cross this boundary."""

import asyncio
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from langsmith import Client

logger = logging.getLogger(__name__)
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")


class LangSmithExporter:
    def __init__(self, config):
        self.project = config.get("LANGSMITH_PROJECT") or "bug-investigator"
        self.client = Client(api_key=config["LANGSMITH_API_KEY"], api_url=config.get("LANGSMITH_ENDPOINT"),
                             workspace_id=config.get("LANGSMITH_WORKSPACE_ID"), timeout_ms=3500,
                             auto_batch_tracing=False, omit_traced_runtime_info=True)

    def send(self, creates, updates):
        # Send only these dictionaries. No SDK inference of inputs, runtime or environment.
        self.client.request_with_retries("POST", "/runs/batch", stop_after_attempt=1,
                                         request_kwargs={"json": {"post": creates, "patch": updates}})

    def close(self):
        self.client.close()

    def link(self, trace_id):
        project = self.client.read_project(project_name=self.project)
        return f"{project.url}/r/{trace_id}?poll=true"


def run_record(name, kind, run_id, trace_id, parent, fields):
    started = datetime.now(timezone.utc)
    order = started.strftime("%Y%m%dT%H%M%S%fZ") + run_id
    run = {"id": run_id, "name": name, "run_type": kind, "trace_id": trace_id,
           "start_time": started.isoformat(), "dotted_order": order, "inputs": fields,
           "outputs": {}}
    if parent:
        run.update(parent_run_id=parent["id"], dotted_order=parent["dotted_order"] + "." + order)
    return run


class InvestigationTrace:
    def __init__(self, incident_id, execution_id=None):
        self.id = execution_id or str(uuid4())
        self.root = run_record("investigation", "chain", self.id, self.id, None, {"incident_id": incident_id})
        self.root["extra"] = {"metadata": {"incident_id": incident_id}}
        self.stack = [self.root]
        self.children = []
        self.calls = []
        self.response_ids = set()
        self.exporter = None
        self.status = "disabled"
        self.url = None

    async def start(self, config):
        if (config.get("LANGSMITH_TRACING") or "").lower() != "true":
            return
        self.status = "unavailable"
        if not config.get("LANGSMITH_API_KEY"):
            return
        try:
            self.exporter = LangSmithExporter(config)
            self.root["session_name"] = self.exporter.project
            await asyncio.to_thread(self.exporter.send, [self.root], [])
            self.status = "running"
        except Exception:
            logger.warning("LangSmith start unavailable for execution %s", self.id)

    @contextmanager
    def span(self, name, kind="chain", **fields):
        run = run_record(name, kind, str(uuid4()), self.id, self.stack[-1], fields)
        self.children.append(run)
        self.stack.append(run)
        try:
            yield run
            run["outputs"].setdefault("outcome", "completed")
        except BaseException:
            run["error"] = "Step interrupted or failed"
            run["outputs"]["outcome"] = "failed"
            raise
        finally:
            run["end_time"] = datetime.now(timezone.utc).isoformat()
            self.stack.pop()

    async def response(self, client, purpose, **request):
        with self.span(purpose, "llm", model=request["model"]) as span:
            response = await client.responses.create(**request)
            model = getattr(response, "model", None) or request["model"]
            metadata = {"ls_provider": "openai", "ls_model_name": model, "ls_model_type": "chat"}
            span["extra"] = {"metadata": metadata}
            response_id = getattr(response, "id", None)
            if response_id and response_id in self.response_ids:
                span["outputs"]["outcome"] = "duplicate_response"
                return response
            if response_id:
                self.response_ids.add(response_id)
            usage = getattr(response, "usage", None)
            counts = {k: getattr(usage, k, None) for k in TOKEN_FIELDS}
            self.calls.append({"purpose": purpose, "model": model, **counts})
            span["outputs"].update(model=model, outcome=response.status, **counts)
            known = {k: n for k, n in counts.items() if n is not None}
            if known:
                metadata["usage_metadata"] = known
            return response

    def usage(self):
        return {"responses": len(self.calls), "calls": self.calls, **{
            k: sum(c[k] for c in self.calls) if all(c[k] is not None for c in self.calls) else None
            for k in TOKEN_FIELDS}}

    async def finish(self, outcome):
        self.root["end_time"] = datetime.now(timezone.utc).isoformat()
        self.root["outputs"] = {"outcome": outcome, **{k: v for k, v in self.usage().items() if k != "calls"}}
        if outcome in {"failed", "cancelled"}:
            self.root["error"] = outcome
        if self.exporter is None:
            return
        try:
            if self.status == "running":
                for child in self.children:
                    child["session_name"] = self.exporter.project
                await asyncio.to_thread(self.exporter.send, self.children, [self.root])
                self.url = await asyncio.to_thread(self.exporter.link, self.id)
                self.status = "exported"
        except Exception:
            self.status = "unavailable"
            logger.warning("LangSmith export unavailable for execution %s", self.id)
        finally:
            try:
                await asyncio.to_thread(self.exporter.close)
            except Exception:
                logger.warning("LangSmith client cleanup failed for execution %s", self.id)
