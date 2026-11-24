import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from fastapi.responses import HTMLResponse

templates = Environment(
    loader=FileSystemLoader(Path(__file__).with_name("templates")),
    autoescape=select_autoescape(["html"]),
)
STATUS_LABELS = {"diagnosed": "Diagnosed", "insufficient_evidence": "Insufficient evidence",
                 "running": "Investigating", "queued": "Queued", "failed": "Investigation failed"}


def render(template, **context):
    html = templates.get_template(template).render(status_labels=STATUS_LABELS, **context)
    return HTMLResponse(html, headers={"Vary": "Accept"})


def sections(summary):
    # Older investigations stored labeled findings in one paragraph.
    headings = {"cause": "Cause", "observed in source": "What the code shows", "inference": "Inference"}
    parts = re.split(r"(?:^|(?<=[.!?])\s+|\n+)(Cause|Observed in source|Inference):\s*", summary, flags=re.I)
    sections = [("Diagnosis", parts[0])] if parts[0].strip() else []
    sections.extend((headings[key.lower()], body) for key, body in zip(parts[1::2], parts[2::2]))
    return [(heading, [p.strip() for p in body.splitlines() if p.strip()]) for heading, body in sections]


def evidence(citations, calls):
    lines = {}
    for call in calls:
        if call["tool"] == "read_source" and isinstance(call["output"], list):
            lines.update({(row["path"], row["line"]): row["text"] for row in call["output"]})
    files = {}
    for path, number in sorted({(c["path"], c["line"]) for c in citations}):
        files.setdefault(path, []).append((number, lines.get((path, number))))
    return files
