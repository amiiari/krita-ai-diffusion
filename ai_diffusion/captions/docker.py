"""The Caption Studio docker, compact: caption text, preset and Add text on two rows, the
stamps as a grid of small icons, and Export / Save All on one row. What used to be notes
under each part are tooltips."""

from __future__ import annotations

import os
import subprocess

from krita import DockWidget, Krita
from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QFontMetrics, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import stamps, text
from .bridge import bridge

ICON = 40  # stamp thumbnails


def _button(label: str, tip: str, parent: QWidget, expand=True):
    button = QToolButton(parent)
    button.setText(label)
    button.setToolTip(tip)
    if expand:
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return button


def _row(*widgets: QWidget):
    row = QHBoxLayout()
    row.setSpacing(2)
    for w in widgets:
        row.addWidget(w, 1 if isinstance(w, QComboBox) else 0)
    return row


class CaptionsDocker(DockWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Caption Studio")
        self.cfg = text.load_config()

        root = QWidget(self)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(2)

        # QPlainTextEdit, not a line edit: captions need newlines (Enter inserts one)
        self.text_input = QPlainTextEdit(root)
        self.text_input.setPlaceholderText("Caption text… (Enter = new line)")
        self.text_input.setToolTip("The caption's color is the color wheel's (foreground) color")
        lines = QFontMetrics(self.text_input.font()).lineSpacing()
        self.text_input.setFixedHeight(2 * lines + 12)
        outer.addWidget(self.text_input)

        self.preset_combo = QComboBox(root)
        for p in self.cfg["text_presets"]:
            self.preset_combo.addItem(p["name"])
        # shrinks with the docker (a long preset name clips)
        self.preset_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.preset_combo.setMinimumWidth(64)
        self.preset_combo.setToolTip("Text preset (presets.json in the captions folder)")
        add = _button(
            "Add text", "Add the caption as a text layer in the CAPTIONS group", root, False
        )
        add.clicked.connect(self.add_text_layer)
        self.style_button = _button("⋯", "", root, False)
        self.style_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.style_button)
        self.style_action = menu.addAction("", self.remember_style)
        self.style_button.setMenu(menu)
        self._refresh_style()
        outer.addLayout(_row(self.preset_combo, add, self.style_button))

        self.thoughts = _button("Thoughts", "Insert the thought bubbles (thoughts.kra)", root)
        self.thoughts.clicked.connect(self.add_thoughts)
        folder = _button("Folder", f"Open the stamps folder: {stamps.ASSETS_DIR}", root, False)
        folder.clicked.connect(self.open_assets)
        refresh = _button("⟳", "Reload the stamps", root, False)
        refresh.clicked.connect(self.reload)
        outer.addLayout(_row(self.thoughts, folder, refresh))

        self.stamp_list = QListWidget(root)
        self.stamp_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.stamp_list.setIconSize(QSize(ICON, ICON))
        self.stamp_list.setGridSize(QSize(ICON + 6, ICON + 6))
        self.stamp_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.stamp_list.setMovement(QListWidget.Movement.Static)
        self.stamp_list.setSpacing(0)
        self.stamp_list.setMinimumHeight(ICON + 10)
        self.stamp_list.setToolTip("Double-click a stamp to insert it")
        self.stamp_list.itemDoubleClicked.connect(self.insert_stamp)
        outer.addWidget(self.stamp_list, 1)

        export = _button(
            "Export",
            "Export <name>-caption.jpg next to the document, a one-off. Captioning a .ora?"
            " Just Ctrl+S or Save All: the Content Manager exports -caption.jpg on every save.",
            root,
        )
        export.clicked.connect(self.export_caption)
        save_all = _button(
            "Save All",
            "Save every open, modified .ora (the Content Manager's SAVE ALL presses this too)",
            root,
        )
        save_all.clicked.connect(lambda: bridge().save_all())
        outer.addLayout(_row(export, save_all))

        self.status = QLabel(root)
        self.status.setStyleSheet("color: #888; font-size: 10px;")
        self.status.setVisible(False)
        bridge().status_changed.connect(self._show_status)
        outer.addWidget(self.status)

        self.setWidget(root)
        self.reload()

    def _show_status(self, message: str):
        self.status.setText(message)
        self.status.setVisible(bool(message))

    def _doc(self):
        doc = Krita.instance().activeDocument()
        if not doc:
            QMessageBox.information(None, "Caption Studio", "No document open.")
        return doc

    # ---------------------------------------------------------------- text
    def _refresh_style(self):
        if os.path.exists(text.TEXT_STYLE_XML):
            self.style_button.setToolTip("Layer style: applied to every caption automatically")
            self.style_action.setText("Re-capture the layer style from the selected layer")
        else:
            self.style_button.setToolTip("No layer style captured yet")
            self.style_action.setText("Remember the selected layer's style for every caption")

    def remember_style(self):
        doc = self._doc()
        if not doc:
            return
        node = doc.activeNode()
        if not node:
            QMessageBox.information(None, "Caption Studio", "Select a layer first.")
            return
        try:
            asl = node.layerStyleToAsl()
        except Exception as e:
            QMessageBox.warning(None, "Caption Studio", f"Could not read the style:\n{e}")
            return
        if not asl or not asl.strip():
            QMessageBox.information(
                None,
                "Caption Studio",
                f"'{node.name()}' has no layer style on it.\n\n"
                "Apply your style to that layer first (Layer ▸ Layer Style…), then try again.",
            )
            return
        with open(text.TEXT_STYLE_XML, "w", encoding="utf-8") as f:
            f.write(asl)
        self._refresh_style()
        QMessageBox.information(
            None,
            "Caption Studio",
            f"Captured the style from '{node.name()}'.\nEvery caption from now on gets it.",
        )

    def add_text_layer(self):
        doc = self._doc()
        if not doc:
            return
        caption = self.text_input.toPlainText().strip() or "caption"
        preset = self.cfg["text_presets"][max(0, self.preset_combo.currentIndex())]
        wheel = preset.get("color", "wheel") == "wheel"
        color = text.foreground_hex() if wheel else preset["color"]
        # style failures are shown, never swallowed
        _layer, style_error = text.add_text(doc, caption, preset, color)
        if style_error:
            QMessageBox.warning(
                None, "Caption Studio", f"Text added, but the layer style failed:\n{style_error}"
            )

    # ---------------------------------------------------------------- stamps
    def reload(self):
        self.stamp_list.clear()
        os.makedirs(stamps.ASSETS_DIR, exist_ok=True)
        thoughts = stamps._thoughts_asset()
        self.thoughts.setEnabled(thoughts is not None)
        skip = {os.path.basename(thoughts).lower()} if thoughts else set()
        for f in sorted(os.listdir(stamps.ASSETS_DIR), key=str.lower):
            low = f.lower()
            if low in skip:
                continue  # has its own button
            path = os.path.join(stamps.ASSETS_DIR, f)
            if low.endswith(stamps.IMG_EXTS):
                icon = QIcon(QPixmap(path))
            elif low.endswith(".kra"):
                img = stamps._kra_merged(path, quiet=True)
                if img is None:
                    continue
                icon = QIcon(QPixmap.fromImage(img))
            else:
                continue
            item = QListWidgetItem(icon, "")  # names as tooltips: icons only, more per row
            item.setToolTip(os.path.splitext(f)[0])
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.stamp_list.addItem(item)

    def open_assets(self):
        os.makedirs(stamps.ASSETS_DIR, exist_ok=True)
        subprocess.Popen(["explorer", os.path.normpath(stamps.ASSETS_DIR)])

    def add_thoughts(self):
        doc = self._doc()
        if not doc:
            return
        path = stamps._thoughts_asset()
        if not path:
            QMessageBox.warning(None, "Stamps", f"thoughts.kra not found in:\n{stamps.ASSETS_DIR}")
            return
        stamps.insert_kra(doc, path)

    def insert_stamp(self, item: QListWidgetItem):
        if doc := self._doc():
            stamps.insert_stamp(doc, item.data(Qt.ItemDataRole.UserRole))

    # ---------------------------------------------------------------- export
    def export_caption(self):
        doc = self._doc()
        if not doc:
            return
        dest = text.caption_destination(doc)
        if dest:
            folder, stem = dest
        else:
            folder = QFileDialog.getExistingDirectory(None, "Export folder")
            if not folder:
                return
            stem = "untitled"
        out = text.export_caption_jpg(doc, folder, stem)
        self._show_status("Exported " + os.path.basename(out))

    def canvasChanged(self, canvas):
        pass
