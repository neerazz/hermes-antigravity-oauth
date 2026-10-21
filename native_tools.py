"""Translate agy-native tool steps into Hermes tool calls.

Some Antigravity models (Claude in particular) ignore the ``<tool_call>`` text protocol and call agy's
built-in tools such as ``run_command``. The plugin never lets agy execute those on the host. Before
this module, the step was killed and the turn came back empty. Now the intent is re-issued as the
equivalent Hermes tool call, so Hermes' own tool, approval and sandbox layer runs it. Unmappable
native tools are still neutralized as before.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable

Mapper = Callable[[dict[str, Any]], dict[str, Any] | None]


def _pick(params: dict[str, Any], *names: str) -> Any:
    lowered = {str(k).lower(): v for k, v in params.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v not in (None, ""):
            return v
    return None


def _terminal(p: dict[str, Any]) -> dict[str, Any] | None:
    cmd = _pick(p, "CommandLine", "command", "cmd")
    if not cmd:
        return None
    out: dict[str, Any] = {"command": str(cmd)}
    if cwd := _pick(p, "Cwd", "cwd", "WorkingDirectory"):
        out["workdir"] = str(cwd)
    return out


def _read_file(p: dict[str, Any]) -> dict[str, Any] | None:
    path = _pick(p, "AbsolutePath", "path", "file_path", "FilePath")
    if not path:
        return None
    out: dict[str, Any] = {"path": str(path)}
    if (start := _pick(p, "StartLine", "offset")) is not None:
        try:
            out["offset"] = max(1, int(start))
        except (TypeError, ValueError):
            pass
    return out


def _write_file(p: dict[str, Any]) -> dict[str, Any] | None:
    path = _pick(p, "TargetFile", "path", "AbsolutePath")
    content = _pick(p, "CodeContent", "content", "Content")
    return {"path": str(path), "content": str(content)} if path and content is not None else None


def _search_content(p: dict[str, Any]) -> dict[str, Any] | None:
    pattern = _pick(p, "Query", "pattern", "SearchPattern")
    if not pattern:
        return None
    out: dict[str, Any] = {"pattern": str(pattern), "target": "content"}
    if path := _pick(p, "SearchPath", "path", "Directory"):
        out["path"] = str(path)
    return out


def _search_files(p: dict[str, Any]) -> dict[str, Any] | None:
    pattern = _pick(p, "Pattern", "pattern", "Query")
    if not pattern:
        return None
    out: dict[str, Any] = {"pattern": str(pattern), "target": "files"}
    if path := _pick(p, "SearchDirectory", "path", "DirectoryPath"):
        out["path"] = str(path)
    return out


def _web_search(p: dict[str, Any]) -> dict[str, Any] | None:
    q = _pick(p, "query", "Query")
    return {"query": str(q)} if q else None


def _web_extract(p: dict[str, Any]) -> dict[str, Any] | None:
    url = _pick(p, "Url", "url")
    return {"urls": [str(url)]} if url else None


# agy tool name -> (Hermes tool name, argument mapper)
NATIVE_TO_HERMES: dict[str, tuple[str, Mapper]] = {
    "run_command": ("terminal", _terminal),
    "view_file": ("read_file", _read_file),
    "write_to_file": ("write_file", _write_file),
    "grep_search": ("search_files", _search_content),
    "find_by_name": ("search_files", _search_files),
    "search_web": ("web_search", _web_search),
    "read_url_content": ("web_extract", _web_extract),
}


def tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names = set()
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.add(str(fn["name"]))
    return names


# agy's stream-json step events carry a display preview of tool parameters: string values longer
# than 512 characters are cut to 512 and end with "…". Executing such a preview would run half a
# command (an unterminated quote at best). The full arguments are in the conversation transcript
# that agy writes under <gemini dir>/brain/<conversation_id>/.system_generated/logs/.
PREVIEW_LIMIT = 512
ELLIPSIS = "\u2026"
TRANSCRIPT_WAIT_SECONDS = 2.0


def truncated_keys(params: dict[str, Any]) -> list[str]:
    return [k for k, v in params.items()
            if isinstance(v, str) and v.endswith(ELLIPSIS) and len(v) >= PREVIEW_LIMIT]


def _transcript_files(gemini_dirs: Iterable[Path], conversation_id: str) -> list[Path]:
    rel = Path("brain") / conversation_id / ".system_generated" / "logs" / "transcript_full.jsonl"
    return [Path(d) / rel for d in gemini_dirs if d]


def restore_truncated(params: dict[str, Any], native: str, conversation_id: str,
                      gemini_dirs: Iterable[Path], wait: float = TRANSCRIPT_WAIT_SECONDS) -> dict[str, Any] | None:
    """Full parameters for a step whose preview was cut, or None when they cannot be proven.

    Matches the newest transcript tool call of the same tool whose every truncated value starts
    with the preview (minus the ellipsis). The transcript can lag the stream by a moment."""
    keys = truncated_keys(params)
    if not keys:
        return params
    if not conversation_id:
        return None
    files = _transcript_files(list(gemini_dirs), conversation_id)
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                for call in reversed(entry.get("tool_calls") or []):
                    if not isinstance(call, dict) or call.get("name") != native:
                        continue
                    args = call.get("args")
                    if not isinstance(args, dict):
                        continue
                    if all(isinstance(args.get(k), str) and args[k].startswith(params[k][:-1]) for k in keys):
                        merged = dict(params)
                        for k in keys:
                            merged[k] = args[k]
                        return merged
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def translate(step: dict[str, Any], available: set[str], index: int,
              resolve: Callable[[dict[str, Any], str], dict[str, Any] | None] | None = None) -> SimpleNamespace | None:
    """Return a Hermes tool-call delta for an agy native tool step, or None if it cannot be mapped."""
    native = str(step.get("tool_name") or (step.get("tool_info") or {}).get("name") or "")
    mapping = NATIVE_TO_HERMES.get(native)
    if not mapping:
        return None
    hermes_name, mapper = mapping
    if hermes_name not in available:
        return None
    params = (step.get("tool_info") or {}).get("parameters")
    if not isinstance(params, dict):
        return None
    if truncated_keys(params):
        # Never hand Hermes a cut command: restore the full text or refuse to translate.
        params = resolve(params, native) if resolve else None
        if params is None:
            return None
    args = mapper(params)
    if args is None:
        return None
    ident = f"agy_{step.get('step_index', index)}_{native}"
    return SimpleNamespace(index=index, id=ident, type="function",
                           function=SimpleNamespace(name=hermes_name, arguments=json.dumps(args, ensure_ascii=False)))
