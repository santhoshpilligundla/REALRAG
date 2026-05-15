"""File discovery + classification for cloned repos."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SKIP_DIRS = {
    ".git", ".svn", ".hg",
    "node_modules", "bower_components", "jspm_packages",
    "target", "build", "dist", "out", "bin", "obj",
    ".gradle", ".mvn", ".idea", ".vscode", ".settings",
    ".angular", ".history", ".nyc_output", "__pycache__",
    "coverage", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "venv", ".venv", "env", ".env",
}


SKIP_EXTS = {
    ".class", ".jar", ".war", ".ear", ".pyc", ".pyo",
    ".min.js", ".min.css", ".map",
    ".lock",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv",
    ".dll", ".exe", ".so", ".dylib",
}


LANGUAGE_BY_EXT: dict[str, str] = {
    ".java":   "java",
    ".kt":     "kotlin",
    ".scala":  "scala",
    ".groovy": "groovy",
    ".ts":     "typescript",
    ".tsx":    "typescript",
    ".js":     "javascript",
    ".jsx":    "javascript",
    ".html":   "html",
    ".scss":   "scss",
    ".css":    "css",
    ".xml":    "xml",
    ".jrxml":  "xml",
    ".sql":    "sql",
    ".py":     "python",
    ".cs":     "csharp",
    ".go":     "go",
    ".rb":     "ruby",
    ".md":     "markdown",
    ".txt":    "text",
    ".yaml":   "yaml",
    ".yml":    "yaml",
    ".json":   "json",
    ".properties": "properties",
    ".sh":     "shell",
    ".ps1":    "powershell",
    ".r":      "r",
}


MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB


@dataclass
class FileInfo:
    rel_path: str
    abs_path: Path
    language: str
    size_bytes: int


def _classify(path: Path) -> str:
    ext = path.suffix.lower()
    if path.name == "Dockerfile":
        return "dockerfile"
    return LANGUAGE_BY_EXT.get(ext, "other")


def walk_repo(clone_path: Path, special_notes: str | None = None) -> Iterator[FileInfo]:
    """Yield FileInfo for every relevant file under clone_path."""
    excluded_segments = _parse_exclusions(special_notes)

    for path in clone_path.rglob("*"):
        if not path.is_file():
            continue

        parts = set(path.relative_to(clone_path).parts)
        if parts & SKIP_DIRS:
            continue
        if any(seg in excluded_segments for seg in parts):
            continue

        suffix = path.suffix.lower()
        if suffix in SKIP_EXTS:
            continue

        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0 or size > MAX_FILE_BYTES:
            continue

        rel = path.relative_to(clone_path).as_posix()
        yield FileInfo(
            rel_path=rel,
            abs_path=path,
            language=_classify(path),
            size_bytes=size,
        )


def _parse_exclusions(special_notes: str | None) -> set[str]:
    if not special_notes:
        return set()
    excluded: set[str] = set()
    lower = special_notes.lower()
    if "rmsws-webapp" in lower:
        excluded.add("rmsws-webapp")
    return excluded
