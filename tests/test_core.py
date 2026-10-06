import json
import struct
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from safetensors import safe_open
from safetensors.numpy import save_file

from refmod_builder import core, backend


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LIBRARY", tmp_path / "projects")
    return tmp_path


def reference(path, kind="image", bundle=False):
    shape = [1, 32, 2, 10] if kind == "audio" else [1, 24, 1, 4, 6]
    data = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    meta = {"_format_version": 4, "kind": kind, "name": kind, "latent_t": 10 if kind == "audio" else 1,
            "latent_h": 4, "latent_w": 6, "description": "Keep this metadata"}
    if bundle:
        meta = {"_format_version": 5, "kind": "bundle", "name": "source", "members": [meta]}
    save_file({"ref_0" if bundle else "latent": data}, path, metadata={"refmod_meta": json.dumps(meta)})
    return data


def test_import_preserves_sources_and_edits(library):
    source = library / "portrait.png"
    Image.new("RGB", (100, 200), "blue").save(source)
    original = source.read_bytes()
    project = core.new_project()
    core.import_media(project, [source])
    ref = project["references"][0]
    ref["crop"] = [0.1, 0.2, 0.5, 0.5]
    core.save_project(project)
    restored = core.load_project(core.project_dir(project) / "project.json")
    assert restored["references"][0]["crop"] == ref["crop"]
    source.unlink()
    assert core.source_path(restored, ref).read_bytes() == original


def test_repack_mixed_bundle_exact_tensors(tmp_path):
    image, audio = tmp_path / "image.safetensors", tmp_path / "audio.safetensors"
    a = reference(image)
    b = reference(audio, "audio", bundle=True)
    destination = tmp_path / "hero.safetensors"
    result = core.repack_bundle([image, audio], destination, "Hero", 100)
    assert result["tokens"] == 26
    with safe_open(destination, framework="numpy") as bundle:
        assert np.array_equal(bundle.get_tensor("ref_0"), a)
        assert np.array_equal(bundle.get_tensor("ref_1"), b)
        meta = json.loads(bundle.metadata()["refmod_meta"])
        assert meta["_format_version"] == 5
        assert [m["kind"] for m in meta["members"]] == ["image", "audio"]
        assert meta["members"][0]["description"] == "Keep this metadata"


def test_budget_overflow_does_not_publish(tmp_path):
    source = tmp_path / "image.safetensors"
    reference(source)
    output = tmp_path / "bundle.safetensors"
    with pytest.raises(ValueError, match="above"):
        core.repack_bundle([source], output, "Hero", 1)
    assert not output.exists()


def test_existing_export_never_overwritten(tmp_path):
    source = tmp_path / "image.safetensors"
    reference(source)
    output = tmp_path / "bundle.safetensors"
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        core.repack_bundle([source], output, "Hero")
    assert output.read_bytes() == b"existing"


def test_truncated_tensor_rejected(tmp_path):
    source = tmp_path / "image.safetensors"
    reference(source)
    source.write_bytes(source.read_bytes()[:-8])
    with pytest.raises(ValueError, match="truncated"):
        core.repack_bundle([source], tmp_path / "bad.safetensors", "Bad")


def test_bfloat16_bytes_are_preserved(tmp_path):
    payload = bytes(range(192))  # 1*24*1*2*2 BF16
    meta = {"_format_version": 4, "kind": "image", "name": "bf16"}
    header = json.dumps({"latent": {"dtype": "BF16", "shape": [1, 24, 1, 2, 2], "data_offsets": [0, 192]},
                         "__metadata__": {"refmod_meta": json.dumps(meta)}}).encode()
    header += b" " * (-len(header) % 8)
    source = tmp_path / "bf16.safetensors"
    source.write_bytes(struct.pack("<Q", len(header)) + header + payload)
    output = tmp_path / "bundle.safetensors"
    core.repack_bundle([source], output, "BF16")
    header, start = core.read_header(output)
    assert header["ref_0"]["dtype"] == "BF16"
    assert output.read_bytes()[start:] == payload
    with safe_open(output, framework="numpy") as reader:
        assert reader.keys() == ["ref_0"]


def test_path_traversal_rejected(library):
    project = core.new_project()
    with pytest.raises(ValueError, match="escapes"):
        core.source_path(project, {"file": "../../outside.png"})


