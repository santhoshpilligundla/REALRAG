from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

import streamlit as st
import streamlit.components.v1 as components
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.chat import chat as run_chat  # noqa: E402
from lib.chat import (  # noqa: E402
    answer_has_fabrication,
    answer_looks_unsure,
    context_covers_subjects,
    prepare_answer,
    stream_answer,
)
from lib.query_cache import cache_get, cache_put  # noqa: E402
from lib.agent import run_agent  # noqa: E402
from lib.agent_flows import build_launch_plan  # noqa: E402
from lib.claude_runner import run_flow  # noqa: E402

# Query cache OFF during active iteration so pipeline fixes aren't masked by stale
# cached answers. Set True once behavior is stable for the speed-on-repeats win.
_CACHE_ON = False
from lib.chunker import chunk_repo  # noqa: E402
from lib.clone import clone_repo  # noqa: E402
from lib.coverage import (  # noqa: E402
    entity_gaps,
    list_open_failed_questions,
    repo_coverage,
)
from lib.cross_repo import count_edges_by_kind, discover_edges  # noqa: E402
from lib.db import apply_schema  # noqa: E402
from lib.doc_gen import count_pending_docs, doc_gen_repo, doc_gen_all_repos_parallel  # noqa: E402
from lib.doc_gen_l4_enrich import count_pending_l4_enrich, enrich_l4_repo  # noqa: E402
from lib.doc_gen_pass2 import count_pending_pass2, doc_gen_pass2_repo  # noqa: E402
from lib.doc_gen_pass3 import count_pending_pass3, doc_gen_pass3  # noqa: E402
from lib.embed_pipeline import count_pending_embeds, embed_repo  # noqa: E402
from lib.models import RepoOnboardingRequest  # noqa: E402
from lib.presets import RMS_PRESETS, RepoPreset  # noqa: E402
from lib.repos_repo import (  # noqa: E402
    bulk_insert_repos,
    delete_repo,
    get_counts_by_repo,
    insert_repo,
    list_products,
    list_repos,
)
from lib.runs_repo import list_recent_runs, list_running  # noqa: E402
from lib.sessions_repo import (  # noqa: E402
    create_session,
    latest_session,
    load_messages,
    save_message,
)


st.set_page_config(page_title="RealRAG — Config", layout="wide")


@st.cache_resource
def _bootstrap() -> bool:
    apply_schema()
    return True


_bootstrap()


def _split_lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _preset_to_request(
    preset: RepoPreset, product_id: UUID, tfs_url: str, pat: str
) -> RepoOnboardingRequest:
    return RepoOnboardingRequest(
        product_id=product_id,
        display_name=preset["display_name"],
        tfs_url=tfs_url,
        pat=pat or None,
        branch=preset["branch"],
        sub_path=None,
        one_line_description=preset["one_line_description"],
        repo_role=preset["repo_role"],
        major_workflows=preset["major_workflows"],
        key_business_concepts=preset["key_business_concepts"],
        critical_entry_points=preset["critical_entry_points"],
        priority=preset["priority"],
        languages=preset["languages"],
        owner_team=preset["owner_team"],
        owner_contact=preset["owner_contact"],
        related_repos=[],
        special_notes=preset["special_notes"] or None,
        clone_depth=preset["clone_depth"],
        enable_lfs=False,
    )


def _render_bulk_register(product_by_label: dict, registered_display_names: set[str]) -> None:
    st.subheader("Bulk register — RMS repos")
    st.caption(
        "Shared PAT used for all 5 repos. Metadata is pre-filled from "
        "RealRAG-scope.html. Leave a URL blank to skip that repo."
    )

    rms_product = product_by_label.get("RMS")
    if not rms_product:
        st.error("Product 'RMS' not found in DB. Re-run `python scripts/dev_up.py`.")
        return

    shared_pat = st.text_input(
        "Shared TFS PAT", type="password", key="bulk_pat",
        help="Used as the PAT for every repo registered below.",
    )

    url_inputs: dict[str, str] = {}
    for key, preset in RMS_PRESETS.items():
        is_registered = key in registered_display_names
        with st.container(border=True):
            c1, c2 = st.columns([2, 3])
            with c1:
                badge = " ✓ registered" if is_registered else ""
                st.markdown(f"**{key}**{badge}")
                st.caption(
                    f"{preset['repo_role']} · {preset['priority']} · {preset['owner_team']}"
                )
                with st.expander("Preview metadata"):
                    st.write(preset["one_line_description"])
                    st.markdown("**Workflows:** " + ", ".join(preset["major_workflows"]))
                    st.markdown(
                        "**Concepts:** " + ", ".join(preset["key_business_concepts"])
                    )
                    if preset["critical_entry_points"]:
                        st.markdown(
                            "**Entry points:** "
                            + ", ".join(preset["critical_entry_points"])
                        )
                    if preset["special_notes"]:
                        st.markdown(f"**Notes:** {preset['special_notes']}")
            with c2:
                url_inputs[key] = st.text_input(
                    f"TFS URL for {key}",
                    key=f"bulk_url_{key}",
                    placeholder=f"https://tfs.realpage.com/.../{key}.git",
                    disabled=is_registered,
                    label_visibility="collapsed",
                )

    if not st.button("Register all", type="primary"):
        return

    candidates = [
        (key, url_inputs[key].strip())
        for key in RMS_PRESETS
        if key not in registered_display_names and url_inputs[key].strip()
    ]

    if not candidates:
        st.warning("No URLs entered for any unregistered repo.")
        return

    if not shared_pat:
        st.error("Provide a shared PAT.")
        return

    requests: list[RepoOnboardingRequest] = []
    errors: list[str] = []
    for key, url in candidates:
        try:
            requests.append(
                _preset_to_request(RMS_PRESETS[key], rms_product.product_id, url, shared_pat)
            )
        except ValidationError as e:
            for err in e.errors():
                loc = ".".join(str(x) for x in err["loc"])
                errors.append(f"{key}: {loc} — {err['msg']}")

    if errors:
        st.error("Validation failed:")
        for line in errors:
            st.write(f"• {line}")
        return

    try:
        inserted_ids = bulk_insert_repos(requests)
    except Exception as e:
        st.error(f"Insert failed (transaction rolled back): {e}")
        return

    st.success(f"Registered {len(inserted_ids)} repos.")
    for req, repo_id in zip(requests, inserted_ids):
        st.write(f"  • **{req.display_name}** → `{repo_id}`")
    st.session_state.pop("bulk_pat", None)
    for key in RMS_PRESETS:
        st.session_state.pop(f"bulk_url_{key}", None)


