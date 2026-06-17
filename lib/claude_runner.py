"""Headless Claude Code launcher for the "Live Code + Agents interact" mode.

Given a launch plan from lib.agent_flows.build_launch_plan, this shells out to the
`claude` CLI in print/headless mode, runs the chosen agent (orchestrator or a leaf
specialist) against the configured repo + sibling repos, and streams the run back
as text deltas suitable for Streamlit's _stream_to().

The run is unattended (bypassPermissions, report-only directive) so it never blocks
on a permission or AskUserQuestion prompt.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from lib.agent_flows import load_flows_config


def _claude_bin() -> str:
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")


def _tool_marker(block: dict) -> str:
    """A short progress line for a tool_use block (esp. sub-agent spawns)."""
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    if name == "Agent":
        sub = inp.get("subagent_type") or inp.get("description") or "sub-agent"
        return f"\n\n🔧 → **{sub}**\n\n"
    if name in ("Bash", "Skill"):
        detail = inp.get("command") or inp.get("skill") or ""
        return f"\n\n🔧 {name} `{str(detail)[:80]}`\n\n"
    return f"\n\n🔧 {name}\n\n"


def _build_prompt(plan: dict, question: str, directive: str) -> str:
    """Prompt for the run. Orchestrator mode lets the forced agent self-drive;
    direct mode tells the default session which sub-agents to invoke, in order."""
    if plan.get("launch_mode") == "orchestrator":
        return f"{question}\n\n---\n{directive}"
    seq = plan.get("agent_sequence") or ([plan["agent"]] if plan.get("agent") else [])
    seq_txt = " → ".join(seq)
    return (
        f"ISSUE TO INVESTIGATE:\n{question}\n\n"
        f"INVESTIGATION PLAN: invoke these sub-agents IN ORDER via the Agent tool, "
        f"forwarding each one's output to the next per "
        f"`.claude/agents/cross-repo-handoff-contracts.md`, then synthesize the findings:\n"
        f"  {seq_txt}\n"
        f"Do not skip steps and do not investigate directly — delegate to each agent.\n\n"
        f"---\n{directive}"
    )


def build_command(plan: dict, question: str) -> tuple[list[str], str, dict]:
    """Return (argv, cwd, run_cfg) for the headless run."""
    cfg = load_flows_config()
    run = cfg.get("run", {})
    fix_mode = run.get("fix_mode", "patch")
    fix_directive = (run.get("fix_directives") or {}).get(fix_mode, "")
    directive = (run.get("unattended_directive") or "") \
        .replace("{db_connection}", plan.get("db_connection", "unavailable")) \
        .replace("{fix_directive}", fix_directive.strip())
    prompt = _build_prompt(plan, question, directive)

    argv = [_claude_bin(), "-p", prompt]
    # Orchestrator mode forces the self-driving orchestrator as the session agent.
    # Direct mode runs the default session, which delegates to the agent_sequence.
    if plan.get("launch_mode") == "orchestrator" and plan.get("agent"):
        argv += ["--agent", plan["agent"]]
    argv += [
        "--permission-mode", run.get("permission_mode", "bypassPermissions"),
        "--output-format", run.get("output_format", "stream-json"),
        "--verbose",  # required alongside stream-json in -p mode
        "--max-turns", str(run.get("max_turns", 80)),
    ]
    if run.get("include_partial_messages"):
        argv.append("--include-partial-messages")
    for d in plan.get("add_dirs", []):
        argv += ["--add-dir", d]
    return argv, plan["cwd"], run


def run_flow(plan: dict, question: str, meta: dict | None = None):
    """Generator: yield text chunks from a headless agent run.

    `meta` (if given) is filled with session_id / returncode / error for the caller
    to read after the generator is exhausted.
    """
    meta = meta if meta is not None else {}
    argv, cwd, run = build_command(plan, question)
    include_partial = bool(run.get("include_partial_messages"))

    chain = " → ".join(plan.get("chain", [])) or plan.get("entry", "?")
    yield (f"**Live Code + Agents** · flow `{plan.get('entry')}` "
           f"({chain}) · agent `{plan.get('agent')}` · fix_mode `{run.get('fix_mode', 'patch')}`"
           f"\n\n_Investigating…_\n\n")

    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        meta["error"] = "claude CLI not found on PATH"
        yield "\n\n_(error: the `claude` CLI was not found — is Claude Code installed?)_"
        return

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-JSON noise

            t = obj.get("type")

            if t == "stream_event" and include_partial:
                ev = obj.get("event", {})
                et = ev.get("type")
                if et == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    yield ev["delta"].get("text", "")
                elif et == "content_block_start" and ev.get("content_block", {}).get("type") == "tool_use":
                    yield _tool_marker(ev["content_block"])
                continue

            if t == "assistant" and not include_partial:
                for b in obj.get("message", {}).get("content", []):
                    if b.get("type") == "text":
                        yield b.get("text", "")
                    elif b.get("type") == "tool_use":
                        yield _tool_marker(b)
                continue

            if t == "result":
                meta["session_id"] = obj.get("session_id")
                meta["cost_usd"] = obj.get("total_cost_usd")
                meta["is_error"] = obj.get("is_error", False)
                if obj.get("is_error"):
                    yield f"\n\n_(run ended with an error: {obj.get('subtype')})_"
                continue
    finally:
        # If the consumer stopped early (Streamlit rerun / GeneratorExit), don't
        # leave the agent chain running — terminate it.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        else:
            proc.wait()
        meta["returncode"] = proc.returncode
        if proc.returncode not in (0, None):
            err = (proc.stderr.read() or "").strip()[-600:]
            meta.setdefault("error", err)
            if err:
                yield f"\n\n_(claude exited {proc.returncode}: {err})_"
