\
from __future__ import annotations

import fnmatch
import re
from typing import Any
from urllib.parse import quote

import httpx

from models import RepoState, WorkflowInfo, WorkflowRunInfo, CompareInfo


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str, log=None) -> None:
        if not token:
            raise GitHubError("Missing GitHub token. Set GITHUB_TOKEN or token in config.yaml.")

        self.log = log
        self.client = httpx.Client(
            base_url="https://api.github.com",
            timeout=30.0,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-actions-qt-commander",
            },
        )

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self.log:
            self.log(f"{method} {url}")

        response = self.client.request(method, url, **kwargs)

        if response.status_code >= 400:
            raise GitHubError(
                f"{method} {url} failed with HTTP {response.status_code}: {response.text[:1600]}"
            )

        return response

    def list_org_repos(self, org: str, repo_type: str = "all", include_archived: bool = False) -> list[RepoState]:
        repos: list[RepoState] = []
        page = 1

        while True:
            response = self.request(
                "GET",
                f"/orgs/{org}/repos",
                params={
                    "type": repo_type,
                    "sort": "full_name",
                    "direction": "asc",
                    "per_page": 100,
                    "page": page,
                },
            )

            items = response.json()
            if not items:
                break

            for item in items:
                archived = bool(item.get("archived", False))
                if archived and not include_archived:
                    continue

                repos.append(
                    RepoState(
                        name=str(item["name"]),
                        full_name=str(item["full_name"]),
                        default_branch=str(item.get("default_branch") or "main"),
                        archived=archived,
                    )
                )

            page += 1

        return repos

    def list_branches(self, full_name: str) -> list[str]:
        branches: list[str] = []
        page = 1

        while True:
            response = self.request(
                "GET",
                f"/repos/{full_name}/branches",
                params={"per_page": 100, "page": page},
            )

            items = response.json()
            if not items:
                break

            branches.extend(str(item["name"]) for item in items)
            page += 1

        return branches

    def resolve_branch_pattern(self, full_name: str, pattern: str) -> str | None:
        pattern = pattern.strip()
        if not pattern:
            return None

        if "*" not in pattern and "?" not in pattern and "[" not in pattern:
            return pattern if self.branch_exists(full_name, pattern) else None

        branches = self.list_branches(full_name)

        # First treat the text as a shell-style wildcard pattern.
        wildcard_matches = [branch for branch in branches if fnmatch.fnmatchcase(branch, pattern)]

        if wildcard_matches:
            return sorted(wildcard_matches)[-1]

        # Then fall back to regex, for users who type a real regex.
        try:
            rx = re.compile(pattern)
            regex_matches = [branch for branch in branches if rx.fullmatch(branch) or rx.search(branch)]
            if regex_matches:
                return sorted(regex_matches)[-1]
        except re.error:
            pass

        return None

    def list_workflows(self, full_name: str) -> list[WorkflowInfo]:
        response = self.request("GET", f"/repos/{full_name}/actions/workflows")
        data = response.json()

        return [
            WorkflowInfo(
                id=int(item["id"]),
                name=str(item.get("name") or ""),
                path=str(item.get("path") or ""),
                state=str(item.get("state") or ""),
            )
            for item in data.get("workflows", [])
        ]

    def resolve_workflow(self, full_name: str, workflow_query: str) -> WorkflowInfo:
        query = workflow_query.strip()
        if not query:
            raise GitHubError("Workflow is empty.")

        workflows = self.list_workflows(full_name)

        for workflow in workflows:
            if workflow.name == query or workflow.path == query or workflow.path.endswith("/" + query):
                return workflow

        available = ", ".join(f"{w.name} ({w.path})" for w in workflows) or "no workflows"
        raise GitHubError(f"Workflow '{query}' not found in {full_name}. Available: {available}")

    def branch_exists(self, full_name: str, branch: str) -> bool:
        # Branch names can contain '/', so encode the branch as one path segment.
        encoded_branch = quote(branch, safe="")
        response = self.client.get(f"/repos/{full_name}/branches/{encoded_branch}")

        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False

        raise GitHubError(
            f"Branch check failed for {full_name}@{branch}: HTTP {response.status_code}: {response.text[:1200]}"
        )

    def dispatch_workflow(self, full_name: str, workflow: WorkflowInfo, ref: str) -> None:
        self.request(
            "POST",
            f"/repos/{full_name}/actions/workflows/{workflow.id}/dispatches",
            json={"ref": ref},
        )

    def latest_workflow_run(
        self,
        full_name: str,
        workflow: WorkflowInfo,
        branch: str,
        event: str | None = None,
    ) -> WorkflowRunInfo | None:
        params: dict[str, Any] = {"branch": branch, "per_page": 10}
        if event:
            params["event"] = event

        response = self.request(
            "GET",
            f"/repos/{full_name}/actions/workflows/{workflow.id}/runs",
            params=params,
        )

        runs = response.json().get("workflow_runs", [])
        if not runs:
            return None

        return self._run_from_json(runs[0])

    def get_workflow_run(self, full_name: str, run_id: int) -> WorkflowRunInfo:
        response = self.request("GET", f"/repos/{full_name}/actions/runs/{run_id}")
        return self._run_from_json(response.json())

    def compare(self, full_name: str, base: str, head: str) -> CompareInfo:
        encoded_base = quote(base, safe="")
        encoded_head = quote(head, safe="")
        response = self.request("GET", f"/repos/{full_name}/compare/{encoded_base}...{encoded_head}")
        data = response.json()

        return CompareInfo(
            status=str(data.get("status") or ""),
            ahead_by=int(data.get("ahead_by") or 0),
            behind_by=int(data.get("behind_by") or 0),
            total_commits=int(data.get("total_commits") or 0),
            html_url=str(data.get("html_url") or ""),
        )

    @staticmethod
    def _run_from_json(data: dict[str, Any]) -> WorkflowRunInfo:
        return WorkflowRunInfo(
            id=int(data["id"]),
            name=str(data.get("name") or ""),
            status=str(data.get("status") or ""),
            conclusion=data.get("conclusion"),
            html_url=str(data.get("html_url") or ""),
            head_branch=str(data.get("head_branch") or ""),
            head_sha=str(data.get("head_sha") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )
