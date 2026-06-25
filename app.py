\
from __future__ import annotations

import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QSettings
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from config_loader import load_config
from github_client import GitHubClient, GitHubError
from models import AppConfig, RepoState
from workers import (
    DispatchReposWorker,
    LoadReposWorker,
    PollRunsWorker,
    StaleCheckWorker,
    ValidateReposWorker,
)


COL_SELECTED = 0
COL_REPOSITORY = 1
COL_OVERRIDE = 2
COL_EFFECTIVE_BRANCH = 3
COL_BRANCH = 4
COL_WORKFLOW = 5
COL_RUN = 6
COL_CI_STATUS = 7
COL_STATUS = 8


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        self.config = load_config()
        self.client: GitHubClient | None = None
        self.repos: list[RepoState] = []
        self.threads: list[QThread] = []
        self.workers: list[object] = []
        self.poll_worker: PollRunsWorker | None = None
        self.poll_thread: QThread | None = None
        self._validating_count = 0

        self.setWindowTitle("GitHub Actions Qt Commander")
        self.resize(1800, 1000)

        self._build_ui()
        self._restore_settings()
        self._connect_client()

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setContentsMargins(0, 0, 0, 0)

        self.organization_edit = QLineEdit(self.config.organization)
        self.workflow_edit = QLineEdit(self.config.workflow)
        self.branch_edit = QLineEdit(self.config.default_branch)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter repositories...")

        self.only_branch_checkbox = QCheckBox("Only repos with global branch/pattern")
        self.only_workflow_checkbox = QCheckBox("Only repos with workflow")

        form.addRow("Organization", self.organization_edit)
        form.addRow("Workflow", self.workflow_edit)
        form.addRow("Global branch/pattern", self.branch_edit)
        form.addRow("Filter", self.filter_edit)

        filter_widget = QWidget()
        filter_layout = QHBoxLayout(filter_widget)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.addWidget(self.only_branch_checkbox)
        filter_layout.addWidget(self.only_workflow_checkbox)
        filter_layout.addStretch(1)
        form.addRow("Show", filter_widget)

        buttons_widget = QWidget()
        buttons_layout = QVBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)

        self.refresh_button = QPushButton("Refresh")
        self.select_visible_button = QPushButton("Select visible")
        self.clear_button = QPushButton("Clear")
        self.validate_button = QPushButton("Validate visible")
        self.run_button = QPushButton("Run selected")
        self.poll_button = QPushButton("Poll once")
        self.stop_poll_button = QPushButton("Stop polling")
        self.open_run_button = QPushButton("Open run")
        self.stale_button = QPushButton("Stale check")
        self.save_bundle_button = QPushButton("Save bundle")
        self.load_bundle_button = QPushButton("Load bundle")

        for button in [
            self.refresh_button,
            self.select_visible_button,
            self.clear_button,
            self.validate_button,
            self.run_button,
            self.poll_button,
            self.stop_poll_button,
            self.open_run_button,
            self.stale_button,
            self.save_bundle_button,
            self.load_bundle_button,
        ]:
            buttons_layout.addWidget(button)

        controls_layout.addWidget(form_widget, 1)
        controls_layout.addWidget(buttons_widget)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "Run",
            "Repository",
            "Branch override",
            "Effective branch",
            "Branch",
            "Workflow",
            "Run ID",
            "Latest CI",
            "Status",
        ])
        self.table.setSortingEnabled(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.verticalHeader().setVisible(False)

        header = self.table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setStretchLastSection(False)
        for column in range(self.table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.Interactive)

        self.table.setColumnWidth(COL_SELECTED, 45)
        self.table.setColumnWidth(COL_REPOSITORY, 330)
        self.table.setColumnWidth(COL_OVERRIDE, 220)
        self.table.setColumnWidth(COL_EFFECTIVE_BRANCH, 360)
        self.table.setColumnWidth(COL_BRANCH, 70)
        self.table.setColumnWidth(COL_WORKFLOW, 80)
        self.table.setColumnWidth(COL_RUN, 130)
        self.table.setColumnWidth(COL_CI_STATUS, 120)
        self.table.setColumnWidth(COL_STATUS, 120)

        self.details = QTextEdit()
        self.details.setReadOnly(True)

        self.log = QTextEdit()
        self.log.setReadOnly(True)

        right_splitter = QSplitter(Qt.Vertical)
        right_splitter.addWidget(self.details)
        right_splitter.addWidget(self.log)
        right_splitter.setStretchFactor(0, 1)
        right_splitter.setStretchFactor(1, 2)

        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.addWidget(self.table)
        main_splitter.addWidget(right_splitter)
        main_splitter.setStretchFactor(0, 3)
        main_splitter.setStretchFactor(1, 2)

        root.addWidget(controls)
        root.addWidget(main_splitter, 1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        self.refresh_button.clicked.connect(self.refresh_repositories)
        self.select_visible_button.clicked.connect(self.select_visible)
        self.clear_button.clicked.connect(self.clear_selection)
        self.validate_button.clicked.connect(self.validate_visible)
        self.run_button.clicked.connect(self.run_selected)
        self.poll_button.clicked.connect(lambda: self.poll_runs(once=True))
        self.stop_poll_button.clicked.connect(self.stop_polling)
        self.open_run_button.clicked.connect(self.open_selected_run)
        self.stale_button.clicked.connect(self.stale_check)
        self.save_bundle_button.clicked.connect(self.save_bundle)
        self.load_bundle_button.clicked.connect(self.load_bundle)

        self.filter_edit.textChanged.connect(self.apply_filter)
        self.only_branch_checkbox.toggled.connect(self.apply_filter)
        self.only_workflow_checkbox.toggled.connect(self.apply_filter)
        self.branch_edit.textChanged.connect(self.update_effective_branches)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.itemSelectionChanged.connect(self.update_details)

        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)
        for label, callback in [
            ("Refresh", self.refresh_repositories),
            ("Validate", self.validate_visible),
            ("Run", self.run_selected),
            ("Poll", lambda: self.poll_runs(once=True)),
            ("Open run", self.open_selected_run),
            ("Stale", self.stale_check),
            ("Save bundle", self.save_bundle),
            ("Load bundle", self.load_bundle),
        ]:
            action = QAction(label, self)
            action.triggered.connect(callback)
            toolbar.addAction(action)

    def _restore_settings(self) -> None:
        self.settings = QSettings("BioVault", "GitHubActionsQtCommander")
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def closeEvent(self, event) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop_polling()

        if self.client:
            self.client.close()

        super().closeEvent(event)

    def _connect_client(self) -> None:
        try:
            self.client = GitHubClient(self.config.token, log=self.log_message)
            self.set_status("Ready.")
        except GitHubError as exc:
            self.set_status(str(exc))
            self.log_message(str(exc))

    def set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)

    def log_message(self, text: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{now}] {text}")

    def visible_repo_indices(self) -> list[int]:
        indices = []

        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                item = self.table.item(row, COL_REPOSITORY)
                if item:
                    indices.append(int(item.data(Qt.UserRole)))

        return indices

    def selected_repos(self) -> list[RepoState]:
        return [repo for repo in self.repos if repo.selected]

    def current_repo(self) -> RepoState | None:
        row = self.table.currentRow()
        if row < 0:
            return None

        item = self.table.item(row, COL_REPOSITORY)
        if not item:
            return None

        index = item.data(Qt.UserRole)
        if index is None:
            return None

        return self.repos[int(index)]

    def populate_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.repos))

        for index, repo in enumerate(self.repos):
            self._populate_row(index, repo)

        self.table.blockSignals(False)
        self.apply_filter()
        self.update_details()

    def _populate_row(self, row: int, repo: RepoState) -> None:
        selected = QTableWidgetItem()
        selected.setFlags(selected.flags() | Qt.ItemIsUserCheckable)
        selected.setCheckState(Qt.Checked if repo.selected else Qt.Unchecked)
        selected.setData(Qt.UserRole, row)
        self.table.setItem(row, COL_SELECTED, selected)

        repository = QTableWidgetItem(repo.full_name)
        repository.setFlags(repository.flags() & ~Qt.ItemIsEditable)
        repository.setData(Qt.UserRole, row)
        self.table.setItem(row, COL_REPOSITORY, repository)

        override = QTableWidgetItem(repo.branch_override)
        override.setData(Qt.UserRole, row)
        self.table.setItem(row, COL_OVERRIDE, override)

        effective = QTableWidgetItem(repo.effective_branch(self.branch_edit.text().strip()))
        effective.setFlags(effective.flags() & ~Qt.ItemIsEditable)
        effective.setData(Qt.UserRole, row)
        self.table.setItem(row, COL_EFFECTIVE_BRANCH, effective)

        branch = QTableWidgetItem(repo.branch_label())
        branch.setFlags(branch.flags() & ~Qt.ItemIsEditable)
        branch.setData(Qt.UserRole, row)
        self._apply_status_color(branch, repo.branch_label())
        self.table.setItem(row, COL_BRANCH, branch)

        workflow = QTableWidgetItem(repo.workflow_label())
        workflow.setFlags(workflow.flags() & ~Qt.ItemIsEditable)
        workflow.setData(Qt.UserRole, row)
        self._apply_status_color(workflow, repo.workflow_label())
        self.table.setItem(row, COL_WORKFLOW, workflow)

        run = QTableWidgetItem(str(repo.run_id or ""))
        run.setFlags(run.flags() & ~Qt.ItemIsEditable)
        run.setData(Qt.UserRole, row)
        self.table.setItem(row, COL_RUN, run)

        ci_status = QTableWidgetItem(repo.ci_status)
        ci_status.setFlags(ci_status.flags() & ~Qt.ItemIsEditable)
        ci_status.setData(Qt.UserRole, row)
        self._apply_status_color(ci_status, repo.ci_status)
        self.table.setItem(row, COL_CI_STATUS, ci_status)

        status = QTableWidgetItem(repo.status)
        status.setFlags(status.flags() & ~Qt.ItemIsEditable)
        status.setData(Qt.UserRole, row)
        self._apply_status_color(status, repo.status)
        self.table.setItem(row, COL_STATUS, status)

    def _apply_status_color(self, item: QTableWidgetItem, status: str) -> None:
        normalized = status.lower()

        if normalized in {"ok", "success", "ready"}:
            item.setForeground(QColor("#2e7d32"))
        elif normalized in {"missing", "failure", "failed", "invalid", "error", "poll failed", "ci poll failed", "ci lookup failed"}:
            item.setForeground(QColor("#c62828"))
        elif normalized in {"queued", "in_progress", "waiting for run", "dispatching", "validating", "checking branch", "resolving workflow", "run not visible yet"}:
            item.setForeground(QColor("#ef6c00"))

    def update_repo_row(self, repo: RepoState) -> None:
        try:
            index = self.repos.index(repo)
        except ValueError:
            return

        self.table.blockSignals(True)
        self._populate_row(index, repo)
        self.table.blockSignals(False)
        self.apply_filter()
        self.update_details()

    def on_item_changed(self, item: QTableWidgetItem) -> None:
        repo_index = item.data(Qt.UserRole)
        if repo_index is None:
            return

        repo = self.repos[int(repo_index)]

        if item.column() == COL_SELECTED:
            repo.selected = item.checkState() == Qt.Checked

        if item.column() == COL_OVERRIDE:
            repo.branch_override = item.text().strip()
            repo.resolved_branch = ""
            repo.branch_exists = None
            self.update_repo_row(repo)

    def update_effective_branches(self) -> None:
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_REPOSITORY)
            if not item:
                continue

            repo = self.repos[int(item.data(Qt.UserRole))]
            repo.resolved_branch = ""
            repo.branch_exists = None

            effective_item = self.table.item(row, COL_EFFECTIVE_BRANCH)
            if effective_item:
                effective_item.setText(repo.effective_branch(self.branch_edit.text().strip()))

            branch_item = self.table.item(row, COL_BRANCH)
            if branch_item:
                branch_item.setText(repo.branch_label())

        self.table.blockSignals(False)
        self.apply_filter()

    def apply_filter(self) -> None:
        filt = self.filter_edit.text().strip().lower()
        only_branch = self.only_branch_checkbox.isChecked()
        only_workflow = self.only_workflow_checkbox.isChecked()

        visible_count = 0
        unknown_branch_count = 0
        unknown_workflow_count = 0

        for row in range(self.table.rowCount()):
            repo_item = self.table.item(row, COL_REPOSITORY)
            if not repo_item:
                continue

            repo = self.repos[int(repo_item.data(Qt.UserRole))]

            visible = True

            if filt and filt not in repo.full_name.lower() and filt not in repo.name.lower():
                visible = False

            if only_branch:
                if repo.branch_exists is None:
                    unknown_branch_count += 1
                if repo.branch_exists is not True:
                    visible = False

            if only_workflow:
                if repo.workflow_exists is None:
                    unknown_workflow_count += 1
                if repo.workflow_exists is not True:
                    visible = False

            self.table.setRowHidden(row, not visible)

            if visible:
                visible_count += 1

        if only_branch or only_workflow:
            hint_parts = [f"{visible_count} repositories visible"]
            if unknown_branch_count:
                hint_parts.append(f"{unknown_branch_count} branch checks unknown; click Validate visible first")
            if unknown_workflow_count:
                hint_parts.append(f"{unknown_workflow_count} workflow checks unknown; click Validate visible first")
            self.set_status(". ".join(hint_parts) + ".")

    def update_details(self) -> None:
        repo = self.current_repo()

        if not repo:
            self.details.setText("No repository selected.")
            return

        self.details.setText(
            f"{repo.full_name}\\n\\n"
            f"Default branch: {repo.default_branch}\\n"
            f"Effective branch: {repo.effective_branch(self.branch_edit.text().strip())}\\n"
            f"Resolved branch: {repo.resolved_branch}\\n"
            f"Branch check: {repo.branch_label()}\\n"
            f"Workflow check: {repo.workflow_label()}\\n"
            f"Workflow: {repo.workflow_name or repo.workflow_path}\\n"
            f"Run ID: {repo.run_id or ''}\\n"
            f"Run URL: {repo.run_url}\\n"
            f"Latest CI: {repo.ci_status}\\n"
            f"CI updated: {repo.ci_updated_at}\\n"
            f"CI SHA: {repo.ci_head_sha}\\n"
            f"Status: {repo.status}\\n"
            f"Error: {repo.last_error}"
        )

    def on_validation_progress(self, current: int, total: int) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)
        self.set_status(f"Validating repositories: {current}/{total}")

    def run_worker(self, worker) -> None:
        thread = QThread(self)

        self.threads.append(thread)
        self.workers.append(worker)

        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log.connect(self.log_message)
        worker.error.connect(lambda message: (self.log_message(message), self.set_status(message)))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda: self.workers.remove(worker) if worker in self.workers else None)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: self.threads.remove(thread) if thread in self.threads else None)

        thread.start()

    def refresh_repositories(self) -> None:
        if not self.client:
            self._connect_client()
            if not self.client:
                return

        organization = self.organization_edit.text().strip()
        if not organization:
            QMessageBox.warning(self, "Missing organization", "Please enter a GitHub organization.")
            return

        self.set_status(f"Loading repositories for {organization}...")
        worker = LoadReposWorker(self.config, organization)
        worker.loaded.connect(self.on_repos_loaded)
        worker.error.connect(lambda message: self.set_status(f"Refresh failed: {message}"))
        self.run_worker(worker)

    def on_repos_loaded(self, repos: list[RepoState]) -> None:
        self.repos = repos
        self.populate_table()
        self.set_status(f"Loaded {len(repos)} repositories.")
        self.log_message(f"Loaded {len(repos)} repositories.")

    def select_visible(self) -> None:
        self.table.blockSignals(True)

        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue

            repo = self.repos[int(self.table.item(row, COL_REPOSITORY).data(Qt.UserRole))]
            repo.selected = True
            self.table.item(row, COL_SELECTED).setCheckState(Qt.Checked)

        self.table.blockSignals(False)

    def clear_selection(self) -> None:
        self.table.blockSignals(True)

        for repo in self.repos:
            repo.selected = False

        for row in range(self.table.rowCount()):
            self.table.item(row, COL_SELECTED).setCheckState(Qt.Unchecked)

        self.table.blockSignals(False)

    def validate_visible(self) -> None:
        if not self.client:
            return

        repos = [self.repos[i] for i in self.visible_repo_indices()]

        # If a strict filter is active before validation, all rows can be hidden
        # because branch/workflow state is still unknown. In that case, validate
        # all repositories rather than doing nothing.
        if not repos and (self.only_branch_checkbox.isChecked() or self.only_workflow_checkbox.isChecked()):
            repos = self.repos

        if not repos:
            self.set_status("No visible repositories.")
            return

        for repo in repos:
            repo.status = "Queued validation"
            self.update_repo_row(repo)

        self.progress_bar.setRange(0, len(repos))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.set_status(f"Validating {len(repos)} repositories...")
        worker = ValidateReposWorker(
            self.config,
            repos,
            self.workflow_edit.text().strip(),
            self.branch_edit.text().strip(),
        )
        worker.repo_updated.connect(self.update_repo_row)
        worker.progress.connect(self.on_validation_progress)
        worker.finished.connect(lambda: (self.progress_bar.setVisible(False), self.mark_stuck_validating_idle(), self.set_status("Validation finished.")))
        self.run_worker(worker)

    def mark_stuck_validating_idle(self) -> None:
        for repo in self.repos:
            if repo.status in {"Validating", "Queued validation"}:
                repo.status = "Ready" if repo.branch_exists is True and (repo.workflow_exists is True or not self.workflow_edit.text().strip()) else "Invalid"
                self.update_repo_row(repo)

    def run_selected(self) -> None:
        if not self.client:
            return

        repos = self.selected_repos()
        if not repos:
            QMessageBox.information(self, "No repositories selected", "Select one or more repositories first.")
            return

        workflow = self.workflow_edit.text().strip()
        if not workflow:
            QMessageBox.warning(self, "Missing workflow", "Please enter a workflow name, path or filename.")
            return

        self.set_status(f"Dispatching {len(repos)} repositories...")
        worker = DispatchReposWorker(
            self.config,
            repos,
            workflow,
            self.branch_edit.text().strip(),
        )
        worker.repo_updated.connect(self.update_repo_row)
        worker.finished.connect(lambda: (self.set_status("Dispatch finished."), self.poll_runs(once=False)))
        self.run_worker(worker)

    def poll_runs(self, once: bool = False) -> None:
        if not self.client:
            return

        # Prefer selected repos. If none are selected, poll visible repos. This allows
        # "Validate visible" + "Poll once" after reopening the app to discover the latest
        # existing CI run, even if the app did not dispatch it.
        tracked = self.selected_repos()
        if not tracked:
            tracked = [self.repos[i] for i in self.visible_repo_indices()]

        if not tracked:
            self.set_status("No repositories to poll.")
            return

        workflow = self.workflow_edit.text().strip()
        if not workflow:
            QMessageBox.warning(self, "Missing workflow", "Please enter a workflow name, path or filename.")
            return

        if self.poll_worker and not once:
            self.set_status("Polling already running.")
            return

        self.set_status(f"Polling CI status for {len(tracked)} repositories...")
        worker = PollRunsWorker(
            self.config,
            tracked,
            workflow,
            self.branch_edit.text().strip(),
            once=once,
            continuous_dashboard=True,
        )
        worker.repo_updated.connect(self.update_repo_row)
        worker.finished.connect(lambda: self.set_status("Polling stopped." if not once else "Poll finished."))

        if once:
            self.run_worker(worker)
            return

        self.poll_worker = worker
        self.poll_thread = QThread(self)
        worker.moveToThread(self.poll_thread)
        self.poll_thread.started.connect(worker.run)
        worker.log.connect(self.log_message)
        worker.error.connect(lambda message: (self.log_message(message), self.set_status(message)))
        worker.finished.connect(self.poll_thread.quit)
        worker.finished.connect(worker.deleteLater)
        self.poll_thread.finished.connect(self.poll_thread.deleteLater)
        self.poll_thread.finished.connect(lambda: setattr(self, "poll_worker", None))
        self.poll_thread.finished.connect(lambda: setattr(self, "poll_thread", None))
        self.poll_thread.start()

    def stop_polling(self) -> None:
        if self.poll_worker:
            self.poll_worker.cancel()
            self.set_status("Stopping polling...")

    def open_selected_run(self) -> None:
        repo = self.current_repo()
        if not repo or not repo.run_url:
            self.set_status("Selected repository has no workflow run URL.")
            return

        webbrowser.open(repo.run_url)

    def stale_check(self) -> None:
        if not self.client:
            return

        self.set_status("Running stale check...")
        worker = StaleCheckWorker(self.config)
        worker.result.connect(lambda text: (self.log_message(text), self.set_status(text)))
        self.run_worker(worker)

    def bundle_data(self) -> dict:
        return {
            "organization": self.organization_edit.text().strip(),
            "workflow": self.workflow_edit.text().strip(),
            "global_branch": self.branch_edit.text().strip(),
            "selected_repositories": [
                {
                    "full_name": repo.full_name,
                    "branch_override": repo.branch_override,
                }
                for repo in self.repos
                if repo.selected
            ],
        }

    def save_bundle(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save repository bundle",
            "repo_bundle.json",
            "JSON files (*.json)",
        )

        if not path:
            return

        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.bundle_data(), f, indent=2)

        self.set_status(f"Saved bundle: {path}")
        self.log_message(f"Saved bundle: {path}")

    def load_bundle(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load repository bundle",
            "",
            "JSON files (*.json)",
        )

        if not path:
            return

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.organization_edit.setText(str(data.get("organization") or self.organization_edit.text()))
        self.workflow_edit.setText(str(data.get("workflow") or self.workflow_edit.text()))
        self.branch_edit.setText(str(data.get("global_branch") or self.branch_edit.text()))

        selected = {
            str(item.get("full_name")): str(item.get("branch_override") or "")
            for item in data.get("selected_repositories", [])
        }

        for repo in self.repos:
            repo.selected = repo.full_name in selected
            if repo.selected:
                repo.branch_override = selected[repo.full_name]

        self.populate_table()
        self.set_status(f"Loaded bundle: {path}")
        self.log_message(f"Loaded bundle: {path}")


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