# ----- Manual single-repo form (for non-RMS / one-offs) ----------------------

FORM_KEYS = [
    "form_product",
    "form_display_name",
    "form_tfs_url",
    "form_pat",
    "form_branch",
    "form_sub_path",
    "form_repo_role",
    "form_priority",
    "form_one_line",
    "form_workflows",
    "form_concepts",
    "form_entry_points",
    "form_languages",
    "form_owner_team",
    "form_owner_contact",
    "form_special_notes",
    "form_clone_depth",
    "form_enable_lfs",
]


def _clear_form() -> None:
    for k in FORM_KEYS:
        st.session_state.pop(k, None)


def _render_manual_form(product_options: list[str], product_by_label: dict) -> None:
    with st.form("add_repo_form", clear_on_submit=False):
        col_l, col_r = st.columns([1, 1])

        with col_l:
            st.subheader("Identity")
            st.selectbox("Product", product_options, key="form_product")
            st.text_input(
                "Display name",
                key="form_display_name",
                placeholder="my-repo",
            )

            st.subheader("Source")
            st.text_input("TFS URL", key="form_tfs_url")
            st.text_input("PAT", type="password", key="form_pat")
            st.text_input("Branch", key="form_branch", placeholder="master")
            st.text_input("Sub-path (optional)", key="form_sub_path")

            st.subheader("Classification")
            st.radio(
                "Repo role",
                ["UI", "API", "ETL", "Reports", "Config", "Other"],
                key="form_repo_role",
                horizontal=True,
            )
            st.radio(
                "Indexing priority",
                ["P0", "P1", "P2", "P3"],
                key="form_priority",
                horizontal=True,
            )

        with col_r:
            st.subheader("Overview (load-bearing)")
            st.text_area(
                "One-line description",
                key="form_one_line",
                height=80,
            )
            st.text_area(
                "Major workflows / features (one per line)",
                key="form_workflows",
                height=120,
            )
            st.text_area(
                "Key business concepts owned (one per line)",
                key="form_concepts",
                height=110,
            )
            st.text_area(
                "Critical entry points (one per line)",
                key="form_entry_points",
                height=90,
            )

            st.subheader("Ownership")
            st.text_input("Owner team", key="form_owner_team")
            st.text_input("Owner contact", key="form_owner_contact")
            st.text_area(
                "Special notes / exclusions",
                key="form_special_notes",
                height=70,
            )

        with st.expander("Advanced"):
            st.text_input(
                "Languages (comma-separated, blank = auto-detect)",
                key="form_languages",
            )
            st.radio(
                "Clone depth",
                ["full", "shallow_50", "shallow_100"],
                key="form_clone_depth",
                horizontal=True,
            )
            st.checkbox("Enable Git LFS", key="form_enable_lfs", value=False)

        c_a, c_b = st.columns([1, 6])
        with c_a:
            submitted = st.form_submit_button("Add & Register", type="primary")
        with c_b:
            cleared = st.form_submit_button("Clear form")

    if cleared:
        _clear_form()
        st.rerun()

    if not submitted:
        return

    languages_raw = st.session_state.get("form_languages", "") or ""
    languages = [s.strip() for s in languages_raw.split(",") if s.strip()]

    product_label = st.session_state.get("form_product")
    if not product_label:
        st.error("Pick a product.")
        return

    try:
        req = RepoOnboardingRequest(
            product_id=product_by_label[product_label].product_id,
            display_name=(st.session_state.get("form_display_name") or "").strip(),
            tfs_url=(st.session_state.get("form_tfs_url") or "").strip(),
            pat=st.session_state.get("form_pat") or None,
            branch=(st.session_state.get("form_branch") or "master").strip() or "master",
            sub_path=(st.session_state.get("form_sub_path") or "").strip() or None,
            one_line_description=(st.session_state.get("form_one_line") or "").strip(),
            repo_role=st.session_state.get("form_repo_role"),
            major_workflows=_split_lines(st.session_state.get("form_workflows") or ""),
            key_business_concepts=_split_lines(st.session_state.get("form_concepts") or ""),
            critical_entry_points=_split_lines(st.session_state.get("form_entry_points") or ""),
            priority=st.session_state.get("form_priority"),
            languages=languages,
            owner_team=(st.session_state.get("form_owner_team") or "").strip(),
            owner_contact=(st.session_state.get("form_owner_contact") or "").strip(),
            special_notes=(st.session_state.get("form_special_notes") or "").strip() or None,
            clone_depth=st.session_state.get("form_clone_depth") or "shallow_50",
            enable_lfs=bool(st.session_state.get("form_enable_lfs")),
        )
    except ValidationError as e:
        st.error("Validation failed:")
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            st.write(f"• **{loc}** — {err['msg']}")
        return

    try:
        repo_id = insert_repo(req)
    except Exception as e:
        st.error(f"Insert failed: {e}")
        return

    st.success(f"Registered: {req.display_name}  ·  repo_id = {repo_id}")
    _clear_form()


