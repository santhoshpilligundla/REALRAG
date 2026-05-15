from __future__ import annotations

from uuid import UUID

from psycopg.rows import dict_row

from lib.db import get_conn
from lib.models import Product, Repo, RepoOnboardingRequest


def list_products() -> list[Product]:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT product_id, name, created_at FROM products ORDER BY name")
        return [Product(**row) for row in cur.fetchall()]


def list_repos(product_id: UUID | None = None) -> list[Repo]:
    sql = """
        SELECT repo_id, product_id, tfs_url, branch, sub_path,
               display_name, one_line_description, repo_role,
               major_workflows, key_business_concepts, critical_entry_points,
               priority, languages, owner_team, owner_contact, related_repos,
               special_notes, clone_depth, enable_lfs,
               last_indexed_sha, status, error_message, clone_path,
               created_at, updated_at
        FROM repos
    """
    params: tuple = ()
    if product_id is not None:
        sql += " WHERE product_id = %s"
        params = (product_id,)
    sql += " ORDER BY created_at DESC"

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [Repo(**row) for row in cur.fetchall()]


def insert_repo(req: RepoOnboardingRequest) -> UUID:
    pat_value = req.pat.get_secret_value() if req.pat else None
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO repos (
                product_id, tfs_url, branch, sub_path, pat_secret,
                display_name, one_line_description, repo_role,
                major_workflows, key_business_concepts, critical_entry_points,
                priority, languages, owner_team, owner_contact, related_repos,
                special_notes, clone_depth, enable_lfs
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s
            )
            RETURNING repo_id
            """,
            (
                req.product_id,
                str(req.tfs_url),
                req.branch,
                req.sub_path,
                pat_value,
                req.display_name,
                req.one_line_description,
                req.repo_role,
                req.major_workflows,
                req.key_business_concepts,
                req.critical_entry_points,
                req.priority,
                req.languages,
                req.owner_team,
                req.owner_contact,
                req.related_repos,
                req.special_notes,
                req.clone_depth,
                req.enable_lfs,
            ),
        )
        repo_id = cur.fetchone()[0]
        conn.commit()
        return repo_id


def delete_repo(repo_id: UUID) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM repos WHERE repo_id = %s", (repo_id,))
        conn.commit()


def get_repo_by_id(repo_id: UUID) -> Repo | None:
    sql = """
        SELECT repo_id, product_id, tfs_url, branch, sub_path,
               display_name, one_line_description, repo_role,
               major_workflows, key_business_concepts, critical_entry_points,
               priority, languages, owner_team, owner_contact, related_repos,
               special_notes, clone_depth, enable_lfs,
               last_indexed_sha, status, error_message, clone_path,
               created_at, updated_at
        FROM repos
        WHERE repo_id = %s
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (repo_id,))
        row = cur.fetchone()
        return Repo(**row) if row else None


def get_repo_counts(repo_id: UUID) -> dict[str, int]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM repo_files  WHERE repo_id = %s),
              (SELECT COUNT(*) FROM repo_files  WHERE repo_id = %s AND parsed),
              (SELECT COUNT(*) FROM entities    WHERE repo_id = %s),
              (SELECT COUNT(*) FROM code_chunks WHERE repo_id = %s)
            """,
            (repo_id, repo_id, repo_id, repo_id),
        )
        files, parsed_files, entities, chunks = cur.fetchone()
        return {
            "files": files,
            "parsed_files": parsed_files,
            "entities": entities,
            "chunks": chunks,
        }


def get_counts_by_repo() -> dict[UUID, dict[str, int]]:
    sql = """
        SELECT r.repo_id,
               COALESCE(f.cnt, 0) AS files,
               COALESCE(f.parsed, 0) AS parsed_files,
               COALESCE(e.cnt, 0) AS entities,
               COALESCE(c.cnt, 0) AS chunks
          FROM repos r
          LEFT JOIN (
              SELECT repo_id,
                     COUNT(*) AS cnt,
                     COUNT(*) FILTER (WHERE parsed) AS parsed
                FROM repo_files GROUP BY repo_id
          ) f USING (repo_id)
          LEFT JOIN (
              SELECT repo_id, COUNT(*) AS cnt FROM entities GROUP BY repo_id
          ) e USING (repo_id)
          LEFT JOIN (
              SELECT repo_id, COUNT(*) AS cnt FROM code_chunks GROUP BY repo_id
          ) c USING (repo_id)
    """
    out: dict[UUID, dict[str, int]] = {}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        for repo_id, files, parsed_files, entities, chunks in cur.fetchall():
            out[repo_id] = {
                "files": files,
                "parsed_files": parsed_files,
                "entities": entities,
                "chunks": chunks,
            }
    return out


def get_pat(repo_id: UUID) -> str | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT pat_secret FROM repos WHERE repo_id = %s", (repo_id,))
        row = cur.fetchone()
        return row[0] if row else None


def set_repo_status(
    repo_id: UUID,
    status: str,
    *,
    error_message: str | None = None,
    last_indexed_sha: str | None = None,
    clone_path: str | None = None,
) -> None:
    sets = ["status = %s", "updated_at = now()"]
    params: list = [status]
    if error_message is not None:
        sets.append("error_message = %s")
        params.append(error_message)
    elif status != "error":
        sets.append("error_message = NULL")
    if last_indexed_sha is not None:
        sets.append("last_indexed_sha = %s")
        params.append(last_indexed_sha)
    if clone_path is not None:
        sets.append("clone_path = %s")
        params.append(clone_path)
    params.append(repo_id)

    sql = f"UPDATE repos SET {', '.join(sets)} WHERE repo_id = %s"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        conn.commit()


def bulk_insert_repos(reqs: list[RepoOnboardingRequest]) -> list[UUID]:
    if not reqs:
        return []
    inserted: list[UUID] = []
    with get_conn() as conn, conn.cursor() as cur:
        for req in reqs:
            pat_value = req.pat.get_secret_value() if req.pat else None
            cur.execute(
                """
                INSERT INTO repos (
                    product_id, tfs_url, branch, sub_path, pat_secret,
                    display_name, one_line_description, repo_role,
                    major_workflows, key_business_concepts, critical_entry_points,
                    priority, languages, owner_team, owner_contact, related_repos,
                    special_notes, clone_depth, enable_lfs
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                RETURNING repo_id
                """,
                (
                    req.product_id,
                    str(req.tfs_url),
                    req.branch,
                    req.sub_path,
                    pat_value,
                    req.display_name,
                    req.one_line_description,
                    req.repo_role,
                    req.major_workflows,
                    req.key_business_concepts,
                    req.critical_entry_points,
                    req.priority,
                    req.languages,
                    req.owner_team,
                    req.owner_contact,
                    req.related_repos,
                    req.special_notes,
                    req.clone_depth,
                    req.enable_lfs,
                ),
            )
            inserted.append(cur.fetchone()[0])
        conn.commit()
    return inserted