def test_invalid_trim_rejected(library):
    source = library / "audio.wav"
    source.write_bytes(b"test")
    project = core.new_project()
    core.import_media(project, [source])
    project["references"][0].update(start=5, end=3)
    with pytest.raises(ValueError, match="End"):
        core.validate_project(project)


def test_graph_preserves_independent_members():
    project = core.new_project()
    project["description"] = "A hero"
    schemas = {name: {"input": {"required": {}}} for name in backend.REQUIRED}
    members = [{"kind": "visual", "folder": "/tmp/image", "name": "front", "notes": "front view"},
               {"kind": "audio", "file": "refmodBuilder/test/audio.wav", "seconds": 2.0, "name": "voice", "notes": ""}]
    graph, outputs = backend.make_graph(project, core.settings(), schemas, members, "test")
    assert len(outputs) == 2
    assert all(graph[node]["class_type"] == "MiniMaxH3RefModBundleSave" for node in outputs)
    extracts = [node for node in graph.values() if node["class_type"].endswith("Extract")]
    assert len(extracts) == 2
    assert all(node["inputs"]["save"] is False for node in extracts)
    assert all(node["inputs"]["budget_policy"] == "error" for node in extracts)


def test_missing_nodes_never_submit(library, monkeypatch):
    source = library / "image.png"
    Image.new("RGB", (32, 32)).save(source)
    project = core.new_project()
    core.import_media(project, [source])
    monkeypatch.setattr(backend, "check_connection", lambda config: {"missing": ["MiniMaxH3RefModExtract"]})
    with pytest.raises(ValueError, match="No job was submitted"):
        backend.build(project, core.settings(), lambda _: None)
    assert project["job"] is None


def test_ambiguous_submission_is_not_retried(library):
    project = core.new_project()
    project["job"] = {"status": "submission_unknown", "prompt_id": None, "comfy_url": "http://127.0.0.1:8188"}
    with pytest.raises(ValueError, match="will not automatically resubmit"):
        backend.build(project, core.settings(), lambda _: None)


def test_build_appends_queue_and_collects_only_own_outputs(library, monkeypatch):
    import httpx
    source = library / "image.png"
    Image.new("RGB", (32, 32)).save(source)
    project = core.new_project()
    core.import_media(project, [source])
    encoded = library / "member.safetensors"
    reference(encoded, bundle=True)
    requests = []
    def handle(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/prompt":
            payload = json.loads(request.content)
            assert "front" not in payload and "number" not in payload
            return httpx.Response(200, json={"prompt_id": "our-job"})
        assert request.url.path == "/history/our-job"
        return httpx.Response(200, json={"our-job": {"status": {"completed": True, "status_str": "success"},
                                                     "outputs": {"3": {"text": [str(encoded)]}}}})
    monkeypatch.setattr(backend, "client", lambda config: httpx.Client(transport=httpx.MockTransport(handle), base_url="http://localhost:8188"))
    monkeypatch.setattr(backend, "check_connection", lambda config: {"missing": [], "schemas": {}})
    monkeypatch.setattr(backend, "prepare", lambda *args: [])
    monkeypatch.setattr(backend, "make_graph", lambda *args: ({"3": {"class_type": "test", "inputs": {}}}, ["3"]))
    config = dict(core.settings(), export_dir=str(library / "exports"))
    result = backend.build(project, config, lambda _: None)
    assert result["job"]["status"] == "complete"
    assert Path(result["exports"][0]["path"]).exists()
    assert requests == [("POST", "/prompt"), ("GET", "/history/our-job")]


def test_prepare_image_crop_and_audio_trim(library):
    import wave
    image = library / "image.png"
    Image.new("RGB", (100, 200), "red").save(image)
    audio = library / "audio.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(32000)
        wav.writeframes(b"\0\0" * 64000)
    project = core.new_project()
    core.import_media(project, [image, audio])
    project["references"][0]["crop"] = [0, 0, 0.5, 0.5]
    project["references"][1].update(start=0.5, end=1.5)
    config = dict(core.settings(), comfy_dir=str(library / "comfy"))
    members = backend.prepare(project, config, "test", lambda _: None)
    with Image.open(Path(members[0]["folder"]) / "reference.png") as cropped:
        assert cropped.size == (50, 100)
    assert members[1]["seconds"] == pytest.approx(1.0)
    with wave.open(str(library / "comfy/input" / members[1]["file"]), "rb") as wav:
        assert wav.getnchannels() == 2
        assert wav.getframerate() == 32000
