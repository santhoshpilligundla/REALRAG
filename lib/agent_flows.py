"""Flow router for the "Live Code + Agents interact" mode.

Reads the per-product dependency graph from data/agent_flows.yaml, classifies a
question to an entry repo node (reusing RealRAG's own hybrid retrieval), walks
the `depends_on` edges to derive the chain + the repos to expose, and returns a
launch plan for lib/claude_runner.py.

The graph is pure data — a new product is a new block in the YAML, no code change.
"""
from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import yaml

from lib.db import get_conn

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "agent_flows.yaml"


@lru_cache
def load_flows_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _product_name_for_id(product_id: UUID | None) -> str | None:
    if product_id is None:
        return None
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM products WHERE product_id = %s", (product_id,))
        row = cur.fetchone()
        return row[0] if row else None


def resolve_product(product_id: UUID | None) -> tuple[str, dict]:
    """Map a RealRAG product_id (UUID, or None) to a (name, config) pair."""
    cfg = load_flows_config()
    products = cfg.get("products", {})
    name = _product_name_for_id(product_id) or cfg.get("default_product")
    if name not in products:
        name = cfg.get("default_product")
    return name, products.get(name, {})


def _repo_ids_for_product(product_id: UUID | None) -> list[UUID]:
    if product_id is None:
        sql, params = "SELECT repo_id FROM repos", ()
    else:
        sql, params = "SELECT repo_id FROM repos WHERE product_id = %s", (product_id,)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [r[0] for r in cur.fetchall()]


def classify_entry_node(question: str, product_id: UUID | None = None) -> tuple[str, dict]:
    """Pick the entry repo node for a question.

    Primary signal: the dominant repo among RealRAG's top retrieval hits (rank-
    weighted). Secondary nudge: per-node `route_keywords` in the config. Falls
    back to the product's `default_entry_node`.

    Returns (entry_node_name, debug_info).
    """
    pname, pcfg = resolve_product(product_id)
    nodes = pcfg.get("nodes", {})
    repo_aliases = pcfg.get("repo_aliases", {})
    tally: Counter = Counter()
    debug: dict = {"product": pname, "retrieval_repos": {}, "keyword_hits": {}}

    # 1) retrieval-based repo signal (reuse the same embed + retrieve path as chat)
    try:
        from lib.embedder import embed_texts
        from lib.retrieval import retrieve
        matrix, _ = embed_texts([question])
        qvec = matrix[0] if getattr(matrix, "size", 0) else None
        hits = retrieve(
            query=question, rewrite=question, hyde=None, structural_targets=[],
            repo_ids=_repo_ids_for_product(product_id), product_id=product_id,
            top_k=8, rerank=False, query_vec=qvec,
        )
        n = len(hits)
        for i, h in enumerate(hits):
            node = repo_aliases.get(getattr(h, "repo_name", None))
            if node in nodes:
                tally[node] += (n - i)  # earlier hits weigh more
        debug["retrieval_repos"] = dict(Counter(
            getattr(h, "repo_name", "?") for h in hits))
    except Exception as e:  # retrieval must never break routing
        debug["retrieval_error"] = f"{type(e).__name__}: {e}"

    # 2) keyword nudge from config (helps thinly-indexed flows like reports)
    q = question.lower()
    for node, ncfg in nodes.items():
        kws = [k.lower() for k in (ncfg.get("route_keywords") or [])]
        hit_kws = [k for k in kws if re.search(rf"\b{re.escape(k)}\b", q)]
        if hit_kws:
            tally[node] += 2 * len(hit_kws)
            debug["keyword_hits"][node] = hit_kws

    entry = tally.most_common(1)[0][0] if tally else pcfg.get("default_entry_node")
    if entry not in nodes:
        entry = next(iter(nodes), None)
    debug["entry"] = entry
    debug["tally"] = dict(tally)
    return entry, debug


def derive_chain(pcfg: dict, entry: str) -> list[str]:
    """Walk depends_on from the entry node to the leaf (linear graph)."""
    nodes = pcfg.get("nodes", {})
    chain: list[str] = []
    seen: set[str] = set()
    cur = entry
    while cur and cur in nodes and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        deps = nodes[cur].get("depends_on") or []
        cur = deps[0] if deps else None
    return chain


def make_db_connection(server: str | None, database: str | None,
                       db_type: str | None = "postgres") -> str:
    """Build the db_connection value the agents expect, or 'unavailable'.

    The user enters the server/database directly in the UI; PO = postgres,
    YSMaster = sqlserver. When either is blank, live DB steps are skipped.
    """
    server = (server or "").strip()
    database = (database or "").strip()
    if not server or not database:
        return "unavailable"
    dbt = (db_type or "postgres").strip() or "postgres"
    return f'{{"server": "{server}", "database": "{database}", "db_type": "{dbt}"}}'


def build_launch_plan(question: str, product_id: UUID | None = None,
                      *, server: str | None = None, database: str | None = None,
                      db_type: str | None = "postgres") -> dict:
    """Resolve a question to a headless-launch plan for lib/claude_runner.py.

    Optional server/database (entered by the user) enable live DB verification;
    when absent, the run proceeds with live DB steps skipped (never blocks).
    """
    pname, pcfg = resolve_product(product_id)
    nodes = pcfg.get("nodes", {})
    if not nodes:
        raise ValueError(f"No agent-flow nodes configured for product {pname!r}")

    entry, debug = classify_entry_node(question, product_id)
    entry_cfg = nodes[entry]
    chain = derive_chain(pcfg, entry)

    launch_mode = entry_cfg.get("launch", "direct")
    agent = pcfg.get("orchestrator") if launch_mode == "orchestrator" else entry_cfg.get("entry_agent")

    # Expose every downstream repo in the chain, plus any explicit extras.
    add_dirs: list[str] = []
    for n in chain[1:]:
        d = nodes[n].get("cwd")
        if d and d not in add_dirs:
            add_dirs.append(d)
    for d in (entry_cfg.get("extra_add_dirs") or []):
        if d not in add_dirs:
            add_dirs.append(d)

    # Live DB target is entered by the user in the UI. When blank, "unavailable"
    # → the run skips live DB steps and never blocks asking for it.
    db_connection = make_db_connection(server, database, db_type)

    return {
        "product": pname,
        "entry": entry,
        "chain": chain,
        "cwd": entry_cfg["cwd"],
        "agent": agent,
        "launch_mode": launch_mode,
        "agent_sequence": entry_cfg.get("agent_sequence") or [],
        "add_dirs": add_dirs,
        "db_connection": db_connection,
        "debug": debug,
    }
