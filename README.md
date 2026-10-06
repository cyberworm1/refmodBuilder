# refmodBuilder

A native PySide6/Qt desktop app for creating one MiniMax H3 reference package per character, asset, or location.

## Install and launch

Source installations require Linux or macOS, Python 3.11+, and FFmpeg on PATH (Homebrew FFmpeg is also discovered on macOS). Install the desktop app in its own environment:

```bash
git clone --branch macos-arm64 https://github.com/cyberworm1/refmodBuilder.git
cd refmodBuilder
python3.11 -m venv .venv
.venv/bin/pip install -e .
./launch.sh
```

The app has its own Python environment. It uses a configured ComfyUI server for encoding, and works offline for reference editing and project browsing. The default server remains `http://127.0.0.1:8188` on both platforms.

## Apple Silicon desktop build

The `macos-arm64` branch adds a compiled `refmodBuilder.app`, packaged with Python, Qt, the app icon, and FFmpeg. Download the ZIP from the repository's macOS prerelease, unzip it, and move the app to Applications. No separate Python or FFmpeg installation is needed to run the bundle.

This build targets **Apple Silicon (arm64), macOS 26 or later**, matching the minimum OS of the build machine's Homebrew Python runtime. Intel Macs are not supported by this artifact. It is ad-hoc signed, not Developer ID signed or notarized. macOS may require approval in System Settings → Privacy & Security when opening a downloaded copy.

Mac settings and projects are saved under `~/Library/Application Support/refmodBuilder/`. Exports default to `~/Documents/refmodBuilder/exports/`. The application bundle can be moved without moving or deleting projects.

To reproduce the build on an Apple Silicon Mac:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev,macos-build]'
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/build_macos.py
```

The build verifies arm64 binaries, checks the bundle signature, runs an offline packaged-app smoke test with Homebrew removed from PATH, and writes a ZIP, SHA-256 checksum, and dependency/version report into `dist/`. The smoke test creates only temporary data and submits no ComfyUI jobs.

## Using the app

- Add images, video, or audio using **Add…** or drag and drop.
- Name the package and choose Character, Asset, or Location.
- Select a reference to preview it, rename it, add notes, crop an image, or trim video/audio.
- Select **Include the video's audio** to add its soundtrack as an independent member.
- Save the project. Source copies and edits remain available in the Library.
- Choose Full Reference or Compressed Reference, resolution, video-frame limit, and package token limit.
- Build when the RefMod nodes are available. Exports receive unique filenames.

Project saves are automatic after edits, with an explicit Save Project button. Removing a reference removes it from the package; the source copy is retained in the project folder. Each build preserves a workflow snapshot. On Linux, the project library is under `~/.local/share/refmodBuilder/projects/` and settings are under `~/.local/share/refmodBuilder/settings.json`. On macOS, these use the Application Support folder described above.

On Linux, exports default to `~/ComfyUI/models/refmods/<type>/`. Settings can change the ComfyUI URL, ComfyUI folder, backend RefMod folder, local export folder, and VAE filenames.

### Configuring a remote ComfyUI backend

Set the backend URL in Settings. This version transfers files through **mounted/shared folders**; changing the URL alone is insufficient. Mount the remote ComfyUI directory using your existing file-sharing setup, select that mount as **ComfyUI folder (local or mounted)**, and select its `models/refmods` folder as **Backend RefMod folder (local or mounted)**. For a backend with a custom RefMod root, mount and select that root instead. The ComfyUI mount needs write access to `input`; the RefMod mount needs read access to the encoded results.

Prepared input paths are sent relative to the backend's input folder, so a Mac mount path is never sent as a server filesystem path. Encoded members are read through the mounted RefMod folder and assembled into a package in the app's local export folder. To make the final package available for generation on the server, copy it to the backend's RefMod folder or choose a writable mount of that folder as the export destination. HTTP file upload/download and automatic share mounting are not implemented.

The paths below use `~/ComfyUI` as a generic example. Set your actual installation and export folders in Settings.

## ComfyUI requirements

Use a ComfyUI installation with native MiniMax H3 support, the H3 video/audio VAEs, and [MiniMaxH3Mod](https://github.com/Luisacaotica/ComfyUI-MiniMaxH3Mod) installed under its `custom_nodes` folder. The default local location is:

`~/ComfyUI/custom_nodes/ComfyUI-MiniMaxH3Mod`

Tested MiniMaxH3Mod revision: `f9462081e28794389b5a6c5067eb327412ad8ee7`.

Install the extension's requirements in ComfyUI's own environment and provide OpenCV or imageio for its video loader. New custom nodes require a ComfyUI restart; arrange this when existing jobs have finished. The app checks node availability before submitting work. ComfyUI defaults to `http://127.0.0.1:8188`; configure the URL and local folders in Settings.

