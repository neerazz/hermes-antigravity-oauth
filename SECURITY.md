# Security policy
Report suspected vulnerabilities privately. Do not put exploit details or credentials in public issues or pull requests.

## Reporting a vulnerability
If the repository's Security tab offers **Report a vulnerability**, use its GitHub private advisory form. Otherwise, use a contact channel explicitly listed on [the maintainer's GitHub profile](https://github.com/neerazz) to request a private reporting route. If none is listed, do not publish sensitive details while seeking a private channel. Private reporting availability and response times are not guaranteed here.

Include the affected package version or commit, OS, Hermes/agy versions, impact, and minimal reproduction with synthetic data. Describe expected and actual behavior. Never send tokens, OAuth codes, private prompts, conversation databases, or unredacted logs. If a credential leaked, revoke it through the official provider before sharing evidence.

## Credential and execution boundaries
- Google's official `agy` CLI owns OAuth sign-in, token refresh, and credential storage. The plugin delegates authentication; it does not implement Google's OAuth flow.
- The isolated HOME can expose agy credentials through a symlink or hard link. If both fail, the fallback copies the credential file without parsing it. macOS keychains may also be linked. Do not assume credential material is never copied.
- Temporary workspaces separate project state; they are not an OS sandbox. The agy child runs with the user's privileges, and prompts and workspace rules cross into Google's CLI/service.
- Hermes tool approvals and the plugin's tool bridge are separate controls. Do not assume every agy tool name or version is supported, or that a workspace alone prevents execution.

See [the architecture/source map](docs/architecture.md) for implementation boundaries. Review changes to subprocess arguments, credential paths, workspace cleanup, and tool mapping as security-sensitive.

## Fix scope
Reports should identify the latest release and, where practical, whether `main` is affected. No older-version maintenance or security response SLA is promised. Coordinate public disclosure with the maintainer after a fix is available.
