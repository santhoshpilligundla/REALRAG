from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, SecretStr


RepoRole = Literal["UI", "API", "ETL", "Reports", "Config", "Other"]
Priority = Literal["P0", "P1", "P2", "P3"]
CloneDepth = Literal["full", "shallow_50", "shallow_100"]
RepoStatus = Literal[
    "registered", "cloning", "cloned", "parsing", "chunking",
    "embedding", "indexing", "ready", "error", "disabled",
]


class RepoOnboardingRequest(BaseModel):
    product_id: UUID
    display_name: str = Field(..., min_length=2, max_length=80)

    tfs_url: HttpUrl
    pat: SecretStr | None = None
    branch: str = "master"
    sub_path: str | None = None

    one_line_description: str = Field(..., min_length=20, max_length=200)
    repo_role: RepoRole
    major_workflows: list[str] = Field(..., min_length=1)
    key_business_concepts: list[str] = Field(..., min_length=1)
    critical_entry_points: list[str] = Field(default_factory=list)

    priority: Priority = "P1"
    languages: list[str] = Field(default_factory=list)

    owner_team: str = Field(..., min_length=1)
    owner_contact: str = Field(..., min_length=1)
    related_repos: list[UUID] = Field(default_factory=list)
    special_notes: str | None = None

    clone_depth: CloneDepth = "shallow_50"
    enable_lfs: bool = False


class Product(BaseModel):
    product_id: UUID
    name: str
    created_at: datetime


class Repo(BaseModel):
    repo_id: UUID
    product_id: UUID
    tfs_url: str
    branch: str
    sub_path: str | None
    display_name: str
    one_line_description: str
    repo_role: RepoRole
    major_workflows: list[str]
    key_business_concepts: list[str]
    critical_entry_points: list[str]
    priority: Priority
    languages: list[str]
    owner_team: str
    owner_contact: str
    related_repos: list[UUID]
    special_notes: str | None
    clone_depth: CloneDepth
    enable_lfs: bool
    last_indexed_sha: str | None
    status: RepoStatus
    error_message: str | None = None
    clone_path: str | None = None
    created_at: datetime
    updated_at: datetime
