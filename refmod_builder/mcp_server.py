"""Local MCP server so agents can edit refmodBuilder projects and request package builds.

Runs over stdio only. It shares the desktop app's project library and settings, and
follows the same rules: builds append to ComfyUI's queue, never interrupt or restart it,
and never overwrite exports or delete projects and sources.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import backend, core

Kind = Literal["Character", "Asset", "Location"]
Mode = Literal["Full Reference", "Compressed Reference"]
Resolution = Literal[256, 512, 768, 1024]
Frames = Annotated[int, Field(ge=1, le=240, description="Video latent frames")]
TokenLimit = Annotated[int, Field(ge=256, le=1048576, description="Package token limit")]

INSTRUCTIONS = """refmodBuilder builds one MiniMax H3 RefMod package per character, asset or location.
Typical flow: create_project, import_media (absolute local paths), update_reference for trims,
crops and notes, validate_project, then submit_build. Submitting queues GPU work on the user's
ComfyUI: ask the user first and pass confirm=true. Builds return immediately; call build_status
until it reports ready, then collect_build to write the package. The desktop app may have the
same project open and reloads your changes. If a submission outcome is unknown, ask the user to
check ComfyUI history; never try to work around it by submitting again."""

server = MCPServer("refmodBuilder", instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
EDIT = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


def tool(annotations):
    """Register a tool and return expected failures to the agent as readable errors."""
    def register(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (ValueError, OSError, httpx.HTTPError) as exc:
                raise ToolError(str(exc) or type(exc).__name__) from exc
        server.tool(annotations=annotations)(wrapper)
        return wrapper
    return register


def load(project_id):
    project = {"id": project_id}
    path = core.project_dir(project) / "project.json"
    if not path.is_file():
        raise ValueError(f"No project with ID {project_id}. Use list_projects to find one.")
    return core.load_project(path)


def find_reference(project, reference_id):
    for index, ref in enumerate(project["references"]):
        if ref["id"] == reference_id:
            return index, ref
    raise ValueError(f"No reference with ID {reference_id} in this project")


def describe(project):
    """The saved project plus details an agent needs but the manifest stores implicitly."""
    result = dict(project, project_folder=str(core.project_dir(project)))
    result["references"] = [dict(ref, source=str(core.source_path(project, ref))) for ref in project["references"]]
    job = project.get("job")
    result["pending_build"] = bool(job and job.get("status") in core.PENDING)
    return result


@tool(READ_ONLY)
def list_projects() -> list[dict[str, Any]]:
    """List refmodBuilder projects in the library, most recently changed first."""
    projects = []
    if not core.LIBRARY.exists():
        return projects
    for path in sorted(core.LIBRARY.glob("*/project.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            project = core.load_project(path)
        except (OSError, ValueError, KeyError):
            continue
        job = project.get("job") or {}
        projects.append({"id": project["id"], "name": project["name"], "kind": project["kind"],
                         "references": len(project["references"]), "exports": len(project["exports"]),
                         "build_status": job.get("status"), "updated": project.get("updated")})
    return projects


@tool(READ_ONLY)
def get_project(project_id: str) -> dict[str, Any]:
    """Return a project's settings, references (with source file paths), exports and build state."""
    return describe(load(project_id))


@tool(READ_ONLY)
def get_settings() -> dict[str, Any]:
    """Return the ComfyUI URL, folders and VAE names configured in the desktop app."""
    return dict(core.settings(), library=str(core.LIBRARY))


@tool(READ_ONLY)
def check_backend() -> dict[str, Any]:
    """Check that ComfyUI is reachable with the RefMod nodes loaded, and report its queue. Changes nothing."""
    result = backend.check_connection(core.settings())
    return {"ready": not result["missing"], "missing_nodes": result["missing"],
            "running": result["running"], "pending": result["pending"]}


@tool(READ_ONLY)
def validate_project(project_id: str) -> dict[str, Any]:
    """Check whether a project is ready to build, without contacting ComfyUI."""
    try:
        core.validate_project(load(project_id))
    except ValueError as exc:
        return {"valid": False, "problem": str(exc)}
    return {"valid": True, "problem": None}


@tool(READ_ONLY)
def inspect_package(path: str) -> dict[str, Any]:
    """Read the metadata and members of a RefMod .safetensors file without loading its tensors."""
    path = Path(path).expanduser()
    if path.suffix != ".safetensors" or not path.is_file():
        raise ValueError("Give the path of an existing .safetensors RefMod")
    meta, members = core.bundle_info(path)
    return {"path": str(path), "format_version": meta.get("_format_version"), "kind": meta.get("kind"),
            "name": meta.get("name"), "members": members}


@tool(EDIT)
def create_project(name: str, kind: Kind = "Character", description: str = "", mode: Mode = "Full Reference",
                   resolution: Resolution = 512, frames: Frames = 22, token_limit: TokenLimit = 16384) -> dict[str, Any]:
    """Create a new, empty project. Add references with import_media."""
    if not name.strip():
        raise ValueError("Give the package a name")
    project = core.new_project()
    project.update(name=name.strip(), kind=kind, description=description, mode=mode,
                   resolution=resolution, frames=frames, token_limit=token_limit)
    core.save_project(project)
    return describe(project)


@tool(EDIT)
def import_media(project_id: str, paths: list[str]) -> dict[str, Any]:
    """Copy local image, video or audio files into a project as new references. Originals are not changed."""
    absolute = [Path(path).expanduser() for path in paths]
    if not absolute or not all(path.is_absolute() for path in absolute):
        raise ValueError("Give one or more absolute file paths")
    return describe(core.import_media(load(project_id), absolute))


