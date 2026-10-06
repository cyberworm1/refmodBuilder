from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps

from .core import (atomic_json, project_dir, repack_bundle, save_project, slug,
                   source_path, validate_project)

REQUIRED = ["MiniMaxH3RefModExtract", "MiniMaxH3RefModAudioExtract", "MiniMaxH3RefModFolderLoader", "MiniMaxH3RefModBundleSave", "VAELoader", "LoadAudio"]


def client(config):
    url = config["comfy_url"].rstrip("/")
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
        raise ValueError("Enter a valid ComfyUI HTTP or HTTPS URL")
    return httpx.Client(base_url=url, timeout=15, trust_env=False)


def check_connection(config):
    with client(config) as api:
        schemas = {}
        for node in REQUIRED:
            response = api.get(f"/object_info/{node}")
            response.raise_for_status()
            schemas.update(response.json())
        missing = [node for node in REQUIRED if node not in schemas]
        queue = api.get("/queue")
        queue.raise_for_status()
        counts = queue.json()
    return {"missing": missing, "running": len(counts.get("queue_running", [])),
            "pending": len(counts.get("queue_pending", [])), "schemas": schemas}


def defaults(schema):
    result = {}
    for name, spec in schema.get("input", {}).get("required", {}).items():
        kind = spec[0]
        options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
        if "default" in options:
            result[name] = options["default"]
        elif isinstance(kind, list) and kind:
            result[name] = kind[0]
        elif kind == "COMBO" and options.get("options"):
            result[name] = options["options"][0]
        elif kind in ("INT", "FLOAT"):
            result[name] = options.get("min", 0)
        elif kind == "BOOLEAN":
            result[name] = False
        elif kind == "STRING":
            result[name] = ""
    return result