The app never restarts ComfyUI, interrupts jobs, clears its queue, or unloads models. A build is appended to the normal queue. **Stop monitoring** stops only this app's monitoring; ComfyUI continues its job. Reopen the project and choose **Resume build** to collect the result. A saved prompt ID prevents automatic duplicate submissions. If a network failure leaves submission outcome unknown, inspect ComfyUI history before retrying; this version deliberately blocks automatic resubmission.

## Encoding and files

Each source reference is prepared in a unique `ComfyUI/input/refmodBuilder/<build-id>/` folder. Images are orientation-corrected and cropped. Trimmed videos are encoded as silent H.264 at 24 fps; selected audio is prepared separately as 32 kHz stereo WAV. Originals remain intact.

Each member is encoded separately through the upstream RefMod nodes. Intermediate members are saved by ComfyUI under its registered RefMod root, in `refmodBuilder_work/<build-id>/`. After the app sees its own completed history record, it copies the member tensor bytes into one v5 safetensors bundle. BF16 data is preserved without loading a second model or converting tensors. Independent audio clips remain independent members.

The combined token limit is checked before publishing the final bundle. Overflow reports an error instead of silently dropping references. Full mode and compressed mode use upstream H3 behavior; category and description are metadata, not training instructions. Compressed mode uses a 16×16 grid with 100 refinement steps. Generation quality and voice identity are not guaranteed.

Unique export names and atomic publication prevent overwriting previous packages. Intermediate files are retained for inspection/recovery; automatic cleanup is not included in this version.

## Current limitations

- Full Reference image/video package creation and subsequent H3 generation have been tested locally, including a generation using two packages together. Audio preprocessing and bundle assembly are tested, but live audio-reference encoding and Compressed Reference quality have not yet been validated.
- Video/audio trimming and playback are available; interactive cropping currently applies to still images.
- The UI shows a token budget, with the exact count after encoding. It does not yet predict tokens before encoding.
- Audio is limited to 600 seconds per reference. A project supports up to 128 source files, producing at most 256 bundle members with video soundtracks.
- Library browsing covers this app's editable projects and their exports; importing arbitrary pre-existing RefMods is not included.
- Model-free local editing works without ComfyUI; building packages requires the existing ComfyUI service.

## Development and validation

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e . pytest numpy safetensors
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

Tests cover source preservation, save/reopen, ordering/removal, mixed image/audio bundle interoperability with safetensors, BF16 byte preservation, truncated input rejection, token-limit failures, export overwrite prevention, image preparation and audio trimming via FFmpeg, independent member workflow construction, queue append behavior, completion collection, missing-node handling, and ambiguous-submission protection.

Local media, RefMod packages, generation workflows, runtime records, and review screenshots are excluded from this repository. Package-creation workflows are constructed by `refmod_builder/backend.py`; no external workflow template is required.

Upstream: https://github.com/Luisacaotica/ComfyUI-MiniMaxH3Mod

## Maintenance

See [the maintainer handoff](docs/MAINTAINER_HANDOFF.md) for architecture, branch and release status, operational constraints, validation, and future work. Coding agents should also read [AGENTS.md](AGENTS.md).
