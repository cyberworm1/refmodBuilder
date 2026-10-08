# Maintainer handoff

Last updated: 2026-10-06. This document records the implementation and validation completed in the initial development session. Recheck branch heads and upstream node schemas before future changes.

## Product and scope

refmodBuilder is a native PySide6 desktop application for authoring one character, asset, or location per RefMod package. Users import image, video, and audio references, edit their preparation settings, save editable projects, and ask ComfyUI to encode references. The final export is a MiniMax H3 v5 safetensors bundle. The app does not run a browser UI or host the generation model itself.

Package-building workflows are generated inside the app, not supplied as a separate workflow file. Downstream video-generation workflows are separate from the builder and are not shipped in this repository.

## Branches and published artifacts

| Branch | Role | Initial implementation | Generic-path cleanup |
| --- | --- | --- | --- |
| `main` | Original Linux application; backend restricted to localhost | `15dbac6` | `9d58189` |
| `macos-arm64` | Apple Silicon packaging and configurable remote backend with shared folders | `2e20567` | `9d8700b` |
| `macos-intel` | Intel packaging; build script supports either native Mac architecture | `97085b8` | `e7a057a` |

These branches are not equivalent. Do not replace one with another without reviewing platform behavior. For shared fixes, apply the change to each relevant branch and verify the resulting diff.

Published prereleases are `v0.1.1-macos-arm64` and `v0.1.1-macos-intel`. The ARM release was built from `2e20567`; the Intel release from `97085b8`. Each includes a ZIP, SHA-256 checksum, and `macos-build-info.json` dependency report. The repository was private when checked during this session.

**Existing release binaries and tags predate the generic-path cleanup.** Current source uses `~/ComfyUI`, removes the local styling-app reference, and replaces a custom FFmpeg search location with `~/.local/bin/ffmpeg`. Previously published downloads and Git history still contain their original contents. A new release is needed to deliver those changes in compiled apps. Existing saved user settings override defaults and are not migrated.

## Code map and data flow

| File | Responsibility |
| --- | --- |
| `refmod_builder/ui.py` | Qt window, reference editing, settings, library, worker threads, monitoring |
| `refmod_builder/theme.py`, `icon.svg` | Desktop appearance and application icon |
| `refmod_builder/core.py` | Settings, project manifests, source copies, validation, safetensors parsing and assembly |
| `refmod_builder/backend.py` | ComfyUI capability checks, FFmpeg preprocessing, workflow construction, submission and recovery |
| `refmod_builder/mcp_server.py` | `main` only: optional stdio MCP server for agents, sharing the project library |
| `tests/test_core.py`, `tests/test_ui.py`, `tests/test_mcp.py` | Offline behavior, UI, and MCP checks |
| `refmod_builder/smoke.py` | Mac-branch offline packaged-app check |
| `tools/build_macos.py`, `tools/macos_entry.py` | Mac bundle creation and packaged entry point |
| `packaging/macos/` | PyInstaller specification and dependency notices |

1. Import copies originals into the project library and records relative source paths in a schema-version-1 JSON manifest.
2. `backend.prepare()` writes prepared media into a unique ComfyUI input subdirectory. Video is silent H.264 at 24 fps; audio is 32 kHz stereo WAV. Selected video soundtracks become separate audio members.
3. `backend.make_graph()` uses backend node schemas to construct extraction and save nodes for each member. The tested upstream MiniMaxH3Mod revision is recorded in the README.
4. `backend.build()` saves the workflow/project snapshot, records submission state, and appends a prompt to ComfyUI's queue.
5. `backend.finish()` polls only the saved prompt ID, resolves encoded files, and calls `core.repack_bundle()`.
6. Repacking accepts supported v4 references and v5 bundles, validates tensor dimensions/dtypes/offsets and total tokens, then copies raw tensor bytes into a v5 bundle. BF16 is preserved without loading model weights or converting tensor values. Publication uses a temporary file and hard link to refuse overwrite.

Character/Asset/Location selects concept metadata; it does not train a model. Compressed Reference uses a 16×16 grid and 100 refinement steps. Package member and token limits are distinct from any downstream generation limit on the number of loaded packages.

## Configuration and storage

The backend defaults to `http://127.0.0.1:8188`. All installation examples use `~/ComfyUI`; actual folders are configured in Settings.

- Linux settings/library: `~/.local/share/refmodBuilder/`.
- macOS settings/library: `~/Library/Application Support/refmodBuilder/`.
- Linux default exports: `~/ComfyUI/models/refmods/<type>/`.
- macOS default exports: `~/Documents/refmodBuilder/exports/<type>/`.
- Prepared input: `ComfyUI/input/refmodBuilder/<build-id>/`.
- Backend intermediates: `<backend-refmod-root>/refmodBuilder_work/<build-id>/`.
- Project build snapshots: `<project-folder>/builds/<build-id>.json`.

