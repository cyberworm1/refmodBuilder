from __future__ import annotations

import copy
import sys
from pathlib import Path

from PIL import Image, ImageOps
from PIL.ImageQt import ImageQt
from PySide6.QtCore import Qt, QThread, Signal, QUrl, QRect, QPoint, QTimer, QSize, QLockFile
from PySide6.QtGui import QDesktopServices, QPixmap, QIcon, QCloseEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QComboBox, QSpinBox,
    QDoubleSpinBox, QTabWidget, QListWidget, QListWidgetItem, QFileDialog,
    QMessageBox, QDialog, QDialogButtonBox, QSplitter, QStackedWidget,
    QCheckBox, QProgressBar, QSlider, QRubberBand, QScrollArea,
)

from . import backend
from .core import (DATA, LIBRARY, IMAGE_EXT, VIDEO_EXT, AUDIO_EXT, atomic_json,
                   import_media, load_project, new_project, project_dir,
                   save_project, settings, source_path)
from .theme import STYLE


class Worker(QThread):
    progress = Signal(str)
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, task):
        super().__init__()
        self.task = task

    def run(self):
        try:
            self.result.emit(self.task(self.report))
        except Exception as exc:
            self.failed.emit(str(exc))

    def report(self, message):
        if self.isInterruptionRequested():
            raise RuntimeError("Monitoring stopped. The ComfyUI job continues; use Resume build to collect its package later.")
        self.progress.emit(message)


