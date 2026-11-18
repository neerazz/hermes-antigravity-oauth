"""Prompt construction, message translation, and tool-call parsing for Antigravity."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

_ROLE_LABELS = {
    "system": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool Result",
}

_PROMPT_PREAMBLE = (
    "You are being used strictly as an LLM inference backend for Hermes Agent.",
    "You do not possess any local execution tools in this mode. NEVER invoke native function calling / agent tools (including read_file, write_file, edit_file, search_files, run_command, manage_task, send_message, schedule, grep_search, find_by_name, view_file, write_to_file, etc.) — native tool calls are blocked/rejected.",
    "All tool executions and filesystem interactions are performed exclusively by Hermes Agent.",
    "IMPORTANT INSTRUCTIONS FOR TOOLS:",
    "- If you need to call a tool, emit ONLY <tool_call>{...}</tool_call> blocks in your text output.",
    "- Each tool call must be a JSON object containing 'id', 'type': 'function', and 'function': {'name': '...', 'arguments': '...'}.",
    "- 'arguments' must be a JSON-encoded string containing the function arguments.",
    "- Do NOT execute local shell commands or file operations directly; only output <tool_call> tags so Hermes can execute them safely.",
    "- NEVER simulate or hallucinate Tool Results (e.g. 'Tool Result (...):'). Hermes Agent executes tools externally and will supply real results in subsequent turns.",
    "- After emitting tool calls, STOP generating immediately. Do NOT generate results, execution output, or commentary after tool calls.",
    "- If no tool is needed, respond naturally with standard text.",
)

_VISION_PREAMBLE = (
    "You are being used as a multimodal vision analysis backend for Hermes Agent.",
    "An image file path is provided in the user prompt below (hermes_agy_vision_*).",
    "Use your read_file tool on that exact image path to inspect the image, then respond directly with your analysis text.",
)

_TOOL_CALL_PREFIXES = tuple(
    "<tool_call>"[:i] for i in range(len("<tool_call>"), 0, -1)
)

_CURRENT_WORKSPACE_DIR: str | None = None
_LAST_OMITTED_BYTES: int = 0


def consume_last_omitted_bytes() -> int:
    """Return and reset the UTF-8 byte count omitted by the most recent prompt assembly."""
    global _LAST_OMITTED_BYTES
    val = _LAST_OMITTED_BYTES
    _LAST_OMITTED_BYTES = 0
    return val


def _materialize_vision_image(url: str) -> str | None:
    """Materialize a data:image/...;base64,... URI or local image path inside workspace or /tmp."""
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    base_dir = Path(_CURRENT_WORKSPACE_DIR) if _CURRENT_WORKSPACE_DIR and os.path.isdir(_CURRENT_WORKSPACE_DIR) else Path("/tmp")
    if u.startswith("data:image/") and ";base64," in u:
        header, b64_data = u.split(";base64,", 1)
        mime_sub = header.split("/", 1)[1].split(";")[0].strip().lower()
        ext_map = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp", "gif": "gif"}
        ext = ext_map.get(mime_sub, "jpg")
        try:
            raw = base64.b64decode(b64_data)
        except Exception:
            return None
        digest = hashlib.sha256(raw).hexdigest()[:16]
        target = base_dir / f"hermes_agy_vision_{digest}.{ext}"
        if not target.is_file():
            try:
                fd, tmp_name = tempfile.mkstemp(prefix="hermes_agy_vision_tmp_", dir=str(base_dir))
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, target)
            except OSError:
                return None
        return str(target)
    if u.startswith("/") and os.path.isfile(u):
        try:
            raw = Path(u).read_bytes()
            ext = Path(u).suffix.lstrip(".") or "jpg"
            digest = hashlib.sha256(raw).hexdigest()[:16]
            target = base_dir / f"hermes_agy_vision_{digest}.{ext}"
            if not target.is_file():
                target.write_bytes(raw)
                os.chmod(target, 0o600)
            return str(target)
        except OSError:
            return u
    return None


def _render_message_content(content: Any) -> str:
    """Normalize multimodal or structured message content into a string."""
    if content is None:
        return ""
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        return str(content.get("content") or "").strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item.strip())
            elif isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text":
                    parts.append(str(item.get("text") or "").strip())
                elif item_type == "image_url":
                    img_obj = item.get("image_url")
                    url = img_obj.get("url") if isinstance(img_obj, dict) else img_obj
                    mat = _materialize_vision_image(str(url or ""))
                    if mat:
                        parts.append(
                            f'[Attached Image File: {mat} — call read_file with Path="{mat}" to inspect this image.]'
                        )
                elif "content" in item:
                    parts.append(str(item.get("content") or "").strip())
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip()


def _within_prompt_budget(text: str, max_chars: int, max_bytes: int) -> bool:
    return len(text) <= max_chars and len(text.encode("utf-8")) <= max_bytes


def _truncate_middle_to_budget(text: str, max_chars: int, max_bytes: int) -> str:
    """Truncate the middle of ``text`` so it fits within both char and UTF-8 byte budgets."""
    global _LAST_OMITTED_BYTES
    if _within_prompt_budget(text, max_chars, max_bytes):
        return text
    _LAST_OMITTED_BYTES += max(0, len(text.encode("utf-8")) - max_bytes)
    marker = f"\n[... truncated {len(text)} chars to fit prompt budget ...]\n"
    avail_chars = max(0, max_chars - len(marker))
    avail_bytes = max(0, max_bytes - len(marker.encode("utf-8")))
    if avail_chars == 0 or avail_bytes == 0:
        return text[-max_chars:].encode("utf-8")[-max_bytes:].decode("utf-8", errors="ignore")
    # Keep 25% head and 75% tail so the latest content at the end is prioritized.
    head_chars = avail_chars // 4
    tail_chars = avail_chars - head_chars
    head = text[:head_chars].encode("utf-8")[: avail_bytes // 4].decode("utf-8", errors="ignore")
    tail_byte_budget = max(0, avail_bytes - len(head.encode("utf-8")))
    tail = text[-tail_chars:] if tail_chars > 0 else ""
    if len(tail.encode("utf-8")) > tail_byte_budget:
        tail = tail.encode("utf-8")[-tail_byte_budget:].decode("utf-8", errors="ignore")
    return head + marker + tail


def _fit_transcript_to_budget(
    transcript_items: list[dict[str, Any]],
    fixed_overhead_text: str,
    max_prompt_chars: int,
    max_prompt_bytes: int,
) -> list[str]:
    """Ensure the assembled prompt stays under agy's ~200KB input truncation limit.

    Preserves the tail of the conversation (recent tool calls/results, the active
    user request, and the trailing instruction section) by first compacting older
    assistant tool-call payloads and then dropping the oldest transcript entries
    from the front if still over budget.
    """
    if not transcript_items:
        return []

    entries = [item["full"] for item in transcript_items]

    def _assembled_size(curr_entries: list[str]) -> tuple[int, int]:
        if curr_entries:
            body = "Conversation transcript:\n\n" + "\n\n".join(curr_entries)
            full = f"{fixed_overhead_text}\n\n{body}" if fixed_overhead_text else body
        else:
            full = fixed_overhead_text
        return len(full), len(full.encode("utf-8"))

    chars, utf8_bytes = _assembled_size(entries)
    if chars <= max_prompt_chars and utf8_bytes <= max_prompt_bytes:
        return entries

    # Pass 1: compact older entries (e.g. large historical assistant tool_calls/text)
    # from oldest to newest before dropping whole turns.
    for idx, item in enumerate(transcript_items):
        if item["compact"] != entries[idx]:
            entries[idx] = item["compact"]
            chars, utf8_bytes = _assembled_size(entries)
            if chars <= max_prompt_chars and utf8_bytes <= max_prompt_bytes:
                return entries

    # Pass 2: keep the most recent entries from the tail, plus pin the latest
    # User message in the transcript so mid-turn worker respawns (where last_role
    # is 'tool') never lose the active user request.
    latest_user_idx: int | None = None
    for idx in range(len(transcript_items) - 1, -1, -1):
        if transcript_items[idx]["role"] == "user":
            latest_user_idx = idx
            break

    overhead_With_header = (
        f"{fixed_overhead_text}\n\nConversation transcript:\n\n"
        if fixed_overhead_text
        else "Conversation transcript:\n\n"
    )
    avail_chars = max(2000, max_prompt_chars - len(overhead_With_header))
    avail_bytes = max(2000, max_prompt_bytes - len(overhead_With_header.encode("utf-8")))

    omission_template = "[... {count} earlier transcript messages omitted to preserve latest user request and recent context ...]"
    gap_template = "[... {count} intermediate tool/assistant messages omitted ...]"
    reserve_chars = len(omission_template.format(count=len(entries))) + len(gap_template.format(count=len(entries))) + 16
    reserve_bytes = len(omission_template.format(count=len(entries)).encode("utf-8")) + len(gap_template.format(count=len(entries)).encode("utf-8")) + 16

    budget_chars = max(1000, avail_chars - reserve_chars)
    budget_bytes = max(1000, avail_bytes - reserve_bytes)

    pinned_user_entry: str | None = None
    pinned_user_chars = 0
    pinned_user_bytes = 0
    if latest_user_idx is not None:
        pinned_user_entry = _truncate_middle_to_budget(
            entries[latest_user_idx],
            max(500, budget_chars // 3),
            max(500, budget_bytes // 3),
        )
        pinned_user_chars = len(pinned_user_entry) + 2
        pinned_user_bytes = len(pinned_user_entry.encode("utf-8")) + 2

    kept_indices: list[int] = []
    used_chars = 0
    used_bytes = 0

    for idx in range(len(entries) - 1, -1, -1):
        candidate = entries[idx]
        sep_chars = 2 if kept_indices else 0
        sep_bytes = 2 if kept_indices else 0

        # If we have not yet reached latest_user_idx, reserve room for it.
        need_user_reserve = (
            latest_user_idx is not None
            and idx > latest_user_idx
            and pinned_user_entry is not None
        )
        user_res_chars = pinned_user_chars if need_user_reserve else 0
        user_res_bytes = pinned_user_bytes if need_user_reserve else 0

        cand_chars = len(candidate)
        cand_bytes = len(candidate.encode("utf-8"))

        if (
            used_chars + sep_chars + cand_chars + user_res_chars <= budget_chars
            and used_bytes + sep_bytes + cand_bytes + user_res_bytes <= budget_bytes
        ):
            kept_indices.append(idx)
            used_chars += sep_chars + cand_chars
            used_bytes += sep_bytes + cand_bytes
        else:
            if not kept_indices:
                # Even the very last transcript entry is oversized; truncate its middle
                # so the tail of the latest entry and the pinned user request still fit.
                rem_chars = max(500, budget_chars - user_res_chars)
                rem_bytes = max(500, budget_bytes - user_res_bytes)
                entries[idx] = _truncate_middle_to_budget(candidate, rem_chars, rem_bytes)
                kept_indices.append(idx)
                used_chars += len(entries[idx])
                used_bytes += len(entries[idx].encode("utf-8"))
            break

    if latest_user_idx is not None and latest_user_idx not in kept_indices and pinned_user_entry is not None:
        entries[latest_user_idx] = pinned_user_entry
        kept_indices.append(latest_user_idx)

    kept_indices.sort()
    global _LAST_OMITTED_BYTES
    omitted_count = len(entries) - len(kept_indices)
    result: list[str] = []
    if omitted_count > 0:
        omitted_set = set(range(len(entries))) - set(kept_indices)
        _LAST_OMITTED_BYTES += sum(len(entries[idx].encode("utf-8")) for idx in omitted_set)
        result.append(omission_template.format(count=omitted_count))
    for pos, idx in enumerate(kept_indices):
        if pos > 0 and idx - kept_indices[pos - 1] > 1 and omitted_count > 0 and pos == 1 and kept_indices[0] == latest_user_idx:
            gap = idx - kept_indices[0] - 1
            if gap > 0:
                result.append(f"[... {gap} intermediate tool/assistant messages omitted ...]")
        result.append(entries[idx])
    return result


_HISTORICAL_TOOL_RETENTION_COUNT = 8
_HISTORICAL_TOOL_MAX_CHARS = 300
_HISTORICAL_TOOL_CALL_ARGS_MAX_CHARS = 600
_HISTORICAL_ASSISTANT_TEXT_MAX_CHARS = 1200
_TOOL_MAX_CHARS_CAP = 20000
# agy CLI hard-truncates single USER_INPUT messages at ~200,000 UTF-8 bytes
# (~191,174 chars), cutting off the end of the prompt where the latest user
# request lives. Keep a safe margin below that cliff.
_MAX_PROMPT_CHARS = 150000
_MAX_PROMPT_BYTES = 160000


def _format_messages_as_prompt(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    rules_in_workspace: bool = False,
    max_prompt_chars: int = _MAX_PROMPT_CHARS,
    max_prompt_bytes: int = _MAX_PROMPT_BYTES,
    workspace_dir: str | None = None,
) -> str:
    """Assemble Hermes message history, system prompt, and tool schemas into a canonical prompt.

    ``rules_in_workspace``: the preamble and system prompt already live in the workspace GEMINI.md
    (see ``soul.py``), so the stdin turn carries only tools and conversation.
    """
    try:
        from agent.acp_openai_bridge import render_tool_bridge_sections
        tool_sections = render_tool_bridge_sections(tools, tool_choice)
    except Exception:
        tool_sections = []
        if tools:
            tool_sections.append(
                "Available tools (OpenAI function schema):\n"
                + json.dumps(tools, ensure_ascii=False)
            )

    global _CURRENT_WORKSPACE_DIR, _LAST_OMITTED_BYTES
    _CURRENT_WORKSPACE_DIR = workspace_dir
    _LAST_OMITTED_BYTES = 0

    valid_messages = [m for m in messages if isinstance(m, dict)]
    last_msg = valid_messages[-1] if valid_messages else None
    last_role = str(last_msg.get("role") or "").strip().lower() if last_msg else ""

    system_parts: list[str] = []
    history_messages: list[dict[str, Any]] = []

    for msg in valid_messages:
        role = str(msg.get("role") or "unknown").strip().lower()
        if role == "system":
            rendered = _render_message_content(msg.get("content"))
            if rendered:
                system_parts.append(rendered)
        else:
            history_messages.append(msg)

    last_user_text = (
        _render_message_content(last_msg.get("content"))
        if (last_role == "user" and last_msg is not None)
        else ""
    )
    is_vision_only = (not tools) and ("hermes_agy_vision_" in last_user_text)
    if is_vision_only:
        preamble = _VISION_PREAMBLE
    elif rules_in_workspace:
        preamble = ()
    else:
        preamble = _PROMPT_PREAMBLE

    sections: list[str] = [*preamble, *tool_sections]

    if system_parts and not rules_in_workspace:
        sections.append("### SYSTEM INSTRUCTIONS (HERMES AGENT):\n" + "\n\n".join(f"System:\n{p}" for p in system_parts))

    transcript_items: list[dict[str, Any]] = []
    prior_messages = history_messages[:-1] if last_role == "user" and len(history_messages) > 1 else history_messages

    tool_indices = [
        i for i, msg in enumerate(prior_messages)
        if str(msg.get("role") or "").strip().lower() == "tool"
    ]
    recent_tool_cutoff = (
        set(tool_indices[-_HISTORICAL_TOOL_RETENTION_COUNT:])
        if len(tool_indices) > _HISTORICAL_TOOL_RETENTION_COUNT
        else set(tool_indices)
    )
    assistant_tool_indices = [
        i for i, msg in enumerate(prior_messages)
        if str(msg.get("role") or "").strip().lower() == "assistant" and msg.get("tool_calls")
    ]
    recent_assistant_tool_cutoff = (
        set(assistant_tool_indices[-_HISTORICAL_TOOL_RETENTION_COUNT:])
        if len(assistant_tool_indices) > _HISTORICAL_TOOL_RETENTION_COUNT
        else set(assistant_tool_indices)
    )

    for i, message in enumerate(prior_messages):
        role = str(message.get("role") or "unknown").strip().lower()
        rendered_content = _render_message_content(message.get("content"))

        if role == "tool":
            tool_id = str(message.get("tool_call_id") or message.get("name") or "tool").strip()
            if i not in recent_tool_cutoff and len(rendered_content) > _HISTORICAL_TOOL_MAX_CHARS:
                rendered_content = (
                    rendered_content[:200]
                    + f"\n[... tool output truncated: {len(rendered_content)} chars ...]\n"
                    + rendered_content[-100:]
                )
            elif len(rendered_content) > _TOOL_MAX_CHARS_CAP:
                rendered_content = (
                    rendered_content[:12000]
                    + f"\n[... large tool output truncated: {len(rendered_content)} chars ...]\n"
                    + rendered_content[-3000:]
                )
            entry_str = f"Tool Result ({tool_id}):\n{rendered_content}"
            compact_str = entry_str
            if len(rendered_content) > _HISTORICAL_TOOL_MAX_CHARS:
                compact_body = (
                    rendered_content[:200]
                    + f"\n[... tool output truncated: {len(rendered_content)} chars ...]\n"
                    + rendered_content[-100:]
                )
                compact_str = f"Tool Result ({tool_id}):\n{compact_body}"
            transcript_items.append({"role": role, "full": entry_str, "compact": compact_str})
            continue

        if role == "assistant":
            parts = []
            compact_parts = []
            if "Tool Result (" in rendered_content:
                rendered_content = rendered_content.split("Tool Result (")[0].strip()
            if rendered_content:
                parts.append(rendered_content)
                if i not in recent_assistant_tool_cutoff and len(rendered_content) > _HISTORICAL_ASSISTANT_TEXT_MAX_CHARS:
                    compact_parts.append(
                        rendered_content[:700]
                        + f"\n[... assistant response truncated: {len(rendered_content)} chars ...]\n"
                        + rendered_content[-300:]
                    )
                else:
                    compact_parts.append(rendered_content)
            if tool_calls := message.get("tool_calls"):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function") or {}
                        raw_args = (
                            fn.get("arguments", "{}")
                            if isinstance(fn.get("arguments"), str)
                            else json.dumps(fn.get("arguments", {}), ensure_ascii=False)
                        )
                        call_obj = {
                            "id": tc.get("id") or "call_1",
                            "type": "function",
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": raw_args,
                            }
                        }
                        full_tc_str = f"<tool_call>{json.dumps(call_obj, ensure_ascii=False)}</tool_call>"
                        parts.append(full_tc_str)
                        if i not in recent_assistant_tool_cutoff and len(raw_args) > _HISTORICAL_TOOL_CALL_ARGS_MAX_CHARS:
                            compact_args = (
                                raw_args[:300]
                                + f" ... [truncated {len(raw_args)} chars] ... "
                                + raw_args[-150:]
                            )
                            compact_call_obj = {
                                "id": tc.get("id") or "call_1",
                                "type": "function",
                                "function": {
                                    "name": fn.get("name", ""),
                                    "arguments": compact_args,
                                }
                            }
                            compact_parts.append(f"<tool_call>{json.dumps(compact_call_obj, ensure_ascii=False)}</tool_call>")
                        else:
                            compact_parts.append(full_tc_str)
            if parts:
                transcript_items.append({
                    "role": role,
                    "full": "Assistant:\n" + "\n".join(parts),
                    "compact": "Assistant:\n" + "\n".join(compact_parts),
                })
            continue

        label = _ROLE_LABELS.get(role, "Context")
        if rendered_content:
            entry_str = f"{label}:\n{rendered_content}"
            transcript_items.append({"role": role, "full": entry_str, "compact": entry_str})

    if last_role == "tool":
        footer_section = (
            "### LATEST TOOL RESULTS RECEIVED.\n"
            "INSTRUCTION: Evaluate the latest tool results in the transcript above and continue the task. "
            "If more tools are needed, emit <tool_call> tags. If you have enough information, "
            "respond clearly to the user without repeating prior summaries."
        )
    elif last_role == "user" and last_msg is not None:
        footer_section = (
            f"### LATEST USER REQUEST TO ANSWER:\nUser:\n{last_user_text}\n\n"
            "INSTRUCTION: Respond directly and specifically to the LATEST USER REQUEST above. "
            "Do NOT repeat previous architectural summaries, code reviews, or overview boilerplate unless explicitly asked."
        )
    else:
        footer_section = "Continue the conversation from the latest message."

    base_overhead_text = "\n\n".join(s.strip() for s in sections if s and s.strip())
    max_footer_chars = max(8000, max_prompt_chars - len(base_overhead_text) - 1024)
    max_footer_bytes = max(8000, max_prompt_bytes - len(base_overhead_text.encode("utf-8")) - 1024)
    if not _within_prompt_budget(footer_section, max_footer_chars, max_footer_bytes):
        footer_section = _truncate_middle_to_budget(footer_section, max_footer_chars, max_footer_bytes)
        if len(history_messages) == 1 and transcript_items:
            transcript_items = []

    fixed_overhead = "\n\n".join(s.strip() for s in [*sections, footer_section] if s and s.strip())
    transcript = _fit_transcript_to_budget(
        transcript_items,
        fixed_overhead,
        max_prompt_chars=max_prompt_chars,
        max_prompt_bytes=max_prompt_bytes,
    )
    if transcript:
        sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))
    sections.append(footer_section)
    return "\n\n".join(s.strip() for s in sections if s and s.strip())


def _messages_match_prefix(history: Sequence[dict[str, Any]], incoming: Sequence[dict[str, Any]]) -> bool:
    """Return True if incoming messages strictly extend history as a continuation."""
    if not history or len(incoming) <= len(history):
        return False
    for i, h_msg in enumerate(history):
        inc_msg = incoming[i]
        if not isinstance(inc_msg, dict) or not isinstance(h_msg, dict):
            return False
        if inc_msg.get("role") != h_msg.get("role"):
            return False
        if inc_msg.get("content") != h_msg.get("content"):
            return False
        if inc_msg.get("tool_calls") != h_msg.get("tool_calls"):
            return False
    return True


def _format_delta_prompt(
    new_messages: Sequence[dict[str, Any]],
    max_prompt_chars: int = _MAX_PROMPT_CHARS,
    max_prompt_bytes: int = _MAX_PROMPT_BYTES,
) -> str:
    """Format only the incremental messages in an ongoing multi-turn interaction."""
    parts: list[str] = []
    last_role = ""
    for msg in new_messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        last_role = role
        rendered_content = _render_message_content(msg.get("content"))
        if role == "tool":
            tool_id = str(msg.get("tool_call_id") or msg.get("name") or "tool").strip()
            if len(rendered_content) > 20000:
                rendered_content = (
                    rendered_content[:12000]
                    + f"\n[... large tool output truncated: {len(rendered_content)} chars ...]\n"
                    + rendered_content[-3000:]
                )
            parts.append(f"Tool Result ({tool_id}):\n{rendered_content}")
            continue
        if role == "assistant":
            subparts = []
            if "Tool Result (" in rendered_content:
                rendered_content = rendered_content.split("Tool Result (")[0].strip()
            if rendered_content:
                subparts.append(rendered_content)
            if tool_calls := msg.get("tool_calls"):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function") or {}
                        call_obj = {
                            "id": tc.get("id") or "call_1",
                            "type": "function",
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": fn.get("arguments", "{}") if isinstance(fn.get("arguments"), str) else json.dumps(fn.get("arguments", {}), ensure_ascii=False)
                            }
                        }
                        subparts.append(f"<tool_call>{json.dumps(call_obj, ensure_ascii=False)}</tool_call>")
            if subparts:
                parts.append("Assistant:\n" + "\n".join(subparts))
            continue
        label = _ROLE_LABELS.get(role, "Context")
        if rendered_content:
            parts.append(f"{label}:\n{rendered_content}")

    if last_role == "tool":
        parts.append("Continue the conversation from the latest tool result.")
    elif last_role == "user":
        parts.append(
            "Respond directly and specifically to the latest user request above. "
            "Do NOT repeat previous architectural summaries or boilerplate."
        )
    else:
        parts.append("Continue the conversation from the latest message above.")
    return "\n\n".join(s.strip() for s in parts if s and s.strip())


def _longest_tool_call_prefix_match(text: str) -> int:
    """Return the length of the longest suffix of `text` that matches a prefix of `<tool_call>`."""
    for prefix in _TOOL_CALL_PREFIXES:
        if text.endswith(prefix):
            return len(prefix)
    return 0


def _parse_tool_block(block: str) -> tuple[list[Any], str]:
    """Parse tool call blocks, returning (tool_call_deltas, cleaned_text)."""
    calls = []
    cleaned_text = ""
    try:
        from agent.acp_openai_bridge import extract_tool_calls_from_text
        calls, cleaned_text = extract_tool_calls_from_text(block)
    except Exception:
        m = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", block, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(1))
                fn = obj.get("function", {})
                call_id = obj.get("id") or "call_1"
                fn_name = fn.get("name", "")
                fn_args = fn.get("arguments", "{}")
                if not isinstance(fn_args, str):
                    fn_args = json.dumps(fn_args, ensure_ascii=False)
                calls = [
                    SimpleNamespace(
                        id=call_id,
                        type="function",
                        function=SimpleNamespace(name=fn_name, arguments=fn_args),
                    )
                ]
                cleaned_text = block[:m.start()] + block[m.end():]
            except Exception:
                cleaned_text = block
        else:
            cleaned_text = block

    deltas = []
    for i, call in enumerate(calls):
        fn = getattr(call, "function", None)
        name = getattr(fn, "name", "") if fn else ""
        args = getattr(fn, "arguments", "{}") if fn else "{}"
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        deltas.append(
            SimpleNamespace(
                index=i,
                id=getattr(call, "id", f"call_{i+1}"),
                type="function",
                function=SimpleNamespace(name=name, arguments=args),
            )
        )
    return deltas, cleaned_text.strip()