def _render_add_repo_tab() -> None:
    products = list_products()
    if not products:
        st.error("No products in database. Re-run `python scripts/dev_up.py`.")
        return

    product_by_label = {p.name: p for p in products}
    product_options = list(product_by_label.keys())

    registered = list_repos()
    registered_display_names = {r.display_name for r in registered}

    _render_bulk_register(product_by_label, registered_display_names)

    st.divider()
    with st.expander("Register a custom repo (manual form)"):
        _render_manual_form(product_options, product_by_label)


_STATUS_BADGE = {
    "registered": "🆕",
    "cloning": "⏳",
    "cloned": "✅",
    "parsing": "⏳",
    "chunking": "⏳",
    "embedding": "⏳",
    "indexing": "⏳",
    "ready": "🟢",
    "error": "❌",
    "disabled": "🚫",
}


def _render_registered_repos_tab() -> None:
    products = {p.product_id: p for p in list_products()}
    repos = list_repos()

    if not repos:
        st.info("No repos registered yet.")
        return

    counts_by_repo = get_counts_by_repo()
    pending_clone = [r for r in repos if r.status in ("registered", "error")]
    parseable = [r for r in repos if r.status in ("cloned", "ready", "error")
                 and r.clone_path]

    docgen_eligible = [r for r in repos if r.clone_path]
    pending_docs_per_repo = {
        r.repo_id: count_pending_docs(r.repo_id, "meaningful", r.critical_entry_points)
        for r in docgen_eligible
    }
    pending_docs_total = sum(pending_docs_per_repo.values())

    embed_eligible = [r for r in repos if r.clone_path]
    pending_embeds_per_repo = {
        r.repo_id: count_pending_embeds(r.repo_id) for r in embed_eligible
    }
    pending_embeds_total = sum(
        v["code_chunks"] + v["generated_docs"] for v in pending_embeds_per_repo.values()
    )

    pending_pass3 = count_pending_pass3()

    top_l, top_clone, top_parse, top_doc, top_pardoc, top_edges, top_pass3, top_embed = st.columns([3, 1, 1, 1, 1, 1, 1, 1])
    with top_l:
        st.write(
            f"**{len(repos)} repos**  ·  "
            f"{len(pending_clone)} need clone  ·  "
            f"{len(parseable)} parseable  ·  "
            f"{pending_docs_total:,} pending docs  ·  "
            f"{pending_embeds_total:,} pending embeds"
        )
    with top_clone:
        if pending_clone and st.button(
            f"Clone all ({len(pending_clone)})", key="clone_all_btn"
        ):
            with st.status(f"Cloning {len(pending_clone)} repos…", expanded=True) as s:
                for r in pending_clone:
                    pname = products[r.product_id].name if r.product_id in products else "?"
                    st.write(f"→ {r.display_name}")
                    result = clone_repo(r, pname)
                    if result.success:
                        st.write(f"  ✅ sha=`{(result.sha or '?')[:8]}`")
                    else:
                        st.write(f"  ❌ {result.message[:200]}")
                s.update(label="Clone batch done", state="complete")
            st.rerun()
    with top_parse:
        if parseable and st.button(
            f"Parse all ({len(parseable)})", key="parse_all_btn"
        ):
            for r in parseable:
                with st.status(f"Parsing {r.display_name}…", expanded=True) as s:
                    progress = st.empty()

                    def _on_progress(msg: str, _ph=progress) -> None:
                        _ph.write(msg)

                    result = chunk_repo(r, on_progress=_on_progress)
                    if result.success:
                        s.update(
                            label=(
                                f"{r.display_name} done — "
                                f"files={result.files_seen}, parsed={result.files_parsed}, "
                                f"entities={result.entities}, chunks={result.chunks}"
                            ),
                            state="complete",
                        )
                    else:
                        s.update(label=f"{r.display_name} failed: {result.error}", state="error")
            st.rerun()
    with top_doc:
        if pending_docs_total and st.button(
            f"Doc-gen all repos ({pending_docs_total:,})",
            key="docgen_all_btn",
            type="primary",
            help="Run doc-gen at 'meaningful' scope across every repo with pending docs.",
        ):
            for r in docgen_eligible:
                if pending_docs_per_repo.get(r.repo_id, 0) == 0:
                    continue
                _run_docgen(r, "meaningful")
            st.rerun()
    with top_pardoc:
        if pending_docs_total and st.button(
            f"⚡ Parallel doc-gen ({pending_docs_total:,})",
            key="pardocgen_all_btn",
            type="primary",
            help="Run doc-gen on ALL repos simultaneously with batch verification. ~12hrs for full codebase.",
        ):
            messages = []
            def _par_progress(repo, msg):
                messages.append(f"[{repo}] {msg}")
            with st.spinner("Running parallel doc-gen across all repos…"):
                results = doc_gen_all_repos_parallel(
                    docgen_eligible, "meaningful",
                    on_progress=_par_progress,
                    run_verifier=True,
                )
            for repo_name, r in results.items():
                if r.success:
                    st.success(f"{repo_name}: ok={r.succeeded} fail={r.failed}")
                else:
                    st.error(f"{repo_name}: {r.error}")
            st.rerun()

    with top_edges:
        if st.button("Build cross-repo edges", key="edges_btn",
                     help="Run pattern catalog → populate cross_repo_edges."):
            _run_build_edges()
            st.rerun()
    with top_pass3:
        if st.button(
            f"Pass 3 narratives ({pending_pass3})",
            key="pass3_btn",
            disabled=pending_pass3 == 0,
            help="Cross-module narratives (Opus). Requires cross-repo edges built.",
        ):
            _run_pass3()
            st.rerun()
    with top_embed:
        if pending_embeds_total and st.button(
            f"Embed all repos ({pending_embeds_total:,})", key="embed_all_btn"
        ):
            for r in embed_eligible:
                pe = pending_embeds_per_repo.get(r.repo_id) or {"code_chunks": 0, "generated_docs": 0}
                if pe["code_chunks"] + pe["generated_docs"] == 0:
                    continue
                _run_embed(r)
            st.rerun()

    for r in repos:
        product_name = products[r.product_id].name if r.product_id in products else "?"
        badge = _STATUS_BADGE.get(r.status, "•")
        counts = counts_by_repo.get(r.repo_id, {"files": 0, "entities": 0, "chunks": 0})
        count_str = (
            f" · files={counts['files']}, entities={counts['entities']}, chunks={counts['chunks']}"
            if counts["files"] else ""
        )
        with st.expander(
            f"{badge} [{product_name}] {r.display_name}  ·  {r.repo_role}  ·  "
            f"{r.priority}  ·  status={r.status}{count_str}"
        ):
            c1, c2 = st.columns([3, 1])
            with c1:
                st.markdown(f"**TFS URL:** `{r.tfs_url}`")
                st.markdown(
                    f"**Branch:** `{r.branch}`"
                    + (f"  ·  **Sub-path:** `{r.sub_path}`" if r.sub_path else "")
                )
                st.markdown(f"**Description:** {r.one_line_description}")
                st.markdown(f"**Owner:** {r.owner_team} · {r.owner_contact}")
                if counts["files"]:
                    st.markdown(
                        f"**Counts:** files={counts['files']}, "
                        f"parsed={counts['parsed_files']}, "
                        f"entities={counts['entities']}, chunks={counts['chunks']}"
                    )
                st.markdown("**Major workflows:** " + ", ".join(r.major_workflows))
                st.markdown(
                    "**Key business concepts:** " + ", ".join(r.key_business_concepts)
                )
                if r.critical_entry_points:
                    st.markdown(
                        "**Critical entry points:** "
                        + ", ".join(r.critical_entry_points)
                    )
                if r.special_notes:
                    st.markdown(f"**Notes:** {r.special_notes}")
                if r.clone_path:
                    st.markdown(f"**Clone path:** `{r.clone_path}`")
                st.caption(
                    f"clone_depth={r.clone_depth} · lfs={r.enable_lfs} · "
                    f"created={r.created_at:%Y-%m-%d %H:%M} · "
                    f"last_indexed_sha={r.last_indexed_sha[:8] if r.last_indexed_sha else '—'}"
                )
                if r.status == "error" and r.error_message:
                    st.error(r.error_message)
            with c2:
                clone_label = "Re-clone" if r.status in ("cloned", "ready", "parsing", "chunking") else "Clone now"
                if st.button(clone_label, key=f"clone_{r.repo_id}"):
                    with st.status(f"Cloning {r.display_name}…", expanded=True) as s:
                        result = clone_repo(r, product_name)
                        if result.success:
                            s.update(
                                label=f"Cloned · sha=`{(result.sha or '?')[:8]}`",
                                state="complete",
                            )
                        else:
                            s.update(label=f"Failed: {result.message[:200]}", state="error")
                    st.rerun()

                can_parse = bool(r.clone_path)
                parse_label = "Re-parse" if r.status == "ready" else "Parse"
                if st.button(parse_label, key=f"parse_{r.repo_id}", disabled=not can_parse):
                    with st.status(f"Parsing {r.display_name}…", expanded=True) as s:
                        progress = st.empty()

                        def _on_progress(msg: str, _ph=progress) -> None:
                            _ph.write(msg)

                        result = chunk_repo(r, on_progress=_on_progress)
                        if result.success:
                            s.update(
                                label=(
                                    f"Parsed — files={result.files_seen}, "
                                    f"parsed={result.files_parsed}, "
                                    f"entities={result.entities}, "
                                    f"chunks={result.chunks}, facts={result.facts}"
                                ),
                                state="complete",
                            )
                        else:
                            s.update(label=f"Failed: {result.error}", state="error")
                    st.rerun()

                pending_critical = count_pending_docs(r.repo_id, "critical", r.critical_entry_points)
                pending_meaningful = count_pending_docs(r.repo_id, "meaningful", r.critical_entry_points)

                if st.button(
                    f"Doc-gen sample ({pending_critical})",
                    key=f"docgen_sample_{r.repo_id}",
                    disabled=pending_critical == 0,
                    help="Doc-gen the critical_entry_points only. Cheap smoke test.",
                ):
                    _run_docgen(r, "critical")
                    st.rerun()

                if st.button(
                    f"Doc-gen meaningful ({pending_meaningful})",
                    key=f"docgen_meaningful_{r.repo_id}",
                    disabled=pending_meaningful == 0,
                    help="All meaningful entities — excludes POJOs, getters/setters, tests. Runs with 10× concurrency.",
                ):
                    _run_docgen(r, "meaningful")
                    st.rerun()

                pending_pass2 = count_pending_pass2(r.repo_id)
                if st.button(
                    f"Pass 2 module ({pending_pass2})",
                    key=f"pass2_{r.repo_id}",
                    disabled=pending_pass2 == 0,
                    help="Per-file rollup of Pass 1 entity docs (Sonnet).",
                ):
                    _run_pass2(r)
                    st.rerun()

                pending_l4 = count_pending_l4_enrich(r.repo_id)
                if st.button(
                    f"Enrich L4 ({pending_l4})",
                    key=f"enrich_{r.repo_id}",
                    disabled=pending_l4 == 0,
                    help="Multiple examples + statement annotations + git history (PO L4 entities).",
                ):
                    _run_enrich(r)
                    st.rerun()

                pending_embeds = count_pending_embeds(r.repo_id)
                pending_total = pending_embeds["code_chunks"] + pending_embeds["generated_docs"]
                embed_label = (
                    f"Embed ({pending_embeds['code_chunks']}+{pending_embeds['generated_docs']})"
                )
                if st.button(
                    embed_label,
                    key=f"embed_{r.repo_id}",
                    disabled=pending_total == 0,
                    help="Embed code chunks AND generated docs into per-repo FAISS.",
                ):
                    _run_embed(r)
                    st.rerun()

                if st.button("Delete", key=f"del_{r.repo_id}"):
                    delete_repo(r.repo_id)
                    st.rerun()


