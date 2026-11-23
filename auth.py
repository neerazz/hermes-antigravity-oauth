"""`hermes auth add|status|logout|refresh antigravity-oauth`, delegated to the official agy CLI.

Google owns this OAuth flow end to end. `add` runs agy's own browser sign-in (it prints a URL and
accepts a pasted code over SSH), then confirms the session with a live `agy models` call. The plugin
never reads, copies, or stores the token: agy keeps it in the OS keychain or its own token file.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any

try:
    from .process import is_authenticated, resolve_agy_command
except ImportError:  # flat source tree under test
    from process import is_authenticated, resolve_agy_command

PROVIDER_ID = "antigravity-oauth"
INSTALL_URL = "https://antigravity.google/cli"
# A one-word print-mode turn. With no saved session, agy starts its Google OAuth flow first and
# then answers, so one command both signs in and proves the session can serve a model.
_LOGIN_PROMPT = "Reply with exactly: OK"
_VERIFY_TIMEOUT_S = 45


def _agy_path() -> str | None:
    cmd = resolve_agy_command()
    return cmd if os.path.isabs(cmd) else shutil.which(cmd)


def login_command(agy: str) -> list[str]:
    return [agy, "-p", _LOGIN_PROMPT, "--print-timeout", "5m"]


def verify_session(agy: str, timeout: float = _VERIFY_TIMEOUT_S) -> tuple[bool, list[str], str]:
    """Live check: `agy models` only succeeds for a signed-in account. Returns (ok, model_ids, detail)."""
    try:
        res = subprocess.run([agy, "models"], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL, check=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        return False, [], f"`agy models` timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, [], f"could not run agy: {exc}"
    models = [ln.split()[0] for ln in res.stdout.splitlines()
              if ln.strip() and "fetching" not in ln.lower()]
    if res.returncode == 0 and models:
        return True, models, f"{len(models)} models available"
    err = (res.stderr or res.stdout).strip().splitlines()
    return False, [], (err[-1] if err else f"agy models exited {res.returncode}")


def probe(*, live: bool = False) -> dict[str, Any]:
    """Shape consumed by Hermes' `hermes model` setup gate (`ProviderProfile.setup_status`)."""
    agy = _agy_path()
    if not agy:
        return {"available": False, "logged_in": False, "plan": "", "login_command": None,
                "detail": f"Antigravity CLI (agy) not found. Install it from {INSTALL_URL} "
                          "or run `npx hermes-antigravity-oauth --yes`."}
    logged_in, detail = is_authenticated(), ""
    if live or logged_in:
        logged_in, _, detail = verify_session(agy)
    if not logged_in:
        detail = f"Not signed in to Antigravity ({detail or 'no saved agy session'}). Run `hermes auth add {PROVIDER_ID}`."
    return {"available": True, "logged_in": logged_in, "plan": "", "command": agy,
            "login_command": login_command(agy), "detail": detail or "signed in"}


def _run_login(agy: str) -> int:
    # Private cwd: agy must not pick up the caller's repo as its workspace during sign-in.
    with tempfile.TemporaryDirectory(prefix="hermes-agy-login-") as cwd:
        try:
            return subprocess.run(login_command(agy), cwd=cwd, check=False).returncode
        except KeyboardInterrupt:
            return 130


def _add(args: Any) -> None:
    status = probe()
    if not status["available"]:
        raise SystemExit(status["detail"])
    agy = status["command"]
    if status["logged_in"]:
        print(f"{PROVIDER_ID}: already signed in via agy ({status['detail']}).")
        return
    if not sys.stdin.isatty():
        raise SystemExit(f"{status['detail']}\nSign-in needs an interactive terminal: run `{shlex.join(login_command(agy))}`.")
    print("Starting Google sign-in through the Antigravity CLI.")
    print("A browser window opens; over SSH, open the printed URL and paste the code back here.\n")
    rc = _run_login(agy)
    ok, models, detail = verify_session(agy)
    if not ok:
        raise SystemExit(f"Sign-in did not complete (agy exit {rc}; {detail}).")
    print(f"\n{PROVIDER_ID}: signed in. {detail}.")
    print(f"Use it: hermes --provider {PROVIDER_ID} -m {_base_model(models[0])}")


def _base_model(model_id: str) -> str:
    for suffix in ("-high", "-medium", "-low"):
        if model_id.startswith("gemini-") and model_id.endswith(suffix):
            return model_id[: -len(suffix)]
    return model_id


def _status(args: Any) -> None:
    status = probe(live=True)
    state = "logged in" if status["logged_in"] else "logged out"
    print(f"{PROVIDER_ID}: {state}")
    print(f"  auth_type: external_process (session owned by agy)")
    if status.get("command"):
        print(f"  command: {status['command']}")
    print(f"  detail: {status['detail']}")


def _logout(args: Any) -> None:
    # agy's credential store is Google's; removing it is agy's job, not this plugin's.
    print(f"{PROVIDER_ID}: the session belongs to the Antigravity CLI.")
    print("To sign out, run `agy`, type /logout, then exit. Hermes stores no token for this provider.")


def _refresh(args: Any) -> None:
    ok, _, detail = verify_session(_agy_path() or "agy")
    print(f"{PROVIDER_ID}: agy refreshes its own token; live check {'passed' if ok else 'failed'} ({detail}).")


_ACTIONS = {"add": _add, "status": _status, "logout": _logout, "refresh": _refresh}


def antigravity_auth_handler(action: str, args: Any) -> bool:
    """`ProviderProfile.auth_handler`: truthy means this plugin owned the action."""
    fn = _ACTIONS.get(action)
    if fn is None:
        return False
    fn(args)
    return True
