"""Offline packaged-app check; all writes are confined to a temporary project."""
import json
import platform
import tempfile
import wave
from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QApplication

from . import backend, core, ui
from .theme import STYLE


def run():
    with tempfile.TemporaryDirectory(prefix='refmodBuilder-smoke-') as directory:
        root = Path(directory)
        core.LIBRARY = root / 'projects'
        ui.LIBRARY = core.LIBRARY
        ui.Window.check = lambda self: None
        app = QApplication.instance() or QApplication([])
        app.setStyle('Fusion')
        app.setStyleSheet(STYLE)
        picture = root / 'image.png'
        Image.new('RGB', (128, 96), '#4b79be').save(picture)
        audio = root / 'audio.wav'
        with wave.open(str(audio), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(32000)
            stream.writeframes(b'\0\0' * 32000)
        project = core.new_project()
        core.import_media(project, [picture, audio])
        (root / 'comfy/input').mkdir(parents=True)
        config = dict(core.settings(), comfy_dir=str(root / 'comfy'))
        members = backend.prepare(project, config, 'offline-smoke', lambda _: None)
        assert len(members) == 2
        assert members[1]['seconds'] == 1.0
        window = ui.Window()
        window.project = project
        window.load_fields()
        window.show()
        app.processEvents()
        assert not window.grab().isNull()
        window.save()
        assert core.load_project(core.project_dir(project) / 'project.json')['id'] == project['id']
        window.close()
        app.processEvents()
        print(json.dumps({'status': 'ok', 'architecture': platform.machine(),
                          'platform': platform.platform(), 'ffmpeg': backend.ffmpeg(),
                          'checks': ['Qt window', 'media import', 'FFmpeg audio conversion', 'project roundtrip'],
                          'comfyui_jobs_submitted': 0}))
