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
                    self.repo_updated.emit(repo)

                    for _ in range(15):
                        time.sleep(2.0)
                        run = client.latest_workflow_run(repo.full_name, workflow, resolved_branch, event="workflow_dispatch")
                        if run:
                            repo.run_id = run.id
                            repo.run_url = run.html_url
                            repo.status = run.display_status
                            self.log.emit(f"{repo.full_name}: run {run.id} is {run.display_status}")
                            self.repo_updated.emit(repo)
                            break

                    if not repo.run_id:
                        repo.status = "Dispatched"
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

    def __init__(self, config: AppConfig, repos: list[RepoState], once: bool = False) -> None:
        super().__init__(config)
        self.repos = repos
        self.once = once
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        client = None

        try:
            client = self.make_client()

            while not self._cancelled:
                active = False

                for repo in self.repos:
                    if repo.run_id is None:
                        continue

                    try:
                        previous = repo.status
                        run = client.get_workflow_run(repo.full_name, repo.run_id)
                        repo.status = run.display_status
                        repo.run_url = run.html_url
                        self.repo_updated.emit(repo)

                        if previous != repo.status:
                            self.log.emit(f"{repo.full_name}: {previous} -> {repo.status}")

                        if repo.status not in TERMINAL_RUN_STATES:
                            active = True

                    except Exception as exc:
                        repo.status = "Poll failed"
                        repo.last_error = self.safe_error(exc)
                        self.repo_updated.emit(repo)
                        self.log.emit(f"{repo.full_name}: poll failed: {repo.last_error}")

                if self.once or not active:
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
