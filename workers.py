\
from __future__ import annotations

import time

from PySide6.QtCore import QObject, Signal, Slot

from github_client import GitHubClient
from models import AppConfig, RepoState, TERMINAL_RUN_STATES


class WorkerBase(QObject):
    log = Signal(str)
    error = Signal(str)
    finished = Signal()

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config

    def safe_error(self, exc: BaseException) -> str:
        return f"{type(exc).__name__}: {exc}"

    def make_client(self) -> GitHubClient:
        # Each worker gets its own HTTP client in its own worker thread.
        # Reusing one httpx.Client across Qt threads can become unstable during
        # large operations such as validating 100+ repositories.
        return GitHubClient(self.config.token, log=lambda message: self.log.emit(message))


class LoadReposWorker(WorkerBase):
    loaded = Signal(list)

    def __init__(self, config: AppConfig, organization: str) -> None:
        super().__init__(config)
        self.organization = organization

    @Slot()
    def run(self) -> None:
        client = None

        try:
            client = self.make_client()
            repos = client.list_org_repos(
                self.organization,
                repo_type=self.config.repo_type,
                include_archived=self.config.include_archived,
            )
            self.loaded.emit(repos)
        except Exception as exc:
            self.error.emit(self.safe_error(exc))
        finally:
            if client:
                client.close()
            self.finished.emit()


class ValidateReposWorker(WorkerBase):
    repo_updated = Signal(object)
    progress = Signal(int, int)

    def __init__(self, config: AppConfig, repos: list[RepoState], workflow: str, global_branch: str) -> None:
        super().__init__(config)
        self.repos = repos
        self.workflow = workflow
        self.global_branch = global_branch

    @Slot()
    def run(self) -> None:
        client = None

        try:
            client = self.make_client()
            total = len(self.repos)

            for index, repo in enumerate(self.repos, start=1):
                try:
                    repo.status = "Validating"
                    repo.last_error = ""
                    self.repo_updated.emit(repo)

                    branch_or_pattern = repo.branch_override.strip() or self.global_branch
                    resolved_branch = client.resolve_branch_pattern(repo.full_name, branch_or_pattern)

                    repo.resolved_branch = resolved_branch or ""
                    repo.branch_exists = resolved_branch is not None

                    if repo.branch_exists and self.workflow:
                        try:
                            workflow = client.resolve_workflow(repo.full_name, self.workflow)
                            repo.workflow_exists = True
                            repo.workflow_id = workflow.id
                            repo.workflow_name = workflow.name
                            repo.workflow_path = workflow.path
                        except Exception as exc:
                            repo.workflow_exists = False
                            repo.last_error = str(exc)
                    else:
                        repo.workflow_exists = None

                    repo.status = "Ready" if repo.branch_exists and (repo.workflow_exists or not self.workflow) else "Invalid"

                    # Validation only means branch/workflow are usable. It does not mean CI passed.
                    # Also discover the latest workflow run so the CI status column is meaningful
                    # after reopening the app or validating existing branches.
                    if repo.branch_exists and repo.workflow_exists and repo.workflow_id is not None:
                        try:
                            run = client.latest_workflow_run(repo.full_name, workflow, repo.resolved_branch or repo.effective_branch(self.global_branch))
                            if run:
                                repo.run_id = run.id
                                repo.run_url = run.html_url
                                repo.ci_status = run.display_status
                                repo.ci_updated_at = run.updated_at
                                repo.ci_head_sha = run.head_sha
                            else:
                                repo.ci_status = "No runs"
                                repo.ci_updated_at = ""
                                repo.ci_head_sha = ""
                        except Exception as exc:
                            repo.ci_status = "CI lookup failed"
                            repo.last_error = str(exc)

                    self.repo_updated.emit(repo)

                except Exception as exc:
                    repo.status = "Error"
                    repo.last_error = self.safe_error(exc)
                    self.repo_updated.emit(repo)
                    self.log.emit(f"{repo.full_name}: validation failed: {repo.last_error}")

                self.progress.emit(index, total)

                # Small pause to reduce UI event pressure and GitHub secondary-rate-limit risk.
                if index % 20 == 0:
                    time.sleep(0.25)

        except Exception as exc:
            self.error.emit(self.safe_error(exc))
        finally:
            if client:
                client.close()
            self.finished.emit()


