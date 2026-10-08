import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("mcp")

from mcp.server.mcpserver.exceptions import ToolError

from refmod_builder import backend, core, mcp_server
from test_core import reference


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LIBRARY", tmp_path / "projects")
    config = dict(core.settings(), comfy_dir=str(tmp_path / "comfy"), export_dir=str(tmp_path / "exports"))
    monkeypatch.setattr(core, "settings", lambda: config)
    return tmp_path


def image(path, color="blue"):
    Image.new("RGB", (64, 48), color).save(path)
    return path


def fake_comfy(monkeypatch, encoded, requests, completed=True):
    import httpx
    def handle(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/prompt":
            payload = json.loads(request.content)
            assert "front" not in payload and "number" not in payload
            return httpx.Response(200, json={"prompt_id": "agent-job"})
        assert request.url.path == "/history/agent-job"
        if not completed:
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"agent-job": {"status": {"completed": True, "status_str": "success"},
                                                       "outputs": {"3": {"text": [str(encoded)]}}}})
    monkeypatch.setattr(backend, "client", lambda config: httpx.Client(transport=httpx.MockTransport(handle), base_url="http://localhost:8188"))
    monkeypatch.setattr(backend, "check_connection", lambda config: {"missing": [], "schemas": {}, "running": 1, "pending": 2})
    monkeypatch.setattr(backend, "prepare", lambda *args: [])
    monkeypatch.setattr(backend, "make_graph", lambda *args: ({"3": {"class_type": "test", "inputs": {}}}, ["3"]))


def test_tools_registered():
    names = {tool.name for tool in asyncio.run(mcp_server.server.list_tools())}
    assert names == {"list_projects", "get_project", "get_settings", "check_backend", "validate_project",
                     "inspect_package", "create_project", "import_media", "update_project", "update_reference",
                     "remove_reference", "move_reference", "submit_build", "build_status", "collect_build"}


def test_agent_edits_project_and_keeps_sources(library):
    front, side = image(library / "front.png"), image(library / "side.png", "red")
    project = mcp_server.create_project("Hero", kind="Character", description="A hero")
    project = mcp_server.import_media(project["id"], [str(front), str(side)])
    first, second = project["references"]
    assert Path(first["source"]).read_bytes() == front.read_bytes()
    mcp_server.update_reference(project["id"], first["id"], notes="front view", crop=[0, 0, 0.5, 0.5])
    mcp_server.move_reference(project["id"], second["id"], 0)
    project = mcp_server.update_project(project["id"], name="Hero v2", resolution=768)
    assert [r["name"] for r in project["references"]] == ["side", "front"]
    assert project["references"][1]["crop"] == [0, 0, 0.5, 0.5]
    assert project["resolution"] == 768
    project = mcp_server.remove_reference(project["id"], second["id"])
    assert len(project["references"]) == 1 and Path(second["source"]).exists()
    assert mcp_server.validate_project(project["id"]) == {"valid": True, "problem": None}
    assert mcp_server.list_projects()[0]["name"] == "Hero v2"


def test_reference_rules_are_reported(library):
    project = mcp_server.create_project("Voice")
    audio = library / "voice.wav"
    audio.write_bytes(b"test")
    ref = mcp_server.import_media(project["id"], [str(audio)])["references"][0]
    with pytest.raises(ToolError, match="Cropping applies to image"):
        mcp_server.update_reference(project["id"], ref["id"], crop=[0, 0, 1, 1])
    with pytest.raises(ToolError, match="End must be after start"):
        mcp_server.update_reference(project["id"], ref["id"], start=5, end=2)
    with pytest.raises(ToolError, match="Unsupported media"):
        mcp_server.import_media(project["id"], [str(library / "notes.txt")])
    with pytest.raises(ToolError, match="No project"):
        mcp_server.get_project("0" * 32)


def test_tools_work_through_an_mcp_client(library):
    from mcp import Client
    async def session():
        async with Client(mcp_server.server) as client:
            created = await client.call_tool("create_project", {"name": "Hero", "kind": "Asset"})
            failed = await client.call_tool("get_project", {"project_id": "not-an-id"})
            return created, failed
    created, failed = asyncio.run(session())
    assert not created.is_error and created.structured_content["kind"] == "Asset"
    assert failed.is_error and "Invalid project ID" in failed.content[0].text


def test_stale_save_is_refused(library):
    project = mcp_server.create_project("Shared")
    window_copy = core.load_project(core.project_dir(project) / "project.json")
    mcp_server.update_project(project["id"], description="Agent edit")
    window_copy["description"] = "Window edit"
    with pytest.raises(core.ProjectConflict):
        core.save_project(window_copy)
    assert mcp_server.get_project(project["id"])["description"] == "Agent edit"


def test_submit_requires_confirmation(library, monkeypatch):
    requests = []
    fake_comfy(monkeypatch, None, requests)
    project = mcp_server.create_project("Hero")
    mcp_server.import_media(project["id"], [str(image(library / "front.png"))])
    with pytest.raises(ToolError, match="1 running and 2 queued"):
        mcp_server.submit_build(project["id"])
    assert requests == [] and mcp_server.get_project(project["id"])["job"] is None


def test_agent_build_appends_then_collects_once(library, monkeypatch):
    encoded = library / "member.safetensors"
    reference(encoded, bundle=True)
    requests = []
    fake_comfy(monkeypatch, encoded, requests)
    project = mcp_server.create_project("Hero")
    mcp_server.import_media(project["id"], [str(image(library / "front.png"))])
    submitted = mcp_server.submit_build(project["id"], confirm=True)
    assert submitted["status"] == "queued" and submitted["prompt_id"] == "agent-job"
    with pytest.raises(ToolError, match="pending build"):
        mcp_server.submit_build(project["id"], confirm=True)
    assert mcp_server.build_status(project["id"])["ready"] is True
    collected = mcp_server.collect_build(project["id"])
    assert Path(collected["export"]["path"]).exists()
    assert mcp_server.collect_build(project["id"]) == collected
    assert mcp_server.build_status(project["id"])["status"] == "complete"
    assert requests.count(("POST", "/prompt")) == 1


def test_unfinished_build_is_not_collected(library, monkeypatch):
    requests = []
    fake_comfy(monkeypatch, None, requests, completed=False)
    project = mcp_server.create_project("Hero")
    mcp_server.import_media(project["id"], [str(image(library / "front.png"))])
    mcp_server.submit_build(project["id"], confirm=True)
    assert mcp_server.build_status(project["id"])["ready"] is False
    with pytest.raises(ToolError, match="not finished"):
        mcp_server.collect_build(project["id"])


def test_build_lock_blocks_concurrent_submission(library, monkeypatch):
    fake_comfy(monkeypatch, None, [])
    project = mcp_server.create_project("Hero")
    mcp_server.import_media(project["id"], [str(image(library / "front.png"))])
    with core.project_lock(project, "build"):
        with pytest.raises(ToolError, match="Another refmodBuilder"):
            mcp_server.submit_build(project["id"], confirm=True)
