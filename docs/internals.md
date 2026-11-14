# Internals

How the plugin picks model IDs, reports context limits, and shows live reasoning. You do not need any of this to install or use the plugin; start with the [README](../README.md).

## Live model IDs and reasoning

The model picker uses only structurally valid ID/name rows from a successful
`agy models` call. It preserves exact IDs, case, order, model families and native
effort variants, deduplicating only identical IDs. Failed, timed-out or incomplete
discovery returns no catalog; there is no hardcoded fallback list or recommended
auxiliary model. A successful empty response remains distinct from failure.
Discovery is bounded and noninteractive; use the auth commands above to sign in.

Selected IDs are passed verbatim to `--model` in both worker and one-shot calls.
Native effort suffixes own their effort and do not expose a second reasoning
control; contradictory direct or inherited effort settings cannot rewrite them.
For a manually entered suffixless `gemini-*` ID, the separate CLI `--effort` flag
accepts `low`, `medium`, `high`, `xhigh` and `max` without generating a model ID or
downgrading `max`. Claude and GPT-OSS get no separate flag. Generation-pinned
aliases and implicit plugin model/effort defaults are not used.

## Context metadata and serving limits

`ANTIGRAVITY_CONTEXT_LENGTH` accepts a positive integer token budget as an explicit
user override. When unset, invalid or nonpositive, the documented
`ProviderProfile.get_model_context_length` hook returns `None`, letting Hermes
resolve the **exact selected ID** through its native metadata. The plugin imposes
no blanket context capacity and imports no private metadata resolver.

Metadata is not proof of the maximum context served by your account or the CLI.
Unrecognized exact IDs can receive Hermes' generic unknown-model fallback: for
example, `gpt-oss-120b-medium` is currently unresolved by the native resolver.
That fallback is **unknown metadata, not native model capacity**. If necessary,
set an explicit conservative budget for your route. This change does not establish
million-token account acceptance; serving limits require separate live testing.

## Live reasoning on non-streaming Hermes turns

`external_process` authentication remains unchanged: Hermes selects its normal
non-streaming path. For foreground agents with an installed reasoning callback
and a text-stream consumer, the plugin's collector forwards available reasoning
while it assembles the response. It calls the existing callback, preserving the
TUI's show/hide and notification-mute gates. Reasoning remains separate from
answer/tool content and is never added to the replay prompt.

The profile's main-request projection installs `llm_execution` middleware using
`PluginContext.register_middleware`. This explicit bootstrap is necessary because
Hermes' provider discovery imports model-provider plugins but skips their general
`register(ctx)` lifecycle. Registration is profile-local, idempotent, and uses an
unloadable host registration handle. Auxiliary projections carry no session ID.

A context-local, single-collector lease fences delivery by profile, provider,
model, session, turn, request, current callback identity and client liveness.
Cancellation, closing sessions, request replacement and middleware exit disable
late delivery. Missing host compatibility seams disable only the optional live
presentation; completed reasoning and usage remain available. Streaming callers
continue to receive ordinary chunks, with no extra callback delivery.

This uses read-only host internals for current-agent/session lookup; compatibility
is tested against the installed Hermes checkout, not guaranteed for all releases.
Already running processes retain their previously loaded plugin code; this plugin
does not hot-patch them. No daemon restart or configuration change is performed.
Agy may return no thinking text even when a request otherwise succeeds. Live
history restoration and actual frontend rendering are separate from delivery.

The opt-in `scripts/verify_live_reasoning.py` makes one bounded, tool-free call to
bare `gemini-4-argon` using the existing account. It exercises real AIAgent kwargs,
the unchanged `_should_stream=False` decision, middleware, the nonstream worker,
agy/DB reader, collector, and installed TUI reasoning callback. Only UI egress is
captured; it does not prove display in an existing desktop window. Its receipt
records counts/times, never reasoning text. See [`VERIFICATION.md`](../VERIFICATION.md).
