from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, PositiveInt


class BaselineCase(BaseModel):
    description: str
    command: list[str] = Field(min_length=1)


class DockerBaseline(BaseModel):
    runner_image: str
    app_url: str
    cases: dict[str, BaselineCase]
    suite_command: list[str] = Field(default_factory=list)
    patch_sources: list[str] = Field(default_factory=list)


class GitHubTarget(BaseModel):
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base: str = "main"


class Target(BaseModel):
    container: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    container_source_root: str
    local_source_root: Path
    browser_source_root: str
    baseline: DockerBaseline | None = None
    github: GitHubTarget | None = None


class SourceFrame(BaseModel):
    path: str  # relative to the target's local_source_root
    line: PositiveInt


class Incident(BaseModel):
    id: str
    target: str
    kind: Literal["container_exit", "browser_error"]
    occurred_at: datetime
    message: str
    evidence: str
    frame: SourceFrame | None
    exit_code: int | None = None


class ReadSourceInput(BaseModel):
    target: str
    frame: SourceFrame
    context_lines: int = Field(default=5, ge=0, le=40)


class ReadSourceOutput(BaseModel):
    first_line: PositiveInt
    lines: list[str]


class CheckPatchInput(BaseModel):
    incident_id: str
    unified_diff: str = Field(min_length=1)


class ScenarioRun(BaseModel):
    outcome: Literal["passed", "failed", "error"]
    diagnostics: str


class CheckPatchOutput(BaseModel):
    baseline: ScenarioRun
    patched: ScenarioRun


class PublishPRInput(BaseModel):
    approved_investigation_id: str


class PublishPROutput(BaseModel):
    url: HttpUrl
