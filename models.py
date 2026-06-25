from __future__ import annotations

from dataclasses import dataclass, field


TERMINAL_RUN_STATES = {
    "success",
    "failure",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
}


@dataclass
class WorkflowInfo:
    id: int
    name: str
    path: str
    state: str


@dataclass
class WorkflowRunInfo:
    id: int
    name: str
    status: str
    conclusion: str | None
    html_url: str
    head_branch: str
    head_sha: str
    created_at: str
    updated_at: str

    @property
    def display_status(self) -> str:
        if self.status == "completed":
            return self.conclusion or "completed"

        return self.status or "unknown"

    @property
    def is_terminal(self) -> bool:
        return self.display_status in TERMINAL_RUN_STATES


@dataclass
class RepoState:
    name: str
    full_name: str
    default_branch: str
    archived: bool = False
    selected: bool = False
    branch_override: str = ""
    resolved_branch: str = ""
    branch_exists: bool | None = None
    workflow_exists: bool | None = None
    workflow_id: int | None = None
    workflow_name: str = ""
    workflow_path: str = ""
    run_id: int | None = None
    run_url: str = ""
    status: str = "Idle"
    last_error: str = ""

    def effective_branch(self, global_branch: str) -> str:
        return self.branch_override.strip() or self.resolved_branch or global_branch.strip() or self.default_branch

    def branch_label(self) -> str:
        if self.branch_exists is True:
            return "OK"
        if self.branch_exists is False:
            return "Missing"
        return "?"

    def workflow_label(self) -> str:
        if self.workflow_exists is True:
            return "OK"
        if self.workflow_exists is False:
            return "Missing"
        return "?"


@dataclass
class CompareInfo:
    status: str
    ahead_by: int
    behind_by: int
    total_commits: int
    html_url: str


@dataclass
class StaleCheckConfig:
    repository: str = ""
    branch: str = ""
    base_branch: str = ""
    workflow: str = ""


@dataclass
class AppConfig:
    organization: str = ""
    workflow: str = ""
    default_branch: str = ""
    token: str = ""
    repo_type: str = "all"
    include_archived: bool = False
    poll_interval_seconds: float = 5.0
    api_concurrency: int = 8
    stale_check: StaleCheckConfig = field(default_factory=StaleCheckConfig)
