from __future__ import annotations

import base64
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lib.models import Repo
from lib.repos_repo import get_pat, set_repo_status


CLONE_ROOT = Path("storage/repos")
CLONE_TIMEOUT_S = 600


@dataclass
class CloneResult:
    repo_id: str
    display_name: str
    success: bool
    dest: Path
    sha: str | None = None
    message: str = ""


def _depth_arg(clone_depth: str) -> list[str]:
    if clone_depth == "full":
        return []
    if clone_depth == "shallow_100":
        return ["--depth=100"]
    return ["--depth=50"]


def _basic_auth_header(pat: str) -> str:
    token = base64.b64encode(f":{pat}".encode()).decode()
    return f"Authorization: Basic {token}"


def _git(args: list[str], cwd: Path | None = None, timeout: int = CLONE_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _head_sha(repo_dir: Path) -> str | None:
    proc = _git(["rev-parse", "HEAD"], cwd=repo_dir, timeout=30)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def clone_dest(repo: Repo, product_name: str) -> Path:
    return CLONE_ROOT / product_name / repo.display_name / repo.branch


def clone_repo(repo: Repo, product_name: str) -> CloneResult:
    set_repo_status(repo.repo_id, "cloning")

    dest = clone_dest(repo, product_name)
    if (dest / ".git").exists():
        sha = _head_sha(dest)
        set_repo_status(
            repo.repo_id,
            "cloned",
            last_indexed_sha=sha,
            clone_path=str(dest),
        )
        return CloneResult(
            repo_id=str(repo.repo_id),
            display_name=repo.display_name,
            success=True,
            dest=dest,
            sha=sha,
            message="already cloned (skipped)",
        )

    pat = get_pat(repo.repo_id)
    extra_args: list[str] = []
    if pat:
        extra_args = ["-c", f"http.extraHeader={_basic_auth_header(pat)}"]

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    cmd = [
        *extra_args,
        "clone",
        "--filter=blob:none",
        *_depth_arg(repo.clone_depth),
        "--single-branch",
        "--branch", repo.branch,
        "--", repo.tfs_url, str(dest),
    ]

    try:
        proc = _git(cmd)
    except subprocess.TimeoutExpired:
        msg = f"git clone timed out after {CLONE_TIMEOUT_S}s"
        set_repo_status(repo.repo_id, "error", error_message=msg)
        return CloneResult(
            repo_id=str(repo.repo_id),
            display_name=repo.display_name,
            success=False,
            dest=dest,
            message=msg,
        )

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "git clone failed").strip()
        if pat and pat in err:
            err = err.replace(pat, "***")
        set_repo_status(repo.repo_id, "error", error_message=err)
        return CloneResult(
            repo_id=str(repo.repo_id),
            display_name=repo.display_name,
            success=False,
            dest=dest,
            message=err,
        )

    sha = _head_sha(dest)
    set_repo_status(
        repo.repo_id,
        "cloned",
        last_indexed_sha=sha,
        clone_path=str(dest),
    )
    return CloneResult(
        repo_id=str(repo.repo_id),
        display_name=repo.display_name,
        success=True,
        dest=dest,
        sha=sha,
        message=(proc.stdout or "").strip(),
    )
