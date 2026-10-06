import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication
from refmod_builder import core, ui


def test_editor_roundtrip_and_order(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LIBRARY", tmp_path / "projects")
    monkeypatch.setattr(ui, "LIBRARY", tmp_path / "projects")
    monkeypatch.setattr(ui.Window, "check", lambda self: None)
    app = QApplication.instance() or QApplication([])
    window = ui.Window()
    window.show()
    first, second = tmp_path / "front.png", tmp_path / "side.png"
    Image.new("RGB", (100, 120), "blue").save(first)
    Image.new("RGB", (100, 120), "red").save(second)
    core.import_media(window.project, [first, second])
    window.load_fields()
    window.name.setText("Review character")
    window.description.setPlainText("Two views")
    window.move(1)
    window.save()
    restored = core.load_project(core.project_dir(window.project) / "project.json")
    assert restored["name"] == "Review character"
    assert restored["references"][0]["name"] == "side"
    assert window.refs.count() == 2
    window.remove()
    assert window.refs.count() == 1
    assert first.exists() and second.exists()
    window.close()
    app.processEvents()