def ffmpeg():
    if getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / "bin/ffmpeg"
        if bundled.is_file():
            return str(bundled)
    path = Path.home() / ".local/bin/ffmpeg"
    for candidate in (path, Path("/opt/homebrew/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("ffmpeg")
    if not found:
        raise ValueError("FFmpeg was not found. Install FFmpeg or use the packaged macOS app.")
    return found


def transcode(source, output, ref, audio=False):
    command = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if ref["start"]:
        command += ["-ss", str(ref["start"])]
    command += ["-i", str(source)]
    if ref["end"]:
        command += ["-t", str(ref["end"] - ref["start"])]
    if audio:
        command += ["-map", "0:a:0", "-vn", "-ar", "32000", "-ac", "2", "-c:a", "pcm_s16le"]
    else:
        filters = []
        if ref.get("crop"):
            x, y, w, h = ref["crop"]
            filters.append(f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y}")
        filters.extend(["scale=trunc(iw/2)*2:trunc(ih/2)*2", "fps=24"])
        command += ["-map", "0:v:0", "-an", "-vf", ",".join(filters), "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p"]
    command.append(str(output))
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    if result.returncode:
        raise ValueError(f"Could not prepare {ref['name']}: {result.stderr[-1800:]}")


def prepare(project, config, build_id, progress):
    input_dir = Path(config["comfy_dir"]).expanduser() / "input"
    if not input_dir.is_dir():
        raise ValueError("ComfyUI's input folder is unavailable. Set the local ComfyUI folder or mount the remote ComfyUI folder and select it in Settings.")
    stage = input_dir / "refmodBuilder" / build_id
    stage.mkdir(parents=True, exist_ok=False)
    members = []
    for index, ref in enumerate(project["references"]):
        progress(f"Preparing {index + 1}/{len(project['references'])}: {ref['name']}")
        source = source_path(project, ref)
        folder = stage / f"{index:03d}"
        folder.mkdir()
        if ref["kind"] == "image":
            dest = folder / "reference.png"
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                if ref.get("crop"):
                    x, y, w, h = ref["crop"]
                    width, height = image.size
                    image = image.crop((round(x*width), round(y*height), round((x+w)*width), round((y+h)*height)))
                image.save(dest)
            members.append({"kind": "visual", "folder": folder.relative_to(input_dir).as_posix(), "name": ref["name"], "notes": ref["notes"]})
        elif ref["kind"] == "video":
            transcode(source, folder / "reference.mp4", ref)
            members.append({"kind": "visual", "folder": folder.relative_to(input_dir).as_posix(), "name": ref["name"], "notes": ref["notes"]})
        if ref["kind"] == "audio" or ref.get("soundtrack"):
            dest = stage / f"audio_{index:03d}.wav"
            transcode(source, dest, ref, audio=True)
            import wave
            with wave.open(str(dest)) as wav:
                duration = wav.getnframes() / wav.getframerate()
            if not 0.025 <= duration <= 600:
                raise ValueError(f"Audio '{ref['name']}' must be between 0.025 and 600 seconds; trim it first")
            members.append({"kind": "audio", "file": dest.relative_to(input_dir).as_posix(),
                            "seconds": duration, "name": ref["name"], "notes": ref["notes"]})
    return members


def make_graph(project, config, schemas, members, build_id):
    graph = {}
    def add(kind, **inputs):
        node_id = str(len(graph) + 1)
        values = defaults(schemas[kind])
        values.update(inputs)
        graph[node_id] = {"class_type": kind, "inputs": values}
        return [node_id, 0]
    visual_vae = add("VAELoader", vae_name=config["video_vae"]) if any(m["kind"] == "visual" for m in members) else None
    audio_vae = add("VAELoader", vae_name=config["audio_vae"]) if any(m["kind"] == "audio" for m in members) else None
    outputs = []
    for index, member in enumerate(members):
        description = project["description"] + ("\n" + member["notes"] if member["notes"] else "")
        if member["kind"] == "visual":
            refs = add("MiniMaxH3RefModFolderLoader", folder=member["folder"], max_items=1, max_frames=max(240, project["frames"]), max_edge=1024)
            mod = add("MiniMaxH3RefModExtract", refs_bundle=refs, vae=visual_vae, name=member["name"],
                      mode=project["mode"], concept_type={"Character": "identity", "Asset": "generic", "Location": "background"}[project["kind"]],
                      ref_resolution=project["resolution"], latent_frames=project["frames"], pool_h=16, pool_w=16,
                      identity=100 if project["mode"] == "Compressed Reference" else 0, max_tokens=project["token_limit"],
                      description=description, save=False, merge=False, motion_only=False, multiplier=1,
                      extraction_preset="manual", budget_policy="error")
        else:
            audio = add("LoadAudio", audio=member["file"])
            mod = add("MiniMaxH3RefModAudioExtract", audio=audio, audio_vae=audio_vae, name=member["name"],
                      max_seconds=member["seconds"], max_tokens=project["token_limit"], budget_policy="error",
                      concept_type="voice" if project["kind"] == "Character" else "ambience" if project["kind"] == "Location" else "sound_fx",
                      description=description, subfolder="", save=False)
        output = add("MiniMaxH3RefModBundleSave", mods=mod, name=f"member_{index:03d}", subfolder=f"refmodBuilder_work/{build_id}")
        outputs.append(output[0])
    return graph, outputs


def build(project, config, progress):
    if project.get("job") and project["job"].get("status") in ("queued", "submitted", "assembling", "submission_unknown"):
        return finish(project, config, progress)
    validate_project(project)
    progress("Checking ComfyUI capabilities…")
    connection = check_connection(config)
    if connection["missing"]:
        raise ValueError("Required nodes are unavailable: " + ", ".join(connection["missing"]) + ". Install MiniMaxH3Mod on the backend and restart ComfyUI when safe. No job was submitted.")
    build_id = uuid.uuid4().hex
    members = prepare(project, config, build_id, progress)
    graph, output_nodes = make_graph(project, config, connection["schemas"], members, build_id)
    destination = Path(config["export_dir"]) / project["kind"].lower() / (slug(project["name"]) + "_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + build_id[:6] + ".safetensors")
    project["job"] = {"build_id": build_id, "status": "submission_unknown", "output_nodes": output_nodes,
                      "destination": str(destination), "prompt_id": None, "comfy_url": config["comfy_url"],
                      "name": project["name"], "token_limit": project["token_limit"],
                      "refmod_dir": config.get("refmod_dir", str(Path(config["comfy_dir"]) / "models/refmods"))}
    atomic_json(project_dir(project) / "builds" / f"{build_id}.json", {"graph": graph, "project": project})
    save_project(project)
    progress("Adding package build to the end of ComfyUI's queue…")
    with client(config) as api:
        # Never set front/number, interrupt, clear the queue, or manage models.
        response = api.post("/prompt", json={"prompt": graph, "client_id": "refmodBuilder-" + build_id})
        if response.is_error:
            project["job"]["status"] = "failed"
            save_project(project)
            raise ValueError("ComfyUI rejected the workflow: " + response.text[:2000])
        result = response.json()
        if "prompt_id" not in result:
            raise ValueError("No prompt ID returned; inspect ComfyUI history before retrying")
        project["job"].update(prompt_id=result["prompt_id"], status="queued")
        save_project(project)
    return finish(project, config, progress)


def finish(project, config, progress):
    job = project["job"]
    config = dict(config, comfy_url=job["comfy_url"])
    if not job.get("prompt_id"):
        raise ValueError("Submission outcome is unknown. Check ComfyUI history before starting another build; this project will not automatically resubmit.")
    progress("Waiting for this package's ComfyUI job. Existing jobs continue normally…")
    with client(config) as api:
        for _ in range(43200):
            progress("Waiting for this package's ComfyUI job. Existing jobs continue normally…")
            response = api.get(f"/history/{job['prompt_id']}")
            response.raise_for_status()
            record = response.json().get(job["prompt_id"])
            if record:
                if record.get("status", {}).get("status_str") == "error":
                    job["status"] = "failed"
                    save_project(project)
                    raise ValueError("ComfyUI encoding failed: " + json.dumps(record.get("status", {}).get("messages", []))[-2200:])
                if record.get("status", {}).get("completed"):
                    break
            time.sleep(2)
        else:
            raise ValueError("Still waiting after 24 hours. Reopen the project to resume monitoring.")
    paths = []
    for index, node_id in enumerate(job["output_nodes"]):
        values = record.get("outputs", {}).get(node_id, {}).get("text", [])
        if len(values) != 1:
            raise ValueError("Completed job did not return a readable RefMod for every reference")
        path = Path(values[0])
        if not path.is_file() and job.get("refmod_dir"):
            path = Path(job["refmod_dir"]).expanduser() / "refmodBuilder_work" / job["build_id"] / f"member_{index:03d}.safetensors"
        if not path.is_file():
            raise ValueError("The encoded RefMod is not accessible locally. Mount the backend's RefMod folder before resuming this build.")
        paths.append(path)
    job["status"] = "assembling"
    save_project(project)
    progress("Assembling and validating the final package…")
    destination = Path(job["destination"])
    if destination.exists():
        # A previous process may have published the bundle before saving the manifest.
        from .core import bundle_info
        _, members = bundle_info(destination)
        result = {"path": str(destination), "members": len(members), "tokens": None}
    else:
        result = repack_bundle(paths, destination, job["name"], job["token_limit"])
    result["created"] = datetime.now().isoformat()
    if not any(e["path"] == result["path"] for e in project["exports"]):
        project["exports"].append(result)
    job["status"] = "complete"
    save_project(project)
    progress("Package saved: " + str(destination))
    return project
