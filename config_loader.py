from __future__ import annotations

import os
from pathlib import Path
import yaml

from models import AppConfig, StaleCheckConfig


def load_config(path: str = "config.yaml") -> AppConfig:
    data = {}

    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    stale = data.get("stale_check") or {}

    return AppConfig(
        organization=str(data.get("organization", "") or ""),
        workflow=str(data.get("workflow", "") or ""),
        default_branch=str(data.get("default_branch", "") or ""),
        token=os.environ.get("GITHUB_TOKEN", str(data.get("token", "") or "")),
        repo_type=str(data.get("repo_type", "all") or "all"),
        include_archived=bool(data.get("include_archived", False)),
        poll_interval_seconds=float(data.get("poll_interval_seconds", 5.0)),
        api_concurrency=int(data.get("api_concurrency", 8)),
        stale_check=StaleCheckConfig(
            repository=str(stale.get("repository", "") or ""),
            branch=str(stale.get("branch", "") or ""),
            base_branch=str(stale.get("base_branch", "") or ""),
            workflow=str(stale.get("workflow", "") or ""),
        ),
    )