class DispatchReposWorker(WorkerBase):
    repo_updated = Signal(object)

    def __init__(self, config: AppConfig, repos: list[RepoState], workflow: str, global_branch: str) -> None:
        super().__init__(config)
        self.repos = repos
        self.workflow = workflow
        self.global_branch = global_branch

    @Slot()
    def run(self) -> None:
        client = None

        try:
            client = self.make_client()

            for repo in self.repos:
                branch_or_pattern = repo.branch_override.strip() or self.global_branch

                try:
                    repo.status = "Checking branch"
                    repo.run_id = None
                    repo.run_url = ""
                    repo.last_error = ""
                    self.repo_updated.emit(repo)

                    resolved_branch = client.resolve_branch_pattern(repo.full_name, branch_or_pattern)

                    if not resolved_branch:
                        repo.resolved_branch = ""
                        repo.branch_exists = False
                        repo.status = "Missing branch"
                        self.repo_updated.emit(repo)
                        self.log.emit(f"{repo.full_name}: missing branch/pattern {branch_or_pattern}")
                        continue

                    repo.resolved_branch = resolved_branch
                    repo.branch_exists = True
                    repo.status = "Resolving workflow"
                    self.repo_updated.emit(repo)

                    workflow = client.resolve_workflow(repo.full_name, self.workflow)
                    repo.workflow_exists = True
                    repo.workflow_id = workflow.id
                    repo.workflow_name = workflow.name
                    repo.workflow_path = workflow.path

                    repo.status = "Dispatching"
                    self.repo_updated.emit(repo)

                    client.dispatch_workflow(repo.full_name, workflow, resolved_branch)
                    self.log.emit(f"{repo.full_name}: dispatched {workflow.path} on {resolved_branch}")

                    repo.status = "Waiting for run"
                    repo.ci_status = "Waiting for run"
                    self.repo_updated.emit(repo)

                    for _ in range(15):
                        time.sleep(2.0)
                        run = client.latest_workflow_run(repo.full_name, workflow, resolved_branch, event="workflow_dispatch")
                        if run:
                            repo.run_id = run.id
                            repo.run_url = run.html_url
                            repo.status = "Dispatched"
                            repo.ci_status = run.display_status
                            repo.ci_updated_at = run.updated_at
                            repo.ci_head_sha = run.head_sha
                            self.log.emit(f"{repo.full_name}: run {run.id} is {run.display_status}")
                            self.repo_updated.emit(repo)
                            break

                    if not repo.run_id:
                        repo.status = "Dispatched"
                        repo.ci_status = "Run not visible yet"
                        self.repo_updated.emit(repo)
                        self.log.emit(f"{repo.full_name}: dispatched, but run is not visible yet.")

                except Exception as exc:
                    repo.status = "Failed"
                    repo.last_error = self.safe_error(exc)
                    self.repo_updated.emit(repo)
                    self.log.emit(f"{repo.full_name}: dispatch failed: {repo.last_error}")

        except Exception as exc:
            self.error.emit(self.safe_error(exc))
        finally:
            if client:
                client.close()
            self.finished.emit()