def button(text, action, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(action)
    return widget


def muted(text):
    widget = QLabel(text)
    widget.setObjectName("muted")
    widget.setWordWrap(True)
    return widget


class CropView(QLabel):
    def __init__(self, pixmap):
        super().__init__()
        self.original = pixmap
        self.setPixmap(pixmap.scaled(850, 560, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.setFixedSize(self.pixmap().size())
        self.band = QRubberBand(QRubberBand.Rectangle, self)
        self.start = QPoint()
        self.selection = None

    def mousePressEvent(self, event):
        self.start = event.position().toPoint()
        self.band.setGeometry(QRect(self.start, self.start))
        self.band.show()

    def mouseMoveEvent(self, event):
        self.band.setGeometry(QRect(self.start, event.position().toPoint()).normalized().intersected(self.rect()))

    def mouseReleaseEvent(self, event):
        rect = self.band.geometry()
        if rect.width() > 8 and rect.height() > 8:
            self.selection = [rect.x()/self.width(), rect.y()/self.height(), rect.width()/self.width(), rect.height()/self.height()]


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = settings()
        self.project = new_project()
        self.worker = None
        self.selected = -1
        self.loading = False
        self.pixmap = None
        self.setWindowTitle("refmodBuilder")
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "icon.svg")))
        self.resize(1140, 920)
        self.setMinimumSize(950, 760)
        self.setAcceptDrops(True)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("refmodBuilder")
        title.setObjectName("title")
        header.addWidget(title)
        tagline = muted("One subject. Every reference.")
        tagline.setWordWrap(False)
        header.addWidget(tagline)
        header.addStretch()
        self.settings_button = button("Settings", self.edit_settings)
        header.addWidget(self.settings_button)
        layout.addLayout(header)
        self.connection = muted("ComfyUI · checking connection…")
        self.connection.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.connection)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.build_editor()
        self.build_library()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.stop_monitor = button("Stop monitoring (leave ComfyUI job running)", self.stop_monitoring)
        self.stop_monitor.hide()
        layout.addWidget(self.stop_monitor)
        self.status = muted("Ready. Add references or drag media files into the window.")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.65)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self.position_changed)
        self.player.durationChanged.connect(self.duration_changed)
        self.player.errorOccurred.connect(lambda _e, text: self.status.setText("Preview: " + text))
        self.autosave = QTimer(self)
        self.autosave.setSingleShot(True)
        self.autosave.setInterval(700)
        self.autosave.timeout.connect(self.save)
        self.load_fields()
        QTimer.singleShot(100, self.check)

    def build_editor(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        top = QHBoxLayout()
        self.new_button = button("New package", self.new)
        top.addWidget(self.new_button)
        top.addWidget(muted("Sources are copied into the project. Edits leave originals intact."))
        top.addStretch()
        layout.addLayout(top)
        form = QFormLayout()
        self.name = QLineEdit()
        self.kind = QComboBox()
        self.kind.addItems(["Character", "Asset", "Location"])
        row = QHBoxLayout()
        row.addWidget(self.name, 3)
        row.addWidget(self.kind, 1)
        form.addRow("Package", row)
        self.description = QPlainTextEdit()
        self.description.setPlaceholderText("Describe the subject and what these references should capture…")
        self.description.setMaximumHeight(65)
        form.addRow("Description", self.description)
        layout.addLayout(form)
        self.name.textChanged.connect(self.changed)
        self.kind.currentTextChanged.connect(self.changed)
        self.description.textChanged.connect(self.changed)
        split = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.addWidget(QLabel("References"))
        self.refs = QListWidget()
        self.refs.setIconSize(QSize(44, 44))
        self.refs.setMinimumWidth(225)
        self.refs.setSpacing(4)
        self.refs.currentRowChanged.connect(self.select_reference)
        left_layout.addWidget(self.refs, 1)
        controls = QHBoxLayout()
        controls.addWidget(button("Add…", self.add))
        controls.addWidget(button("Remove", self.remove))
        controls.addWidget(button("↑", lambda: self.move(-1)))
        controls.addWidget(button("↓", lambda: self.move(1)))
        left_layout.addLayout(controls)
        left_layout.addWidget(muted("Images · video · audio\nDrop files anywhere to add them."))
        split.addWidget(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self.preview_stack = QStackedWidget()
        self.image = QLabel("Add references to begin\n\nChoose a file to preview and edit it.")
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setMinimumSize(360, 220)
        self.image.setObjectName("preview")
        self.video = QVideoWidget()
        self.preview_stack.addWidget(self.image)
        self.preview_stack.addWidget(self.video)
        right_layout.addWidget(self.preview_stack, 1)
        playback = QHBoxLayout()
        self.play_button = button("Play / pause", self.play)
        playback.addWidget(self.play_button)
        self.seek = QSlider(Qt.Horizontal)
        self.seek.sliderMoved.connect(lambda position: self.player.setPosition(position))
        playback.addWidget(self.seek, 1)
        self.time_label = muted("0:00 / 0:00")
        playback.addWidget(self.time_label)
        right_layout.addLayout(playback)
        self.details = QWidget()
        details_form = QFormLayout(self.details)
        details_form.setContentsMargins(0, 0, 0, 0)
        self.ref_name = QLineEdit()
        details_form.addRow("Reference name", self.ref_name)
        self.ref_name.textEdited.connect(self.ref_changed)
        self.trim_row = QWidget()
        trim = QHBoxLayout(self.trim_row)
        trim.setContentsMargins(0, 0, 0, 0)
        self.start = QDoubleSpinBox()
        self.end = QDoubleSpinBox()
        for field in (self.start, self.end):
            field.setRange(0, 86400)
            field.setDecimals(3)
            field.setSuffix(" s")
            field.valueChanged.connect(self.ref_changed)
        self.end.setSpecialValueText("End of source")
        trim.addWidget(QLabel("From"))
        trim.addWidget(self.start)
        trim.addWidget(QLabel("To"))
        trim.addWidget(self.end)
        trim.addWidget(button("Set in", lambda: self.start.setValue(self.player.position()/1000)))
        trim.addWidget(button("Set out", lambda: self.end.setValue(self.player.position()/1000)))
        details_form.addRow("Trim", self.trim_row)
        crop_row = QHBoxLayout()
        self.crop_button = button("Crop image…", self.crop)
        self.reset_crop = button("Reset crop", self.clear_crop)
        crop_row.addWidget(self.crop_button)
        crop_row.addWidget(self.reset_crop)
        self.crop_label = muted("Full image")
        crop_row.addWidget(self.crop_label, 1)
        details_form.addRow("Framing", crop_row)
        self.soundtrack = QCheckBox("Include the video's audio as a separate reference")
        self.soundtrack.toggled.connect(self.ref_changed)
        details_form.addRow("Audio", self.soundtrack)
        self.notes = QLineEdit()
        self.notes.setPlaceholderText("Front view, walking cycle, room ambience…")
        self.notes.textEdited.connect(self.ref_changed)
        details_form.addRow("Notes", self.notes)
        right_layout.addWidget(self.details)
        split.addWidget(right)
        split.setSizes([290, 750])
        layout.addWidget(split, 1)
        encoding = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(["Full Reference", "Compressed Reference"])
        self.mode.setToolTip("Full preserves more detail. Compressed uses a 16×16 latent grid with 100 refinement steps; fine identity may be lost.")
        self.resolution = QComboBox()
        self.resolution.addItems(["256", "512", "768", "1024"])
        self.frames = QSpinBox()
        self.frames.setRange(1, 240)
        self.frames.setToolTip("Full mode samples videos on H3's 5, 22, 39… frame grid. Compressed mode limits latent frames.")
        self.tokens = QSpinBox()
        self.tokens.setRange(256, 1048576)
        self.tokens.setSingleStep(1024)
        for label, widget in [("Mode", self.mode), ("Resolution", self.resolution), ("Video frames", self.frames), ("Token limit", self.tokens)]:
            encoding.addWidget(QLabel(label))
            encoding.addWidget(widget)
        self.mode.currentTextChanged.connect(self.changed)
        self.resolution.currentTextChanged.connect(self.changed)
        self.frames.valueChanged.connect(self.changed)
        self.tokens.valueChanged.connect(self.changed)
        layout.addLayout(encoding)
        self.summary = muted("")
        layout.addWidget(self.summary)
        footer = QHBoxLayout()
        self.save_button = button("Save project", self.save)
        footer.addWidget(self.save_button)
        footer.addWidget(button("Project folder", self.open_project_folder))
        footer.addStretch()
        self.build_button = button("Build package", self.build, True)
        footer.addWidget(self.build_button)
        layout.addLayout(footer)
        self.editor = page
        self.tabs.addTab(page, "Build")

    def build_library(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a character, asset, or location…")
        self.search.textChanged.connect(self.refresh_library)
        row.addWidget(self.search, 1)
        self.filter = QComboBox()
        self.filter.addItems(["All types", "Character", "Asset", "Location"])
        self.filter.currentTextChanged.connect(self.refresh_library)
        row.addWidget(self.filter)
        row.addWidget(button("Refresh", self.refresh_library))
        layout.addLayout(row)
        self.library = QListWidget()
        self.library.setIconSize(QSize(64, 64))
        self.library.setSpacing(8)
        self.library.itemDoubleClicked.connect(self.open_library_project)
        self.library.currentItemChanged.connect(self.library_selection)
        layout.addWidget(self.library, 1)
        self.library_info = muted("Saved projects appear here. Double-click one to continue editing.")
        self.library_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.library_info)
        row = QHBoxLayout()
        row.addWidget(button("Open project", self.open_library_project))
        row.addWidget(button("Show latest package", self.show_export))
        row.addStretch()
        row.addWidget(button("Open exports folder", lambda: self.open_folder(Path(self.config["export_dir"]))))
        layout.addLayout(row)
        self.tabs.addTab(page, "Library")
        self.refresh_library()

    def run_task(self, task, done):
        if self.worker is not None:
            return
        self.save()
        self.editor.setEnabled(False)
        self.settings_button.setEnabled(False)
        self.library.setEnabled(False)
        self.progress.show()
        self.worker = Worker(task)
        self.worker.progress.connect(self.task_progress)
        self.worker.result.connect(done)
        self.worker.failed.connect(self.error)
        self.worker.finished.connect(self.task_finished)
        self.worker.start()

    def task_progress(self, message):
        self.status.setText(message)
        if message.startswith("Waiting for this package"):
            self.stop_monitor.show()

    def task_finished(self):
        worker = self.worker
        self.worker = None
        worker.deleteLater()
        self.editor.setEnabled(True)
        self.settings_button.setEnabled(True)
        self.library.setEnabled(True)
        self.progress.hide()
        self.stop_monitor.hide()
        self.stop_monitor.setEnabled(True)
        # Reload durable job state, including failed/submitted builds.
        path = project_dir(self.project) / "project.json"
        if path.exists():
            self.project = load_project(path)
        self.load_fields()
        self.refresh_library()

    def error(self, text):
        self.status.setText(text)
        if "checking connection" in self.connection.text():
            self.connection.setText("ComfyUI unavailable · editing and project browsing remain available")
        QMessageBox.warning(self, "refmodBuilder", text)

    def check(self):
        self.run_task(lambda progress: backend.check_connection(self.config), self.checked)

    def checked(self, result):
        if result["missing"]:
            text = "ComfyUI connected · RefMod nodes not loaded · restart required when you authorize it"
        else:
            text = "ComfyUI connected · ready to build"
        self.connection.setText(text + f" · {result['running']} running / {result['pending']} queued")
        self.status.setText("Connection checked. No jobs submitted or changed.")

    def collect(self):
        self.project.update(name=self.name.text(), kind=self.kind.currentText(), description=self.description.toPlainText(),
                            mode=self.mode.currentText(), resolution=int(self.resolution.currentText()), frames=self.frames.value(), token_limit=self.tokens.value())

    def changed(self, *args):
        if self.loading:
            return
        self.collect()
        self.update_summary()
        self.autosave.start()

    def save(self):
        if self.loading or self.worker:
            return
        self.autosave.stop()
        self.collect()
        if not self.project["references"] and not self.project["description"] and self.project["name"] == "Untitled character":
            return
        try:
            save_project(self.project)
            self.status.setText("Project saved locally.")
            self.refresh_library()
        except (OSError, ValueError) as exc:
            self.error(str(exc))

    def load_fields(self):
        self.loading = True
        self.name.setText(self.project["name"])
        self.kind.setCurrentText(self.project["kind"])
        self.description.setPlainText(self.project["description"])
        self.mode.setCurrentText(self.project["mode"])
        self.resolution.setCurrentText(str(self.project["resolution"]))
        self.frames.setValue(self.project["frames"])
        self.tokens.setValue(self.project["token_limit"])
        self.refresh_refs()
        job = self.project.get("job")
        pending = job and job.get("status") in ("queued", "submitted", "assembling", "submission_unknown")
        self.build_button.setText("Resume build" if pending else "Build package")
        self.loading = False
        self.update_summary()

    def update_summary(self):
        counts = {kind: sum(r["kind"] == kind for r in self.project["references"]) for kind in ("image", "video", "audio")}
        self.summary.setText(f"{counts['image']} images · {counts['video']} videos · {counts['audio']} audio clips  |  Package limit: {self.tokens.value():,} tokens\nExact token count is measured after encoding. Overflow stops export; references are not silently truncated.")

    def refresh_refs(self, select=None):
        previous = self.refs.currentRow() if select is None else select
        self.refs.blockSignals(True)
        self.refs.clear()
        for ref in self.project["references"]:
            item = QListWidgetItem(f"{ref['name']}\n{ref['kind'].upper()}" + (" + AUDIO" if ref.get("soundtrack") else ""))
            item.setToolTip(ref["notes"] or ref["name"])
            if ref["kind"] == "image":
                item.setIcon(QIcon(str(source_path(self.project, ref))))
            self.refs.addItem(item)
        self.refs.blockSignals(False)
        row = min(max(0, previous), len(self.project["references"]) - 1)
        self.refs.setCurrentRow(row)
        if row < 0:
            self.select_reference(-1)

    def select_reference(self, index):
        if not hasattr(self, "player"):
            return
        self.player.stop()
        self.player.setSource(QUrl())
        self.selected = index
        self.pixmap = None
        self.preview_stack.setCurrentIndex(0)
        if index < 0:
            self.image.clear()
            self.image.setText("Add references to begin\n\nChoose a file to preview and edit it.")
            self.details.setEnabled(False)
            self.play_button.setEnabled(False)
            return
        self.details.setEnabled(True)
        ref = self.project["references"][index]
        self.loading = True
        self.ref_name.setText(ref["name"])
        self.start.setValue(ref["start"])
        self.end.setValue(ref["end"])
        self.notes.setText(ref["notes"])
        self.soundtrack.setChecked(ref.get("soundtrack", False))
        self.soundtrack.setEnabled(ref["kind"] == "video")
        self.trim_row.setEnabled(ref["kind"] != "image")
        self.crop_button.setEnabled(ref["kind"] == "image")
        self.reset_crop.setEnabled(bool(ref.get("crop")))
        self.crop_label.setText("Custom crop" if ref.get("crop") else "Full image" if ref["kind"] == "image" else "Video uses the full frame")
        self.play_button.setEnabled(ref["kind"] != "image")
        self.loading = False
        path = source_path(self.project, ref)
        if ref["kind"] == "image":
            try:
                with Image.open(path) as im:
                    im = ImageOps.exif_transpose(im).convert("RGBA")
                    if ref.get("crop"):
                        x, y, w, h = ref["crop"]
                        iw, ih = im.size
                        im = im.crop((round(x*iw), round(y*ih), round((x+w)*iw), round((y+h)*ih)))
                    self.pixmap = QPixmap.fromImage(ImageQt(im))
                self.fit_image()
            except (OSError, ValueError) as exc:
                self.image.setText(str(exc))
        else:
            self.preview_stack.setCurrentIndex(1 if ref["kind"] == "video" else 0)
            self.image.clear()
            self.image.setText("AUDIO REFERENCE\n\n" + ref["name"] + "\n\nPress Play to listen.")
            self.player.setSource(QUrl.fromLocalFile(str(path)))

    def fit_image(self):
        if self.pixmap:
            self.image.setPixmap(self.pixmap.scaled(self.image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "image"):
            self.fit_image()

    def ref_changed(self, *args):
        if self.loading or self.selected < 0:
            return
        ref = self.project["references"][self.selected]
        ref.update(name=self.ref_name.text(), start=self.start.value(), end=self.end.value(), notes=self.notes.text(), soundtrack=self.soundtrack.isChecked())
        self.refs.item(self.selected).setText(f"{ref['name']}\n{ref['kind'].upper()}" + (" + AUDIO" if ref["soundtrack"] else ""))
        self.autosave.start()

    def add(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add references", str(Path.home()), "Media (" + " ".join("*" + x for x in sorted(IMAGE_EXT | VIDEO_EXT | AUDIO_EXT)) + ")")
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths):
        if self.worker:
            return
        self.collect()
        snapshot = copy.deepcopy(self.project)
        def done(project):
            self.project = project
            self.status.setText(f"Added {len(paths)} references. Source copies saved in the project.")
        self.run_task(lambda progress: import_media(snapshot, paths), done)

    def dragEnterEvent(self, event):
        if not self.worker and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.add_paths(paths)
            event.acceptProposedAction()

    def remove(self):
        if self.selected >= 0:
            self.project["references"].pop(self.selected)
            self.refresh_refs()
            self.update_summary()
            self.save()

    def move(self, delta):
        index = self.selected
        target = index + delta
        refs = self.project["references"]
        if index >= 0 and 0 <= target < len(refs):
            refs[index], refs[target] = refs[target], refs[index]
            self.refresh_refs(target)
            self.save()

    def crop(self):
        if self.selected < 0:
            return
        ref = self.project["references"][self.selected]
        with Image.open(source_path(self.project, ref)) as im:
            pixmap = QPixmap.fromImage(ImageQt(ImageOps.exif_transpose(im).convert("RGBA")))
        dialog = QDialog(self)
        dialog.setWindowTitle("Crop reference")
        layout = QVBoxLayout(dialog)
        layout.addWidget(muted("Drag a rectangle around the part of this image to keep."))
        view = CropView(pixmap)
        layout.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted and view.selection:
            ref["crop"] = view.selection
            self.select_reference(self.selected)
            self.save()

    def clear_crop(self):
        if self.selected >= 0:
            self.project["references"][self.selected]["crop"] = None
            self.select_reference(self.selected)
            self.save()

    def play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            if self.player.position() < self.start.value()*1000 or (self.end.value() and self.player.position() >= self.end.value()*1000):
                self.player.setPosition(round(self.start.value()*1000))
            self.player.play()

    def position_changed(self, position):
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        self.time_label.setText(f"{position/1000:.1f} / {self.player.duration()/1000:.1f} s")
        if self.end.value() and position >= self.end.value()*1000 and self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()

    def duration_changed(self, duration):
        self.seek.setRange(0, duration)
        if self.selected >= 0:
            ref = self.project["references"][self.selected]
            if ref["kind"] != "image":
                ref["duration"] = duration / 1000

    def new(self):
        if self.worker:
            return
        self.save()
        self.project = new_project()
        self.load_fields()
        self.tabs.setCurrentIndex(0)
        self.name.setFocus()
        self.name.selectAll()

    def build(self):
        self.player.stop()
        self.collect()
        project = copy.deepcopy(self.project)
        self.run_task(lambda progress: backend.build(project, self.config, progress), self.built)

    def stop_monitoring(self):
        if self.worker:
            self.worker.requestInterruption()
            self.status.setText("Stopping local monitoring at the next checkpoint. ComfyUI is unaffected.")
            self.stop_monitor.setEnabled(False)

    def built(self, project):
        self.project = project
        export = project["exports"][-1]
        self.status.setText(f"Saved {export['members']} reference members: {export['path']}")

    def refresh_library(self, *args):
        if not hasattr(self, "library"):
            return
        self.library.clear()
        if not LIBRARY.exists():
            return
        for path in sorted(LIBRARY.glob("*/project.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                project = load_project(path)
            except (OSError, ValueError, KeyError):
                continue
            if self.filter.currentText() not in ("All types", project["kind"]):
                continue
            if self.search.text().lower() not in (project["name"] + " " + project["description"]).lower():
                continue
            exports = len(project["exports"])
            item = QListWidgetItem(f"{project['name']}\n{project['kind']} · {len(project['references'])} references · {exports} exports" + (" · Draft" if not exports else ""))
            item.setData(Qt.UserRole, str(path))
            for ref in project["references"]:
                if ref["kind"] == "image":
                    item.setIcon(QIcon(str(source_path(project, ref))))
                    break
            self.library.addItem(item)

    def library_selection(self, item, previous=None):
        if not item:
            return
        try:
            project = load_project(item.data(Qt.UserRole))
            self.library_info.setText(project["description"] or "No description")
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))

    def open_library_project(self, *args):
        if self.worker:
            return
        item = self.library.currentItem()
        if not item:
            return
        path = item.data(Qt.UserRole)
        self.save()
        self.project = load_project(path)
        self.load_fields()
        self.tabs.setCurrentIndex(0)

    def show_export(self):
        item = self.library.currentItem()
        if not item:
            return
        project = load_project(item.data(Qt.UserRole))
        if project["exports"]:
            self.open_folder(Path(project["exports"][-1]["path"]).parent)
        else:
            self.status.setText("This project has no exported packages yet.")

    def open_folder(self, path):
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_project_folder(self):
        self.save()
        self.open_folder(project_dir(self.project))

    def edit_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("refmodBuilder settings")
        dialog.resize(720, 380)
        layout = QVBoxLayout(dialog)
        layout.addWidget(muted("ComfyUI runs separately. This app never restarts it, interrupts jobs, or clears its queue."))
        form = QFormLayout()
        fields = {}
        for key, label in [("comfy_url", "ComfyUI URL"), ("comfy_dir", "ComfyUI folder (local or mounted)"), ("refmod_dir", "Backend RefMod folder (local or mounted)"), ("export_dir", "Export folder on this computer"), ("video_vae", "Video VAE"), ("audio_vae", "Audio VAE")]:
            field = QLineEdit(self.config[key])
            form.addRow(label, field)
            fields[key] = field
        layout.addLayout(form)
        layout.addWidget(muted("For a remote backend, mount its ComfyUI and RefMod folders here. The URL connects to the server; the mounted folders transfer references and encoded results. Exports are saved on this computer."))
        layout.addWidget(muted("Project library: " + str(LIBRARY)))
        layout.addWidget(muted("Audio reference support does not guarantee voice identity or synchronized generation."))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            self.config.update({key: field.text().strip() for key, field in fields.items()})
            atomic_json(DATA / "settings.json", self.config)
            self.check()

    def closeEvent(self, event: QCloseEvent):
        if self.worker:
            self.status.setText("An operation is still running. Let it finish before closing; ComfyUI jobs are not interrupted.")
            event.ignore()
            return
        self.save()
        self.player.stop()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("refmodBuilder")
    app.setDesktopFileName("refmodBuilder")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    DATA.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(DATA / "app.lock"))
    if not lock.tryLock(100):
        QMessageBox.information(None, "refmodBuilder", "refmodBuilder is already running. Use its existing window.")
        return
    window = Window()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
