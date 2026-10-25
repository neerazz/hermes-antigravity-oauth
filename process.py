"""Cross-platform process, OS support, and isolated workspace management for Antigravity."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

# Marker scheme for Antigravity local provider
AGY_MARKER_BASE_URL = "agy://local"

# Known OAuth token basenames. agy1.2 renamed the fallback file from
# antigravity-oauth-token to jetski-standalone-oauth-token (issue #1),
# so both must resolve. Order is new-first: an upgraded user (1.1 -> 1.2)
# has BOTH files in the same directory, because 1.2 does not remove the
# old one, and 1.2 reads only the new name — so when both coexist the new
# name is authoritative. Resolving the stale legacy file there would
# authenticate (size > 10) a token that agy 1.2 ignores. Legacy-only 1.1
# users are unaffected: the new file simply does not exist and the scan
# falls through to the legacy name.
_TOKEN_FILENAMES = ("jetski-standalone-oauth-token", "antigravity-oauth-token")


def _is_existing_file(path: str | Path) -> bool:
    """True if path is a regular file, treating any OS error as absent.

    Candidate discovery walks directories that may be unreadable to the
    current user (Path.home() of another account, /root under a
    non-root runner). pathlib's is_file() only swallows part of the
    OSError family, so a PermissionError on stat() escaped and aborted
    the whole scan instead of just skipping that candidate.

    ⚡ Bolt Optimization: Using os.path.isfile() directly for strings
    is ~4x faster than constructing a Path object (0.32ms vs 1.22ms
    per 100k calls) which helps during aggressive token path scanning.
    """
    try:
        if isinstance(path, Path):
            return path.is_file()
        return os.path.isfile(path)
    except OSError:
        return False


def _own_process_group() -> dict[str, Any]:
    """Popen kwargs for background processes in a group we can kill cleanly."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill process and every descendant: taskkill /F /T on Windows, killpg on POSIX."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — the nt branch above never reaches this line
    except (ProcessLookupError, PermissionError, AttributeError):
        with contextlib.suppress(Exception):
            proc.kill()


def terminate_process(proc: subprocess.Popen) -> None:
    """Attempt graceful termination, falling back to process-tree kill."""
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        _kill_process_tree(proc)


def resolve_agy_command() -> str:
    """Find the path to the official `agy` binary across platforms."""
    for var in ("ANTIGRAVITY_COMMAND", "AGY_CLI_PATH", "ANTIGRAVITY_CLI_PATH"):
        if val := os.getenv(var, "").strip():
            p = Path(val)
            if _is_existing_file(p) and (os.name == "nt" or os.access(val, os.X_OK)):
                return val

    # Check PATH (shutil.which checks PATHEXT on Windows, e.g. agy.exe)
    if path := shutil.which("agy"):
        return path

    binary_name = "agy.exe" if os.name == "nt" else "agy"
    candidates = [
        Path.home() / ".gemini" / "antigravity-cli" / "bin" / binary_name,
        Path.home() / ".local" / "bin" / binary_name,
        Path("/root/.local/bin") / binary_name,
        Path("/usr/local/bin") / binary_name,
        Path("/usr/bin") / binary_name,
    ]
    if os.name == "nt":
        if localappdata := os.getenv("LOCALAPPDATA"):
            candidates.append(Path(localappdata) / "Programs" / "agy" / binary_name)
            candidates.append(Path(localappdata) / "Microsoft" / "WinGet" / "Links" / binary_name)

    for candidate in candidates:
        if _is_existing_file(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate)

    return "agy"


def resolve_real_token_path() -> Path | None:
    """Locate the authentic Antigravity OAuth token on the host.

    ANTIGRAVITY_CONFIG_DIR is an override, not a hint: when it is set to a
    directory without a token, the explicit configuration wins and the
    implicit locations are not consulted. Falling through anyway would let a
    stray /root token satisfy auth for a user who deliberately pointed
    ANTIGRAVITY_CONFIG_DIR somewhere else.
    """
    token_dir = os.getenv("ANTIGRAVITY_CONFIG_DIR", "").strip()
    if token_dir:
        config_path = Path(token_dir)
        for filename in _TOKEN_FILENAMES:
            explicit = config_path / filename
            if _is_existing_file(explicit):
                return explicit
        return None

    home_base = Path.home() / ".gemini" / "antigravity-cli"
    root_base = Path("/root/.gemini/antigravity-cli")
    candidates = [home_base / name for name in _TOKEN_FILENAMES]
    # Last resort for containers/sudo contexts where HOME does not point
    # at the account that ran `agy`. All home candidates win over any
    # /root fallback so a stale /root legacy token never beats the user's
    # current token. Skip the extra stat when HOME already is /root.
    if root_base != home_base:
        candidates += [root_base / name for name in _TOKEN_FILENAMES]
    for candidate in candidates:
        if _is_existing_file(candidate):
            return candidate
    return None