@tool(EDIT)
def update_project(project_id: str, name: str | None = None, kind: Kind | None = None, description: str | None = None,
                   mode: Mode | None = None, resolution: Resolution | None = None, frames: Frames | None = None,
                   token_limit: TokenLimit | None = None) -> dict[str, Any]:
    """Change package settings. Only the fields you pass are changed."""
    project = load(project_id)
    changes = {key: value for key, value in dict(name=name, kind=kind, description=description, mode=mode,
               resolution=resolution, frames=frames, token_limit=token_limit).items() if value is not None}
    if "name" in changes and not changes["name"].strip():
        raise ValueError("Give the package a name")
    project.update(changes)
    core.save_project(project)
    return describe(project)


@tool(EDIT)
def update_reference(project_id: str, reference_id: str, name: str | None = None, notes: str | None = None,
                     start: Annotated[float, Field(ge=0)] | None = None,
                     end: Annotated[float, Field(ge=0, description="Seconds; 0 means the end of the media")] | None = None,
                     crop: Annotated[list[float], Field(min_length=4, max_length=4,
                                     description="Image crop as fractions [x, y, width, height]")] | None = None,
                     clear_crop: bool = False, include_audio: bool | None = None) -> dict[str, Any]:
    """Edit one reference: rename, notes, trim (video/audio), crop (images) or include a video's soundtrack."""
    project = load(project_id)
    _, ref = find_reference(project, reference_id)
    if (start is not None or end is not None) and ref["kind"] == "image":
        raise ValueError("Trimming applies to video and audio references")
    if (crop is not None or clear_crop) and ref["kind"] != "image":
        raise ValueError("Cropping applies to image references")
    if include_audio is not None and ref["kind"] != "video":
        raise ValueError("Only video references have a soundtrack to include")
    if name is not None:
        if not name.strip():
            raise ValueError("Give the reference a name")
        ref["name"] = name.strip()
    if notes is not None:
        ref["notes"] = notes
    if start is not None:
        ref["start"] = start
    if end is not None:
        ref["end"] = end
    if ref["end"] and ref["end"] <= ref["start"]:
        raise ValueError("End must be after start")
    if crop is not None:
        x, y, w, h = crop
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.00001 or y + h > 1.00001:
            raise ValueError("Crop must be fractions of the image: x, y >= 0, width and height > 0, within 1")
        ref["crop"] = crop
    if clear_crop:
        ref["crop"] = None
    if include_audio is not None:
        ref["soundtrack"] = include_audio
    core.save_project(project)
    return describe(project)


@tool(EDIT)
def remove_reference(project_id: str, reference_id: str) -> dict[str, Any]:
    """Remove a reference from the package. Its source copy stays in the project folder."""
    project = load(project_id)
    index, _ = find_reference(project, reference_id)
    project["references"].pop(index)
    core.save_project(project)
    return describe(project)


@tool(EDIT)
def move_reference(project_id: str, reference_id: str, position: Annotated[int, Field(ge=0)]) -> dict[str, Any]:
    """Move a reference to a zero-based position in the package order."""
    project = load(project_id)
    index, ref = find_reference(project, reference_id)
    project["references"].pop(index)
    project["references"].insert(min(position, len(project["references"])), ref)
    core.save_project(project)
    return describe(project)


@tool(ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False))
def submit_build(project_id: str, confirm: bool = False) -> dict[str, Any]:
    """Prepare references and append one encoding job to the end of ComfyUI's queue. Returns without waiting.

    This uses the user's GPU. Ask the user before calling with confirm=true. It never reorders,
    interrupts or clears the ComfyUI queue.
    """
    project = load(project_id)
    if not confirm:
        core.validate_project(project)
        state = check_backend()
        raise ValueError(f"Not submitted. ComfyUI has {state['running']} running and {state['pending']} queued jobs. "
                         "Confirm with the user, then call submit_build again with confirm=true.")
    project = backend.submit(project, core.settings(), lambda _message: None)
    return {"status": project["job"]["status"], "build_id": project["job"]["build_id"],
            "prompt_id": project["job"]["prompt_id"], "destination": project["job"]["destination"],
            "next": "Call build_status until it reports ready, then collect_build."}


@tool(ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
def build_status(project_id: str) -> dict[str, Any]:
    """Check the project's latest build once, without waiting."""
    project = load(project_id)
    job = project.get("job")
    if not job:
        return {"status": "none"}
    summary = {"status": job["status"], "build_id": job.get("build_id"), "prompt_id": job.get("prompt_id")}
    if job["status"] == "complete":
        return dict(summary, export=project["exports"][-1] if project["exports"] else None)
    if job["status"] == "failed":
        return dict(summary, error=job.get("error"))
    if not job.get("prompt_id"):
        return dict(summary, error="Submission outcome is unknown. Ask the user to check ComfyUI history; do not resubmit.")
    try:
        record = backend.poll(project, core.settings())
    except ValueError as exc:
        return dict(summary, status="failed", error=str(exc))
    if record:
        return dict(summary, ready=True, next="Call collect_build to write the package.")
    return dict(summary, ready=False)


@tool(ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
def collect_build(project_id: str) -> dict[str, Any]:
    """Assemble a finished build into its package file. Never overwrites an existing export."""
    project = load(project_id)
    job = project.get("job")
    if not job:
        raise ValueError("This project has no build")
    if job["status"] != "complete":
        if job["status"] not in core.PENDING:
            raise ValueError(f"This build is {job['status']}; nothing to collect")
        record = backend.poll(project, core.settings())
        if not record:
            raise ValueError("ComfyUI has not finished this build yet; check build_status later")
        project = backend.collect(project, record, lambda _message: None)
    return {"status": "complete", "export": project["exports"][-1]}


def main():
    server.run("stdio")


if __name__ == "__main__":
    main()