def _run_pass2(repo) -> None:
    with st.status(f"Pass 2 (module) {repo.display_name}…", expanded=True) as s:
        progress = st.empty()

        def _on_progress(msg: str, _ph=progress) -> None:
            _ph.write(msg)

        result = doc_gen_pass2_repo(repo.repo_id, on_progress=_on_progress)
        s.update(
            label=(
                f"Pass 2 — ok={result.succeeded}, fail={result.failed}, "
                f"tokens(in/out)={result.prompt_tokens}/{result.completion_tokens}"
            ),
            state="complete" if result.success else "error",
        )


def _run_enrich(repo) -> None:
    with st.status(f"Enrich L4 {repo.display_name}…", expanded=True) as s:
        progress = st.empty()

        def _on_progress(msg: str, _ph=progress) -> None:
            _ph.write(msg)

        result = enrich_l4_repo(repo.repo_id, on_progress=_on_progress)
        s.update(
            label=(
                f"L4 enrich — ok={result.succeeded}, fail={result.failed}, "
                f"examples={result.examples_added}, annotated={result.annotated}, "
                f"tokens(in/out)={result.prompt_tokens}/{result.completion_tokens}"
            ),
            state="complete" if result.success else "error",
        )


def _run_pass3() -> None:
    with st.status("Pass 3 (cross-module narratives)…", expanded=True) as s:
        progress = st.empty()

        def _on_progress(msg: str, _ph=progress) -> None:
            _ph.write(msg)

        result = doc_gen_pass3(on_progress=_on_progress)
        s.update(
            label=(
                f"Pass 3 — ok={result.succeeded}, fail={result.failed}, "
                f"tokens(in/out)={result.prompt_tokens}/{result.completion_tokens}"
            ),
            state="complete" if result.success else "error",
        )


