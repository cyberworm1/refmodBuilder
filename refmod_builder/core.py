from __future__ import annotations

import json
import os
import re
import shutil
import struct
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

DATA = Path.home() / ("Library/Application Support/refmodBuilder" if sys.platform == "darwin" else ".local/share/refmodBuilder")
LIBRARY = DATA / "projects"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def settings():
    defaults = {"comfy_url": "http://127.0.0.1:8188", "comfy_dir": str(Path.home() / "ComfyUI"),
                "export_dir": str(Path.home() / ("Documents/refmodBuilder/exports" if sys.platform == "darwin" else "ComfyUI/models/refmods")),
                "refmod_dir": str(Path.home() / "ComfyUI/models/refmods"),
                "video_vae": "minimax_h3_video_vae_fp16.safetensors",
                "audio_vae": "minimax_h3_audio_vae_fp32.safetensors"}
    path = DATA / "settings.json"
    if path.exists():
        saved = json.loads(path.read_text())
        defaults.update(saved)
        if "refmod_dir" not in saved:
            defaults["refmod_dir"] = str(Path(defaults["comfy_dir"]) / "models/refmods")
    return defaults


def new_project():
    return {"schema_version": 1, "id": uuid.uuid4().hex, "name": "Untitled character", "kind": "Character",
            "description": "", "mode": "Full Reference", "resolution": 512, "frames": 22,
            "token_limit": 16384, "references": [], "exports": [], "job": None}


def project_dir(project):
    identifier = project["id"]
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("Invalid project ID")
    return LIBRARY / identifier


def save_project(project):
    project["updated"] = datetime.now(timezone.utc).isoformat()
    atomic_json(project_dir(project) / "project.json", project)


def load_project(path):
    project = json.loads(Path(path).read_text())
    if project.get("schema_version") != 1:
        raise ValueError("Unsupported project version")
    project_dir(project)
    for ref in project["references"]:
        source_path(project, ref)
    return project