On Mac branches, a remote URL alone is insufficient: the app needs mounted, writable access to the backend input folder and readable access to the backend RefMod folder. Inputs use server-relative names; output recovery can map results through the configured `refmod_dir`. HTTP media transfer is not implemented. The remote path mapping has mocked coverage, not a completed live mounted-server integration test.

Do not commit settings, user projects, source media, generated packages, local workflows, machine shortcuts, build outputs, or credentials. Keep real usernames, hostnames, LAN addresses, and home paths out of documentation and release notes. Git author identity and repository/dependency links remain intentional attribution.

## Operational rules and recovery

Do not interrupt active ComfyUI work or restart ComfyUI without explicit user permission. Installing nodes may require a restart; coordinate that separately. Tests and packaging checks must not submit production jobs.

Preserve these existing behaviors:

- Submission appends to the queue; it never requests priority, clears the queue, interrupts generation, or unloads models.
- Stop monitoring stops the app's monitoring, not the backend job.
- Project saves carry a `revision`; a stale save raises `ProjectConflict` instead of overwriting another window's or agent's edit. Build submission and assembly hold a per-project `.build.lock`.
- The MCP `submit_build` tool requires `confirm=true`. Agents get no tools to delete projects, sources or exports, or to change settings.
- A saved prompt ID allows Resume build to collect the existing result.
- `submission_unknown` without a prompt ID blocks automatic resubmission. Inspect ComfyUI history before recovery to avoid duplicate work.
- If backend output is inaccessible, correct the folder mount and resume; do not automatically re-encode.
- Original media and intermediates are retained. No automatic cleanup policy exists.

## Validation completed

The user reported successful live package creation and downstream video generation using one character package, followed by generation combining a character and vehicle package. These private assets/workflows were intentionally excluded from Git. Do not treat this as a general quality guarantee or evidence of all audio/compressed modes working.

All 18 source tests passed on Linux and both Mac build hosts before publication. The generic-path cleanup subsequently passed all 18 tests on Linux. Mac packages passed architecture/signature checks and offline smoke checks with both offscreen Qt and native Cocoa rendering, using a PATH without Homebrew. Smoke coverage includes a rendered window, media import, bundled FFmpeg conversion, and project save/reopen; it submits zero ComfyUI jobs.

Known gaps: live audio-reference encoding, compressed-reference quality, and live remote-mounted-backend integration. Cropping applies to still images; token totals are exact after encoding, not predicted before it. Projects accept up to 128 sources and packages up to 256 members. Audio is limited to 600 seconds per reference. Importing an existing RefMod as an editable project is not implemented.

## Development and macOS release procedure

Use the branch README for exact installation commands. `main` does not have the Mac branches' optional dependency groups; install its test dependencies explicitly as documented there. FFmpeg must be available for source tests.

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

For Mac builds, use a native Python environment on the target architecture and the branch's `tools/build_macos.py`. Intel was tested with Python 3.13.12 on macOS 12.7.6; Apple Silicon with Python 3.12.15 on macOS 26.6.2. Intel test setup pins NumPy 2.2.6 for Monterey compatibility; NumPy and safetensors are not runtime requirements of the bundled app.

The published Intel app requires macOS 12+; the ARM artifact requires macOS 26+ due to its build runtime. The Intel branch's generalized builder sets minimum macOS to the host major version. Build on the oldest intended OS and inspect dependency deployment targets; changing Info.plist alone does not make a newer runtime compatible with older systems.

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/build_macos.py
QT_QPA_PLATFORM=cocoa dist/refmodBuilder.app/Contents/MacOS/refmodBuilder --smoke-test
codesign --verify --deep --strict dist/refmodBuilder.app
```

The builder bundles Python, Qt, FFmpeg, icons, and third-party notices; verifies architecture/signature; runs an offline smoke test; and writes a ZIP/checksum/build report. Both published apps are ad-hoc signed, not Developer ID signed or notarized. No signing credentials are configured.

For a new release, update the version consistently in `pyproject.toml`, the bundle specification, and build artifact naming; inspect any other version literals. Commit the source, build that exact revision, verify native launch and checksum, then push the intended branch and publish a new GitHub prerelease targeting that commit. Upload the ZIP, checksum, and build report with `gh`; verify uploaded assets. Use a file for multiline release notes. Never commit binaries. Do not silently replace older artifacts or rewrite history as routine maintenance.

## Suggested next work

1. Rebuild and publish both Mac architectures from the generic-path source updates.
2. Decide how to consolidate shared changes across the three branches without losing Linux or Mac behavior.
3. Validate a remote backend with mounted folders, live audio encoding, and compressed references in an explicitly authorized test session.
4. Consider earlier token estimates, clearer interrupted-submission recovery, and user-controlled intermediate cleanup.
5. Evaluate older macOS support for Apple Silicon and Developer ID signing/notarization if broader distribution is needed.