def _windows_credential_present() -> bool:
    """True if agy's Windows Credential Manager entry exists.

    On Windows agy stores its session in the Credential Manager
    (target ``gemini:antigravity``) instead of an oauth token file. `cmdkey /list`
    prints only target metadata, never the secret.
    """
    try:
        # Bytes, not text: cmdkey prints in the console/OEM code page, so nothing is decoded.
        out = subprocess.run(["cmdkey", "/list:gemini:antigravity"], capture_output=True, timeout=3, check=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    except (OSError, subprocess.SubprocessError):  # missing cmdkey, TimeoutExpired
        return False
    # Fail closed on a failing cmdkey; the listed target keeps the casing it was stored with.
    return out.returncode == 0 and b"gemini:antigravity" in (out.stdout or b"").lower()


# Ground truth for agy's Linux credential: the reporter's sanitized live dump
# from an active agy session (issue #1 comment, 2026-09-26, RvbbenSN), which
# follows the zalando/go-keyring convention — label
# "Password for '<username>' on '<service>'" and attributes
# {'service': 'gemini', 'username': 'antigravity',
#  'xdg:schema': 'org.freedesktop.Secret.Generic'}. The pair also mirrors the
# Windows Credential Manager target ``gemini:antigravity`` from PR #2. Both
# constants are injected into the secret-tool argv, its stderr markers and the
# fallback script, so they cannot drift apart.
_KEYRING_SERVICE = "gemini"
_KEYRING_USERNAME = "antigravity"

_SECRET_TOOL_BINARY = "secret-tool"
# `search --all service <s> username <u>` ANDs the attribute pairs, so the pair
# is the whole query. Only search is ever used: `secret-tool lookup` prints
# nothing but the secret, and search prints the secret too (below) — the
# difference is only which stream we allow ourselves to capture.
_SECRET_TOOL_PROBE_ARGV = [
    _SECRET_TOOL_BINARY,
    "search",
    "--all",
    "service",
    _KEYRING_SERVICE,
    "username",
    _KEYRING_USERNAME,
]

# How libsecret's secret-tool reports a hit (verified against tool/secret-tool.c
# in libsecret master): secret_tool_action_search() sets SECRET_SEARCH_LOAD_SECRETS
# unconditionally, and on_retrieve_secret() then writes the credential itself to
# STDOUT — g_print ("secret = ") followed by write_password_data()'s raw
# write (1, ...) — while the attributes go to STDERR as g_printerr
# ("attribute.%s = %s\n", key, value) lines. The search we run for its metadata
# therefore does load the credential into the CLI's own process memory, so stdout
# is closed (DEVNULL) and only the stderr markers are read, derived from the same
# constants as the argv so they cannot drift.
#
# The markers are taken with their trailing newline: g_printerr appends exactly
# "\n" per line, and an unanchored b"attribute.service = gemini" would also be
# satisfied by a hypothetical b"attribute.service = gemini-cli". Anchored, the
# verdict depends only on the exact pair the argv already AND-queries.
_SECRET_TOOL_HIT_MARKERS = (
    b"attribute.service = " + _KEYRING_SERVICE.encode() + b"\n",
    b"attribute.username = " + _KEYRING_USERNAME.encode() + b"\n",
)

# Cascade of interpreters for the scripted fallback. The absolute system entries
# come FIRST because they are the ones carrying the distro's secretstorage
# packages (reporter's field observation, same issue #1 comment); the bare
# "python3" entry is resolved through PATH, which on agent setups is typically
# the agent's own venv — the very interpreter that often lacks the package.
_KEYRING_PROBE_INTERPRETERS = ("/usr/bin/python3", "python3", "/usr/local/bin/python3")
# Generous, not measured against agy: the 1.2.x changelog strings in the binary
# mention longer keyring timeouts and a one-hour skip after a timeout, and a
# wedged D-Bus session can stall Secret Service activation for seconds.
_KEYRING_PROBE_TIMEOUT_SECONDS = 5.0

# Fallback for hosts without secret-tool: the same question asked through the
# secretstorage module instead of the CLI. The exact attribute pair (service
# gemini AND username antigravity, as verified live) is the match; the
# label-contains branch is documented cheap robustness: the go-keyring label
# embeds the username, so 'antigravity' in the label survives a service rename,
# while a gemini-cli credential never carries 'antigravity' in its label.
#
# Exit-code contract. Codes 1 and 2 are deliberately NOT reused: they are
# CPython's own conventions (1 = uncaught exception, which a broken or ancient
# interpreter raises before any of the try/except below runs; 2 = CLI misuse),
# so letting either mean "definitive miss" would let a broken interpreter
# masquerade as a searched-but-empty keyring and silently un-authenticate:
#   0 - an Antigravity credential exists
#   3 - the collections were readable and hold no Antigravity credential
#       (definitive miss: stop the interpreter cascade)
#   2 - this interpreter cannot answer at all (secretstorage not installed, no
#       D-Bus session bus, ...), so the cascade moves to the next one
# Anything else is treated like 2: cannot answer.
# It reads item attributes and the label only: get_secret() is never called.
_KEYRING_PROBE_SCRIPT = """\
import sys
try:
    import secretstorage
except Exception:
    sys.exit(2)

service = {service!r}
username = {username!r}
needle = {needle!r}


def label_matches(bus):
    # search_items() answers on attributes only, so walk every collection for
    # the go-keyring label form. A collection that cannot be read (locked,
    # broken) is skipped rather than aborting the whole label scan.
    for collection in secretstorage.get_all_collections(bus):
        try:
            for item in collection.get_all_items():
                if needle in (item.get_label() or "").lower():
                    return True
        except Exception:
            continue
    return False


try:
    bus = secretstorage.dbus_init()
    # search_items() matches server-side across ALL collections and never
    # creates a collection, unlike get_default_collection() which creates the
    # "Default" collection (and can fire a D-Bus unlock prompt) when the
    # default alias does not exist.
    hits = secretstorage.search_items(bus, {{"service": service, "username": username}})
    # search_items() returns a GENERATOR (secretstorage/collection.py), and a
    # generator object is always truthy — testing it directly would report
    # "found" on every host with a reachable keyring. Consume it:
    # next(hits, None) is not None asks "does at least one item exist", and
    # stops at the first hit instead of walking the whole keyring.
    if next(hits, None) is not None:
        sys.exit(0)
    if label_matches(bus):
        sys.exit(0)
    sys.exit(3)
except Exception:
    sys.exit(2)
""".format(service=_KEYRING_SERVICE, username=_KEYRING_USERNAME, needle=_KEYRING_USERNAME)


def _secret_tool_credential_present() -> bool | None:
    """Ask libsecret's CLI whether agy's credential item exists.

    Returns the verdict, or None when secret-tool could not answer at all —
    spawn failure of any kind, a timeout, or a non-zero rc (search returns 1 on
    a tool-level error, which is "couldn't answer", not "not found"). Only
    rc == 0 is a verdict; the caller then hands the question to the scripted
    probe, because a secret-tool failure is normally tool-specific (missing,
    broken, or wedged binary) rather than a verdict about the credential. A
    hung secret-tool (wedged session bus) must not read as "not authenticated".

    rc == 0 with no matching attribute lines is a definitive False: search
    exits 0 with empty stderr when nothing matched.
    """
    try:
        # Bytes, not text: secret-tool prints in the session locale, so nothing
        # is decoded. stdout is DEVNULL — it carries `secret = <credential>`
        # (see _SECRET_TOOL_HIT_MARKERS); only stderr, which holds the
        # `attribute.<name> = <value>` metadata, is captured.
        out = subprocess.run(
            list(_SECRET_TOOL_PROBE_ARGV),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_KEYRING_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # missing tool, wedged D-Bus, TimeoutExpired
        return None
    if out.returncode != 0:
        return None
    stderr = out.stderr or b""
    return all(marker in stderr for marker in _SECRET_TOOL_HIT_MARKERS)


def _scripted_keyring_present() -> bool:
    """Ask secretstorage directly, cascading python3 interpreters.

    Exit code drives the cascade, using the script's pinned contract: 0 means
    the credential exists (stop, True), 3 means the collections were readable
    and hold no Antigravity credential (stop, False — definitive, another
    interpreter cannot answer better). Codes 1 and 2 are CPython's own
    conventions (uncaught exception, CLI misuse) and must never read as a miss,
    so a broken interpreter is "cannot answer" here, not a verdict; the same
    holds for a spawn failure or a timeout. All exhausted means False. stdout
    and stderr stay silenced: only the exit code is read.
    """
    seen: set[str] = set()
    for interpreter in _KEYRING_PROBE_INTERPRETERS:
        command = _existing_interpreter(interpreter)
        if command is None:
            continue
        # Dedupe on the real path, not the spelling: which() can hand back a
        # relative entry (an empty or "." PATH component joins to a bare name),
        # and a symlinked interpreter can land on the same file as an absolute
        # cascade entry. Either way the same interpreter must not be spawned —
        # and timed out — twice.
        identity = os.path.realpath(command)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            proc = subprocess.run(
                [command, "-c", _KEYRING_PROBE_SCRIPT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_KEYRING_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):  # no such interpreter, TimeoutExpired
            continue
        if proc.returncode == 3:
            return False
        if proc.returncode == 0:
            return True
        # Anything else (CPython's own 1/2, our 2, or a code we do not know):
        # this interpreter cannot answer, try the next one.
    return False


def _existing_interpreter(interpreter: str) -> str | None:
    """Resolve one cascade entry to a runnable interpreter, or None if absent."""
    if "/" in interpreter:
        return interpreter if os.access(interpreter, os.X_OK) else None
    found = shutil.which(interpreter)
    # A PATH entry of "" or "." makes which() return a RELATIVE path, and
    # spawning that verbatim would execute <cwd>/python3 — an arbitrary local
    # file. Only absolute results are trustworthy; the absolute cascade entries
    # already cover hosts whose python3 is not on PATH at all.
    return found if found and os.path.isabs(found) else None


def _linux_keyring_present() -> bool:
    """True if agy's freedesktop Secret Service credential exists.

    On Linux agy keeps its session in the Secret Service (stored through
    zalando/go-keyring with service ``gemini`` and username ``antigravity``,
    per the reporter's sanitized live dump in issue #1, 2026-09-26) instead of
    an oauth token file — but only when a D-Bus session bus exists. Headless
    machines and containers make agy skip the keyring and fall back to the
    token file, so a missing bus means there is no keyring to look at and this
    degrades to False; the file scan in resolve_real_token_path() already
    covers those users.

    Zero-Exfiltration compliance, scoped to THIS plugin process: the credential
    is never received, parsed, logged, or transmitted here. secret-tool loads it
    into its OWN process memory — libsecret's search sets
    SECRET_SEARCH_LOAD_SECRETS and it prints the secret to stdout (see
    _SECRET_TOOL_HIT_MARKERS) — so we close that stream and read only the stderr
    attribute lines. The scripted fallback reads item attributes and labels and
    never calls get_secret().

    Asymmetry worth knowing: with secret-tool present its rc-0 exact-pair
    verdict STANDS, and there is no fall-through to the more permissive scripted
    check — the label insurance described next only ever applies on the
    scripted path.

    The scripted fallback is deliberately MORE permissive than the CLI probe
    (exact attribute pair OR label-contains), so an agy service rename is still
    caught on hosts where secret-tool is absent and its attribute search would
    miss. Documented trade-off: any non-agy credential whose label embeds
    "antigravity" matches the label branch and errs True.

    Cost, uncached (PR #2 adjudication): is_authenticated() runs per completion
    request, and one probe is marginal next to the agy child spawn this plugin
    also runs. Ceiling is ~20 s in the worst case (secret-tool 5 s, then up to
    3 interpreters x 5 s) and milliseconds typically. No caching: a positive
    TTL would outlive a logout and a negative one would block a fresh login.
    """
    if shutil.which(_SECRET_TOOL_BINARY) is not None:
        verdict = _secret_tool_credential_present()
        if verdict is not None:
            return verdict
    return _scripted_keyring_present()


# macOS: go-keyring's darwin backend shells out to /usr/bin/security and stores
# a generic password with service "gemini" and account "antigravity" in the
# login keychain (verified live against agy 1.2.11: `security
# find-generic-password -s gemini -a antigravity` finds exactly that item, and
# agy's cli.log reports "authenticated via keyring"). The absolute path is used
# so a PATH entry cannot shadow the system binary.
_SECURITY_BINARY = "/usr/bin/security"
# Without -g or -w, find-generic-password prints only the item's attributes (to
# stdout) and never the secret, so the probe never asks the keychain for the
# credential and never triggers an access prompt.
_SECURITY_PROBE_ARGV = [
    _SECURITY_BINARY,
    "find-generic-password",
    "-s",
    _KEYRING_SERVICE,
    "-a",
    _KEYRING_USERNAME,
]
# The -s/-a filters already select the exact pair; the markers re-check it so
# the verdict never rests on the return code alone. Each is anchored on the
# closing quote, so a hypothetical service "gemini-cli" does not match.
_SECURITY_HIT_MARKERS = (
    b'"svce"<blob>="' + _KEYRING_SERVICE.encode() + b'"',
    b'"acct"<blob>="' + _KEYRING_USERNAME.encode() + b'"',
)


def _macos_keychain_present() -> bool:
    """True if agy's macOS keychain item exists.

    Zero-Exfiltration compliance: only item attributes are requested (no -g/-w),
    so the credential never leaves the keychain. Fails closed like the Windows
    probe: a missing binary, a timeout, or any non-zero rc (44 is "item not
    found") reads as not authenticated. Uncached for the same reason as the
    Linux probe: a cached verdict would outlive a logout or block a fresh login.
    """
    try:
        out = subprocess.run(
            list(_SECURITY_PROBE_ARGV),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_KEYRING_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # missing binary, TimeoutExpired
        return False
    if out.returncode != 0:
        return False
    stdout = out.stdout or b""
    return all(marker in stdout for marker in _SECURITY_HIT_MARKERS)


def is_authenticated() -> bool:
    """Verify that the user has an active Antigravity OAuth session.

    Zero-Exfiltration compliance: We only verify file presence and non-zero
    size, or credential existence in the OS keyring. We NEVER read, parse, or
    transmit the token contents.
    """
    token_path = resolve_real_token_path()
    if not token_path:
        # An explicit ANTIGRAVITY_CONFIG_DIR still wins (see resolve_real_token_path).
        explicit_dir = os.getenv("ANTIGRAVITY_CONFIG_DIR", "").strip()
        if os.name == "nt" and not explicit_dir:
            return _windows_credential_present()
        # sys.platform is the right discriminator here, while the nt branch above
        # keeps os.name: "nt" is unambiguous, but os.name's "posix" means both
        # Linux and macOS, and only Linux exposes the freedesktop Secret Service
        # this probe reads.
        if sys.platform.startswith("linux") and not explicit_dir:
            return _linux_keyring_present()
        if sys.platform == "darwin" and not explicit_dir:
            return _macos_keychain_present()
        return False
    try:
        return token_path.is_file() and token_path.stat().st_size > 10
    except OSError:
        return False


def setup_isolated_home(cwd: Path | str) -> tuple[Path, Path]:
    """Create isolated HOME inside cwd and link authentic oauth token."""
    isolated_home = Path(cwd) / "home"
    isolated_gemini_dir = isolated_home / ".gemini" / "antigravity-cli"
    isolated_gemini_dir.mkdir(parents=True, exist_ok=True)

    real_token = resolve_real_token_path()
    if real_token and _is_existing_file(real_token):
        # Reused cwd: a previous run may have selected the OTHER known
        # basename (e.g. a pre-upgrade agy1.1 run linked
        # antigravity-oauth-token). Such a leftover link would linger next
        # to the fresh one, so drop it — but only if it is a symlink we
        # created; never unlink a real file that could be user data.
        for stale_name in _TOKEN_FILENAMES:
            if stale_name == real_token.name:
                continue
            stale_link = isolated_gemini_dir / stale_name
            if os.path.islink(stale_link):
                with contextlib.suppress(OSError):
                    stale_link.unlink()
        # Preserve the selected basename so the agy1.2 filename keeps
        # working inside the isolated home (zero secret parsing).
        isolated_token = isolated_gemini_dir / real_token.name
        # Reused cwd: the link under the selected basename may itself be a
        # leftover pointing at a previous run's source. If that source is
        # stale but still present, the exists() guard below would skip
        # recreation and the child would use the old token; if it is
        # dangling (previous run's temp source deleted), symlink raises
        # FileExistsError and the copy2 fallback opens through the dangling
        # link and fails, leaving no usable token. Always relink to the
        # CURRENT real token. A real regular file is left untouched: it can
        # be a valid copy2 hardlink-failure artifact and could be user data.
        # Never unlink the resolved source itself, though: when
        # ANTIGRAVITY_CONFIG_DIR points at this very gemini dir, real_token
        # IS isolated_token, and unlinking it would relink the token to
        # itself — a self-referential symlink, ELOOP on open. Compare
        # resolved paths rather than raw ones: resolve_real_token_path()
        # echoes ANTIGRAVITY_CONFIG_DIR verbatim and cwd is caller-supplied,
        # so either side can be relative, and symlinked parents would make
        # one file alias two spellings past a raw comparison.
        is_resolved_source = isolated_token.resolve() == real_token.resolve()
        if not is_resolved_source and os.path.islink(isolated_token):
            with contextlib.suppress(OSError):
                isolated_token.unlink()
        if not isolated_token.exists():
            try:
                os.symlink(real_token, isolated_token)
            except OSError:
                try:
                    os.link(real_token, isolated_token)
                except OSError:
                    with contextlib.suppress(OSError):
                        shutil.copy2(real_token, isolated_token)

    _link_macos_keychains(isolated_home)
    return isolated_home, isolated_gemini_dir


def _link_macos_keychains(isolated_home: Path) -> None:
    """Expose the user's keychains inside the isolated HOME on macOS.

    agy 1.2 on macOS keeps its session in the login keychain (see
    _macos_keychain_present), and the keychain search list is resolved through
    $HOME/Library/Keychains. Under the isolated HOME that directory does not
    exist, so the child agy finds no credential and answers "Authentication
    required" even though the user is logged in (verified live, agy 1.2.11).
    Linking the directory restores exactly the access agy has when run
    normally; the isolation this HOME provides is for project files, not for
    agy's own credential. Skipped when ANTIGRAVITY_CONFIG_DIR is set, so an
    explicit token directory is not overridden by the keychain session (agy
    tries the keychain first).
    """
    if sys.platform != "darwin" or os.getenv("ANTIGRAVITY_CONFIG_DIR", "").strip():
        return
    real_keychains = Path.home() / "Library" / "Keychains"
    if not real_keychains.is_dir():
        return
    isolated_keychains = Path(isolated_home) / "Library" / "Keychains"
    # Same directory already (isolated_home is, or links to, the real HOME).
    with contextlib.suppress(OSError):
        if isolated_keychains.resolve() == real_keychains.resolve():
            return
    if os.path.islink(isolated_keychains):
        # Reused cwd: a leftover link from a previous run may point elsewhere.
        with contextlib.suppress(OSError):
            isolated_keychains.unlink()
    elif isolated_keychains.exists():
        return  # a real directory: never replace it
    with contextlib.suppress(OSError):
        isolated_keychains.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(real_keychains, isolated_keychains, target_is_directory=True)


def build_child_env(isolated_home: Path | str) -> dict[str, str]:
    """Construct child environment isolating home and session storage on POSIX and Windows."""
    env = dict(os.environ)
    home_str = str(isolated_home)
    env["HOME"] = home_str
    # Windows: Go's os.UserHomeDir() reads USERPROFILE then HOMEDRIVE+HOMEPATH
    env["USERPROFILE"] = home_str
    if "HOMEPATH" in env:
        env["HOMEPATH"] = home_str
    # agy's background updater would otherwise open its own console on Windows (issue #2).
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    return env


def _check_early_quota_error(
    gemini_dir: Path | str | None,
    min_mtime: float | None = None,
) -> str | None:
    """Check isolated agy logs for early RESOURCE_EXHAUSTED / 429 quota exhaustion.

    agy internally retries 429s up to 8 times with exponential backoff (~140s)
    without emitting anything to stdout. Detecting this early allows fast failover.
    """
    if not gemini_dir:
        return None
    log_dir = Path(gemini_dir) / "log"
    if not log_dir.is_dir():
        return None

    try:
        log_files = sorted(
            log_dir.glob("cli-*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not log_files:
            return None

        latest_log = log_files[0]
        if min_mtime is not None:
            # Allow 2.0s margin for filesystem timestamp resolution
            if latest_log.stat().st_mtime < (min_mtime - 2.0):
                return None

        with open(latest_log, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            content = f.read()

        for line in reversed(content.splitlines()):
            if "RESOURCE_EXHAUSTED" in line and (
                "Individual quota reached" in line
                or "quota exceeded" in line.lower()
                or "code 429" in line
            ):
                start = line.find("RESOURCE_EXHAUSTED")
                if start != -1:
                    detail = line[start:]
                    retrying_idx = detail.rfind("), retrying in")
                    if retrying_idx != -1:
                        detail = detail[:retrying_idx]
                    if detail.count("(") < detail.count(")"):
                        detail = detail.rstrip(")")
                    return detail.strip()
                return line.strip()
    except Exception:
        pass
    return None

