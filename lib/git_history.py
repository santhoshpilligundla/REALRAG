"""Git-history fetcher for L4 enrichment (bible: 'last meaningful commit per entity').

Uses `git log -L start,end:file` (with --no-patch, --pretty) to fetch the
recent commits affecting an entity's line range.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommitInfo:
    sha: str
    author: str
    author_email: str
    date: str       # ISO 8601
    subject: str
    body: str = ""


_LOG_FORMAT = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b%x1e"


def fetch_history_for_range(
    repo_path: Path,
    file_path: str,
    start_line: int,
    end_line: int,
    limit: int = 5,
) -> list[CommitInfo]:
    """Run `git log -L start,end:file --pretty=...` and parse the result.

    Returns up to `limit` recent commits. Empty list on failure.
    """
    if start_line < 1:
        start_line = 1
    if end_line < start_line:
        end_line = start_line

    cmd = [
        "git", "log",
        f"-L", f"{start_line},{end_line}:{file_path}",
        "--no-patch",
        f"--pretty=format:{_LOG_FORMAT}",
        f"-n", str(limit),
    ]

    try:
        proc = subprocess.run(
            cmd, cwd=str(repo_path),
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if proc.returncode != 0:
        return []

    out: list[CommitInfo] = []
    for raw_record in proc.stdout.split("\x1e"):
        record = raw_record.strip()
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 5:
            continue
        sha, author, email, date, subject, *rest = parts
        body = "\n".join(rest).strip() if rest else ""
        out.append(CommitInfo(
            sha=sha[:40],
            author=author,
            author_email=email,
            date=date,
            subject=subject.strip(),
            body=body[:500],
        ))
    return out


def commits_to_jsonable(commits: list[CommitInfo]) -> list[dict]:
    return [
        {
            "sha": c.sha[:8],
            "author": c.author,
            "date": c.date,
            "subject": c.subject,
        }
        for c in commits
    ]
