# Working on refmodBuilder

Read `docs/MAINTAINER_HANDOFF.md` and the current branch's `README.md` before making changes. Branches differ in backend support and packaging; do not assume `main` contains Mac features.

- Preserve the native desktop interface and configurable localhost backend default.
- Use generic paths such as `~/ComfyUI` in source defaults and documentation. Do not commit machine-specific settings, private media, generated packages, or credentials.
- Do not restart ComfyUI or interrupt active jobs without explicit user permission. Offline tests and packaging checks must not submit ComfyUI jobs.
- Preserve queue append behavior, prompt-ID recovery, protection against duplicate submission, BF16 byte preservation, and refusal to overwrite exports.
- Run the relevant offline tests for functional changes; use the branch README's environment setup. Documentation-only changes need link/content checks, not model execution.
- Report validation limits honestly. Source tests and packaged smoke tests do not establish live encoding or remote-backend compatibility.
- Release binaries can lag source branches. Record the exact source revision and verify artifacts before publishing. Do not rewrite history or replace old releases as routine cleanup.