class PollRunsWorker(WorkerBase):
    repo_updated = Signal(object)

    def __init__(
        self,
        config: AppConfig,
        repos: list[RepoState],
        workflow: str,
        global_branch: str,
        once: bool = False,
        continuous_dashboard: bool = True,
    ) -> None:
        super().__init__(config)
        self.repos = repos
        self.workflow = workflow
        self.global_branch = global_branch
        self.once = once
        self.continuous_dashboard = continuous_dashboard
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        client = None

        try:
            client = self.make_client()

            while not self._cancelled:
                any_active = False

                for repo in self.repos:
                    try:
                        branch = repo.resolved_branch or repo.effective_branch(self.global_branch)

                        if not branch:
                            repo.ci_status = "No branch"
                            self.repo_updated.emit(repo)
                            continue

                        workflow = None

                        if repo.workflow_id is not None:
                            workflow = type("WorkflowRef", (), {
                                "id": repo.workflow_id,
                                "name": repo.workflow_name,
                                "path": repo.workflow_path,
                                "state": "",
                            })()
                        elif self.workflow:
                            workflow = client.resolve_workflow(repo.full_name, self.workflow)
                            repo.workflow_exists = True
                            repo.workflow_id = workflow.id
                            repo.workflow_name = workflow.name
                            repo.workflow_path = workflow.path

                        if not workflow:
                            repo.ci_status = "No workflow"
                            self.repo_updated.emit(repo)
                            continue

                        # Always rediscover the latest run for this workflow+branch.
                        # This fixes two problems:
                        # 1. reopening the app and polling now discovers already existing failed/successful runs;
                        # 2. rerunning CI on GitHub creates a newer run, and the app switches to it automatically.
                        latest = client.latest_workflow_run(repo.full_name, workflow, branch)

                        if not latest:
                            repo.ci_status = "No runs"
                            repo.run_id = None
                            repo.run_url = ""
                            self.repo_updated.emit(repo)
                            continue

                        previous_run_id = repo.run_id
                        previous_ci_status = repo.ci_status

                        repo.run_id = latest.id
                        repo.run_url = latest.html_url
                        repo.ci_status = latest.display_status
                        repo.ci_updated_at = latest.updated_at
                        repo.ci_head_sha = latest.head_sha

                        # Keep Status as operational state, not CI result.
                        if repo.status in {"Idle", "Ready", "Dispatched", "Waiting for run", "Poll failed"}:
                            repo.status = "Ready" if repo.branch_exists is True and repo.workflow_exists is True else repo.status

                        if latest.display_status not in TERMINAL_RUN_STATES:
                            any_active = True

                        if previous_run_id != repo.run_id:
                            self.log.emit(f"{repo.full_name}: tracking latest run {repo.run_id} ({repo.ci_status})")
                        elif previous_ci_status != repo.ci_status:
                            self.log.emit(f"{repo.full_name}: CI {previous_ci_status} -> {repo.ci_status}")

                        self.repo_updated.emit(repo)

                    except Exception as exc:
                        repo.ci_status = "CI poll failed"
                        repo.last_error = self.safe_error(exc)
                        self.repo_updated.emit(repo)
                        self.log.emit(f"{repo.full_name}: CI poll failed: {repo.last_error}")

                if self.once:
                    break

                # In dashboard mode, keep polling even when everything is currently terminal,
                # because a user may re-run a workflow in GitHub while the app is open.
                if not self.continuous_dashboard and not any_active:
                    break

                time.sleep(self.config.poll_interval_seconds)

        except Exception as exc:
            self.error.emit(self.safe_error(exc))
        finally:
            if client:
                client.close()
            self.finished.emit()


class StaleCheckWorker(WorkerBase):
    result = Signal(str)

    @Slot()
    def run(self) -> None:
        client = None

        try:
            client = self.make_client()
            cfg = self.config.stale_check

            if not cfg.repository or not cfg.branch or not cfg.base_branch:
                self.result.emit("Configure stale_check.repository, stale_check.branch and stale_check.base_branch.")
                return

            branch = client.resolve_branch_pattern(cfg.repository, cfg.branch)
            if not branch:
                self.result.emit(f"{cfg.repository}: branch/pattern '{cfg.branch}' does not exist.")
                return

            compare = client.compare(cfg.repository, cfg.base_branch, branch)

            message = (
                f"{cfg.repository}: {branch} vs {cfg.base_branch}: "
                f"status={compare.status}, ahead={compare.ahead_by}, behind={compare.behind_by}."
            )

            if cfg.workflow:
                workflow = client.resolve_workflow(cfg.repository, cfg.workflow)
                run = client.latest_workflow_run(cfg.repository, workflow, branch)
                if run:
                    message += f" Latest {cfg.workflow} run on {branch}: {run.display_status} ({run.updated_at})."
                else:
                    message += f" No {cfg.workflow} runs found on {branch}."

            self.result.emit(message)

        except Exception as exc:
            self.error.emit(self.safe_error(exc))
        finally:
            if client:
                client.close()
            self.finished.emit()