def _run_build_edges() -> None:
    with st.status("Building cross-repo edges…", expanded=True) as s:
        progress = st.empty()

        def _on_progress(msg: str, _ph=progress) -> None:
            _ph.write(msg)

        result = discover_edges(on_progress=_on_progress)
        if result.success:
            s.update(
                label=f"Edges built — patterns={result.patterns_run}, edges={result.edges_created}",
                state="complete",
            )
        else:
            s.update(label=f"Failed: {result.error}", state="error")


def _render_sources(citations: list[dict]) -> None:
    """Collapsed 'Sources' expander, NLW-style — tucked away, not in the answer flow."""
    if not citations:
        return
    with st.expander(f"Sources ({len(citations)})"):
        for c in citations:
            label = c.get("qname") or c.get("repo") or "source"
            loc = f"`{c.get('file', '?')}`"
            if c.get("start_line"):
                loc += f":{c.get('start_line')}-{c.get('end_line', '?')}"
            st.markdown(f"- **{label}** — {loc}")


def _render_chat_tab() -> None:
    st.markdown("""
    <style>
    /* hide avatars — let bubbles speak */
    [data-testid^="chatAvatarIcon"] { display: none !important; }

    /* reset base message container */
    [data-testid="stChatMessage"] {
        padding: 2px 10px !important;
        background: transparent !important;
        box-shadow: none !important;
        border: none !important;
    }

    /* USER → right-aligned green bubble */
    [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) {
        flex-direction: row-reverse !important;
        margin-left: 20% !important;
        margin-right: 4px !important;
    }
    [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"])
    [data-testid="stChatMessageContent"] {
        background: #dcf8c6 !important;
        border-radius: 18px 18px 4px 18px !important;
        padding: 10px 16px !important;
        margin-left: auto;
    }

    /* ASSISTANT → left-aligned white bubble */
    [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) {
        margin-right: 20% !important;
    }
    [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"])
    [data-testid="stChatMessageContent"] {
        background: #ffffff !important;
        border: 1px solid #e0e0e0 !important;
        border-radius: 18px 18px 18px 4px !important;
        padding: 10px 16px !important;
        box-shadow: 0 1px 2px rgba(0,0,0,0.08) !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # --- all controls in the sidebar (clean main area, NLW-style) ---
    with st.sidebar:
        st.header("💬 RealRAG Chat")
        products = {p.product_id: p for p in list_products()}
        product_options = ["(all products)"] + [p.name for p in products.values()]
        selected = st.selectbox("Product", product_options, key="chat_product")
        product_id = next((pid for pid, p in products.items() if p.name == selected), None)
        deep_search = st.checkbox("Deep search", key="chat_deep_search", value=False,
                                  help="Slower, better recall on hard questions. Otherwise it auto-escalates only when needed.")
        agent_mode = st.checkbox("Agent / Trace mode", key="chat_agent_mode", value=False,
                                 help="Multi-step investigation (search → symbols → knowledge-graph → grep). Best for cross-repo traces and 'how is X calculated'. Slower — several steps.")
        live_agents = st.checkbox("Live Code + Agents interact", key="chat_live_agents", value=False,
                                  help="Real-time troubleshooting. Routes the question to the right codebase and drives the cross-repo investigator agents (rm-web→ys→po, reports→po) on the LIVE code. Returns the root cause + any data-fix SQL. Slowest — runs a full agent chain.")
        # Optional live-DB target for the agent run (you enter it directly). Leave
        # blank to skip live DB checks. PO = postgres, YSMaster = sqlserver.
        lc_db_server = lc_db_name = ""
        lc_db_type = "postgres"
        if live_agents:
            lc_db_server = st.text_input("DB server (optional)", key="lc_db_server",
                                         placeholder="e.g. rcqypodbpgr001.realpage.com")
            lc_db_name = st.text_input("Database (optional)", key="lc_db_name",
                                       placeholder="e.g. truamerica")
            lc_db_type = st.selectbox("DB type", ["postgres", "sqlserver"], key="lc_db_type")
            st.caption("Leave DB blank to skip live data checks. Credentials come from the repo's tools/.env.")
        new_chat = st.button("🗨 New chat", use_container_width=True)
        st.caption("Plain-business answers from your indexed RMS codebase. Sources shown under each answer.")

    # Durable conversation: resume the latest saved session on first load (survives
    # page reloads and server restarts); "New chat" starts a fresh session.
    if new_chat or "chat_session_id" not in st.session_state:
        if new_chat:
            sid = create_session(product_id)
            st.session_state["chat_history"] = []
        else:
            sid = latest_session() or create_session(product_id)
            st.session_state["chat_history"] = load_messages(sid)
        st.session_state["chat_session_id"] = sid
        if new_chat:
            st.rerun()

    # --- main area: just the conversation thread + input ---
    for turn in st.session_state["chat_history"]:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn["role"] == "assistant":
                _render_sources(turn.get("citations") or [])

    question = st.chat_input("Ask about recommended rent, forecasting, renewals, widgets…")
    if not question:
        return

    sid = st.session_state["chat_session_id"]
    st.session_state["chat_history"].append({"role": "user", "content": question})
    save_message(sid, "user", question)
    with st.chat_message("user"):
        st.write(question)

    def _stream_to(placeholder, gen) -> str:
        acc = ""
        for delta in gen:
            acc += delta
            placeholder.markdown(acc)
        return acc

    def _run(prep, placeholder):
        if prep["mode"] == "final":
            placeholder.markdown(prep["answer"])
            return prep["answer"]
        return _stream_to(placeholder, stream_answer(prep))

    hist = st.session_state["chat_history"][:-1]
    with st.chat_message("assistant"):
        # Live Code + Agents: route to the right repo and drive the existing
        # cross-repo Claude Code investigator agents headless on the live source.
        if live_agents:
            ph = st.empty()
            status = st.empty()
            with st.spinner("Routing to the right codebase…"):
                plan = build_launch_plan(question, product_id=product_id,
                                         server=lc_db_server, database=lc_db_name,
                                         db_type=lc_db_type)
            _live = plan["db_connection"] != "unavailable"
            status.caption(f"🧭 flow `{plan['entry']}` ({' → '.join(plan['chain'])}) · "
                           f"agent `{plan['agent']}` · live-DB {'on' if _live else 'off'}")
            meta: dict = {}
            answer_text = _stream_to(ph, run_flow(plan, question, meta))
            citations = []
            display_tier = f"live-agents · {plan['entry']}"
            if meta.get("cost_usd"):
                status.caption(f"🧭 flow `{plan['entry']}` ({' → '.join(plan['chain'])}) · "
                               f"agent `{plan['agent']}` · ${meta['cost_usd']:.2f}")
        # Opt-in agentic path: multi-step tool-use investigation. Separate from the
        # default fast/streaming path (which is left exactly as-is).
        elif agent_mode:
            ph = st.empty()
            status = st.empty()
            with st.spinner("Agent investigating…"):
                res = run_agent(question, product_id=product_id, history=hist,
                                on_step=lambda s: status.caption(f"🔧 {s}"))
            status.empty()
            answer_text = res.get("answer") or "(no answer)"
            ph.markdown(answer_text)
            citations = res.get("citations") or []
            display_tier = f"agent · {res.get('steps')} steps"
            if res.get("trace"):
                with st.expander(f"Investigation ({len(res['trace'])} tool calls)"):
                    for t in res["trace"]:
                        st.markdown(f"- `{t}`")
        # Speed: exact cache hit (only for standalone questions, no prior context).
        elif (cached := (cache_get(product_id, question) if (_CACHE_ON and not hist) else None)):
            ph = st.empty()
            ph.markdown(cached["answer"])
            answer_text = cached["answer"]
            citations = cached["citations"] or []
            display_tier = f"{cached.get('tier')} · cached"
        else:
            with st.spinner("Retrieving…"):
                prep = prepare_answer(question, product_id=product_id, history=hist,
                                      deep_search=deep_search)
            ph = st.empty()
            answer_text = _run(prep, ph)
            citations = prep.get("citations") or []
            display_tier = prep.get("display_tier")

            # Weakness signals: refusal, hedge, fabricated artifact, OR the question's
            # named subject is absent from the retrieved sources (retrieval drifted —
            # the 'confident-wrong neighborhood' failure).
            def _weak(ans: str, ctx: str) -> bool:
                return (answer_looks_unsure(ans)
                        or answer_has_fabrication(ans, ctx)
                        or not context_covers_subjects(question, ctx))

            ctx_text = prep.get("context_text", "")
            grounded = True
            if not deep_search and (prep["mode"] == "final" or _weak(answer_text, ctx_text)):
                # Step 1: escalate to Deep search.
                st.caption("🔎 Verifying / searching deeper…")
                with st.spinner("Deep search…"):
                    prep2 = prepare_answer(question, product_id=product_id, history=hist,
                                           deep_search=True)
                answer_text = _run(prep2, ph)
                citations = prep2.get("citations") or citations
                display_tier = f"{prep2.get('display_tier')} (deep)"
                ctx_text = prep2.get("context_text", ctx_text)
                # Step 2: still weak → hand off to the multi-step agent (final recourse).
                if prep2["mode"] != "final" and _weak(answer_text, ctx_text):
                    st.caption("🤖 Still uncertain — investigating step-by-step…")
                    with st.spinner("Agent investigating…"):
                        res = run_agent(question, product_id=product_id, history=hist)
                    answer_text = res.get("answer") or answer_text
                    citations = res.get("citations") or citations
                    display_tier = f"agent · {res.get('steps')} steps (auto)"
                    grounded = not answer_looks_unsure(answer_text)

            # Cache only grounded, confident answers (never cache a weak/uncertain one).
            if (_CACHE_ON and not hist and answer_text and grounded
                    and not answer_looks_unsure(answer_text)
                    and context_covers_subjects(question, ctx_text)):
                cache_put(product_id, question, answer_text, citations, display_tier)

        _render_sources(citations)

    st.session_state["chat_history"].append({
        "role": "assistant",
        "content": answer_text or "(empty)",
        "citations": citations,
    })
    save_message(sid, "assistant", answer_text or "(empty)", citations)

    # Scroll the conversation to the latest answer (Streamlit stays at top otherwise).
    components.html(
        """
        <script>
          const doc = window.parent.document;
          const sels = ['section.main', '[data-testid="stMain"]',
                        '[data-testid="stAppViewContainer"]', '.main'];
          let c = null;
          for (const s of sels) { const e = doc.querySelector(s); if (e) { c = e; break; } }
          const go = () => { if (c) c.scrollTo({ top: c.scrollHeight, behavior: 'smooth' });
                             else window.parent.scrollTo(0, doc.body.scrollHeight); };
          setTimeout(go, 100); setTimeout(go, 400);
        </script>
        """,
        height=0,
    )


def _render_coverage_tab() -> None:
    st.subheader("Coverage matrix")
    rows = repo_coverage()
    if rows:
        for r in rows:
            with st.expander(f"{r.display_name} — {r.total_entities:,} entities"):
                cols = st.columns(7)
                cols[0].metric("Pass 1", r.pass1_count)
                cols[1].metric("verified", r.pass1_verified_count)
                cols[2].metric("Pass 2 (module)", r.module_doc_covered_count)
                cols[3].metric("statement-annotated", r.statement_annotated_count)
                cols[4].metric("git-history", r.git_history_count)
                cols[5].metric("multi-example", r.multi_example_count)
                cols[6].metric("entities", r.total_entities)
    else:
        st.info("No parsed repos yet.")

    st.divider()
    st.subheader("Cross-repo edges")
    by_kind = count_edges_by_kind()
    if by_kind:
        st.write(dict(by_kind))
    else:
        st.info("No cross-repo edges built yet. Use the **Build cross-repo edges** button above.")

    st.divider()
    st.subheader("Open failed questions")
    failed = list_open_failed_questions(limit=30)
    if not failed:
        st.info("No failed questions in queue. Self-healing fires when chat answers refuse or fail verification.")
    else:
        for f in failed:
            with st.expander(f"❓ {f['question'][:120]}"):
                st.markdown(f"**Reason:** {f.get('refusal_reason') or '?'}")
                st.markdown(f"**Retrieved/Used:** {f.get('retrieved_count', 0)} / {f.get('used_count', 0)}")
                st.caption(f"created {f['created_at']:%Y-%m-%d %H:%M}")


def _run_docgen(repo, scope: str) -> None:
    with st.status(f"Doc-gen ({scope}) {repo.display_name}…", expanded=True) as s:
        progress = st.empty()

        def _on_progress(msg: str, _ph=progress) -> None:
            _ph.write(msg)

        result = doc_gen_repo(
            repo.repo_id, scope, repo.critical_entry_points, on_progress=_on_progress
        )
        if result.success:
            s.update(
                label=(
                    f"Doc-gen done — attempted={result.attempted}, "
                    f"ok={result.succeeded}, fail={result.failed}, "
                    f"tokens(in/out)={result.prompt_tokens}/{result.completion_tokens}"
                ),
                state="complete",
            )
        else:
            s.update(
                label=(
                    f"Doc-gen completed with errors — ok={result.succeeded}, "
                    f"fail={result.failed}: {result.error or ''}"
                ),
                state="error",
            )


def _run_embed(repo) -> None:
    with st.status(f"Embedding {repo.display_name}…", expanded=True) as s:
        progress = st.empty()

        def _on_progress(msg: str, _ph=progress) -> None:
            _ph.write(msg)

        result = embed_repo(repo.repo_id, on_progress=_on_progress)
        if result.success:
            s.update(
                label=(
                    f"Embedded — code={result.code_chunks_embedded}, "
                    f"docs={result.generated_docs_embedded}"
                ),
                state="complete",
            )
        else:
            s.update(label=f"Failed: {result.error}", state="error")


def _render_activity_tab() -> None:
    running = list_running()
    if running:
        st.subheader("Currently running")
        for run in running:
            st.write(
                f"⏳ **{run['stage']}** · {run.get('display_name') or '?'} · "
                f"started {run['started_at']:%H:%M:%S}  ·  {run.get('notes') or ''}"
            )
        st.divider()

    runs = list_recent_runs(limit=30)
    if not runs:
        st.info("No pipeline runs yet.")
        return

    st.subheader(f"Recent runs (latest {len(runs)})")
    for r in runs:
        icon = {"running": "⏳", "success": "✅", "error": "❌", "cancelled": "🚫"}.get(
            r["status"], "•"
        )
        elapsed = f"{r['elapsed_seconds']:.1f}s" if r["elapsed_seconds"] else "—"
        repo_label = r.get("display_name") or "—"
        with st.expander(
            f"{icon} {r['stage']} · {repo_label} · {r['started_at']:%Y-%m-%d %H:%M:%S} · {elapsed}"
        ):
            if r["counts"]:
                cols = st.columns(len(r["counts"]) or 1)
                for col, (k, v) in zip(cols, r["counts"].items()):
                    col.metric(k, v)
            if r["notes"]:
                st.caption(r["notes"])
            if r["error_message"]:
                st.error(r["error_message"])


st.title("RealRAG — Config")
st.caption("Phase 1 — register repos. Indexing pipeline lands next.")

tab_add, tab_list, tab_chat, tab_coverage, tab_activity = st.tabs([
    "Add repos", "Registered repos", "Chat", "Coverage", "Activity",
])
with tab_add:
    _render_add_repo_tab()
with tab_list:
    _render_registered_repos_tab()
with tab_chat:
    _render_chat_tab()
with tab_coverage:
    _render_coverage_tab()
with tab_activity:
    _render_activity_tab()