def source_path(project, ref):
    root = project_dir(project).resolve()
    path = (root / ref["file"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Reference escapes the project folder")
    return path


def import_media(project, paths):
    imported = []
    folder = project_dir(project) / "sources"
    folder.mkdir(parents=True, exist_ok=True)
    for item in paths:
        path = Path(item)
        suffix = path.suffix.lower()
        kind = "image" if suffix in IMAGE_EXT else "video" if suffix in VIDEO_EXT else "audio" if suffix in AUDIO_EXT else None
        if not kind:
            raise ValueError(f"Unsupported media: {path.name}")
        identifier = uuid.uuid4().hex
        dest = folder / (identifier + suffix)
        shutil.copy2(path, dest)
        ref = {"id": identifier, "name": path.stem, "kind": kind, "file": str(dest.relative_to(project_dir(project))),
               "start": 0.0, "end": 0.0, "crop": None, "soundtrack": False, "notes": ""}
        if kind == "image":
            with Image.open(dest) as image:
                oriented = ImageOps.exif_transpose(image)
                ref["width"], ref["height"] = oriented.size
        imported.append(ref)
    if len(project["references"]) + len(imported) > 128:
        raise ValueError("A project supports up to 128 source references")
    project["references"].extend(imported)
    save_project(project)
    return project


def slug(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:64] or "package"


def validate_project(project):
    if not project["name"].strip():
        raise ValueError("Give the package a name")
    if not project["references"]:
        raise ValueError("Add at least one reference")
    for ref in project["references"]:
        if not source_path(project, ref).is_file():
            raise ValueError(f"Missing source: {ref['name']}")
        if ref["start"] < 0 or (ref["end"] and ref["end"] <= ref["start"]):
            raise ValueError(f"End must be after start: {ref['name']}")
        if ref.get("crop"):
            x, y, w, h = ref["crop"]
            if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.00001 or y + h > 1.00001:
                raise ValueError(f"Invalid crop: {ref['name']}")


def read_header(path):
    with Path(path).open("rb") as stream:
        size_data = stream.read(8)
        if len(size_data) != 8:
            raise ValueError("Truncated safetensors file")
        size = struct.unpack("<Q", size_data)[0]
        if size > 16 * 1024 * 1024:
            raise ValueError("Safetensors header is too large")
        data = stream.read(size)
        if len(data) != size:
            raise ValueError("Truncated safetensors header")
        header = json.loads(data)
    return header, 8 + size


def bundle_info(path):
    header, _ = read_header(path)
    meta = json.loads(header.get("__metadata__", {}).get("refmod_meta", "{}"))
    if meta.get("kind") == "bundle" and meta.get("_format_version") == 5:
        members = meta["members"]
    elif meta.get("kind") in ("image", "video", "audio") and meta.get("_format_version") == 4:
        members = [meta]
    else:
        raise ValueError("Not a supported v4/v5 RefMod")
    return meta, members


def repack_bundle(paths, destination, name, token_limit=0):
    """Copy tensor bytes unchanged, including BF16, into a standard v5 bundle."""
    members, parts, tensors = [], [], {}
    offset = 0
    tokens = 0
    for path in paths:
        path = Path(path)
        header, base = read_header(path)
        meta, current = bundle_info(path)
        for i, member in enumerate(current):
            key = f"ref_{i}" if meta["kind"] == "bundle" else "latent"
            tensor = header[key]
            shape = tensor["shape"]
            if member["kind"] == "audio":
                valid = len(shape) == 4 and shape[:3] == [1, 32, 2] and shape[3] > 0
                cost = shape[-1] * 2
            elif member["kind"] in ("image", "video"):
                valid = len(shape) == 5 and shape[:2] == [1, 24] and all(n > 0 for n in shape) and shape[-1] % 2 == 0 and shape[-2] % 2 == 0
                if member["kind"] == "image":
                    valid = valid and shape[2] == 1
                cost = shape[2] * (shape[-1] // 2) * (shape[-2] // 2) if valid else 0
            else:
                valid, cost = False, 0
            if not valid:
                raise ValueError("Invalid reference tensor shape")
            width = {"F16": 2, "BF16": 2, "F32": 4, "F64": 8}.get(tensor["dtype"])
            if width is None:
                raise ValueError("Unsupported latent dtype")
            expected = width
            for n in shape:
                expected *= n
            start, end = tensor["data_offsets"]
            if start < 0 or end - start != expected or base + end > path.stat().st_size:
                raise ValueError("Invalid or truncated reference tensor")
            tensors[f"ref_{len(members)}"] = {"dtype": tensor["dtype"], "shape": shape, "data_offsets": [offset, offset + expected]}
            parts.append((path, base + start, expected))
            members.append(member)
            offset += expected
            tokens += cost
    if not 1 <= len(members) <= 256:
        raise ValueError("Bundles require 1–256 members")
    if token_limit and tokens > token_limit:
        raise ValueError(f"Package has {tokens:,} tokens, above its {token_limit:,} limit. Reduce references or resolution, or raise the limit and rebuild.")
    metadata = {"_format_version": 5, "kind": "bundle", "name": name, "members": members}
    tensors["__metadata__"] = {"refmod_meta": json.dumps(metadata)}
    header = json.dumps(tensors, separators=(",", ":")).encode()
    header += b" " * ((-len(header)) % 8)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(struct.pack("<Q", len(header)))
            stream.write(header)
            for path, start, length in parts:
                with path.open("rb") as source:
                    source.seek(start)
                    while length:
                        block = source.read(min(length, 1024 * 1024))
                        if not block:
                            raise ValueError("Reference changed while bundling")
                        stream.write(block)
                        length -= len(block)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard link publishes atomically and refuses to overwrite a previous export.
        os.link(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return {"path": str(destination), "tokens": tokens, "members": len(members)}
