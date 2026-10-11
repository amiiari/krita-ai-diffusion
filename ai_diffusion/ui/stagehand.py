"""The cards of an image made with Forge's Stagehand (see ..stagehand): on/off, prompt,
Undesired Content, and its area -- shown as a selection, so it's never part of the image."""

from __future__ import annotations

from PyQt5.QtCore import QMetaObject, QObject
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QPlainTextEdit,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import stagehand
from ..localization import translate as _
from ..model.model import DocumentModel


class StagehandWidget(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._model: DocumentModel | None = None
        self._connection: QMetaObject.Connection | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # many characters scroll instead of pushing the history out of the docker
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMaximumHeight(360)
        content = QWidget(scroll)
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self.setVisible(False)

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: DocumentModel):
        if self._connection:
            QObject.disconnect(self._connection)
        self._model = model
        self._connection = model.stagehand_changed.connect(self._rebuild)
        self._rebuild()

    def _rebuild(self, *_args):
        while self._layout.count():
            if widget := self._layout.takeAt(0).widget():
                widget.deleteLater()
        state = self._model.stagehand if self._model else None
        self.setVisible(state is not None)
        if state is None:
            return
        forge = QCheckBox(_("Generate with Forge (Stagehand)"), self)
        forge.setToolTip(
            _(
                "Generate and inpaint through Forge with this image's settings and the cards that are on."
                " The selection is the inpaint mask, Strength is the denoise. Off: ComfyUI as usual."
            )
        )
        forge.setChecked(state.enabled)
        forge.toggled.connect(lambda on: self._edit(state, setattr(state, "enabled", on)))
        self._layout.addWidget(forge)
        for card in state.cards:
            self._layout.addWidget(self._card(state, card))
        self._layout.addStretch()

    @staticmethod
    def _edit(state: stagehand.State, _change=None):
        state.changed.emit()  # saved with the document

    def _card(self, state: stagehand.State, card: stagehand.Card):
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        on = QCheckBox(card.title, frame)
        on.setChecked(card.enabled)
        on.setToolTip(
            _("Off: she's left out of the generation, as switching her card off in Forge")
        )
        select = QToolButton(frame)
        select.setText(_("Select"))
        select.setToolTip(_("Select where her prompt applies -- inpaint it with Generate"))
        place = QToolButton(frame)
        place.setText(_("Set box"))
        place.setToolTip(_("Make the current selection's bounds her box"))
        uc_button = QToolButton(frame)
        uc_button.setText(_("UC"))
        uc_button.setCheckable(True)
        uc_button.setToolTip(_("Her Undesired Content"))
        head = QHBoxLayout()
        head.addWidget(on, 1)
        for button in (select, place, uc_button):
            head.addWidget(button)
        layout.addLayout(head)

        prompt = QPlainTextEdit(card.text, frame)
        prompt.setPlaceholderText(_("Her prompt"))
        prompt.setMaximumHeight(64)
        uc = QPlainTextEdit(card.uc, frame)
        uc.setPlaceholderText(_("Undesired Content"))
        uc.setMaximumHeight(48)
        uc.setVisible(bool(card.uc))
        uc_button.setChecked(bool(card.uc))
        layout.addWidget(prompt)
        layout.addWidget(uc)

        def set_enabled(value: bool):
            card.enabled = value
            prompt.setEnabled(value)

        def select_area():
            assert self._model is not None
            doc = self._model.document
            doc.select_rects(stagehand.card_area(state.cards, card, *doc.extent))

        def set_box():
            assert self._model is not None
            doc = self._model.document
            if bounds := doc.selection_bounds:
                card.places = [stagehand.rect_place(tuple(bounds), *doc.extent, grid=state.grid)]
                self._edit(state)

        def edit(field: str, widget: QPlainTextEdit):
            setattr(card, field, widget.toPlainText())
            self._edit(state)

        on.toggled.connect(lambda value: self._edit(state, set_enabled(value)))
        select.clicked.connect(select_area)
        place.clicked.connect(set_box)
        uc_button.toggled.connect(uc.setVisible)
        prompt.textChanged.connect(lambda: edit("text", prompt))
        uc.textChanged.connect(lambda: edit("uc", uc))
        set_enabled(card.enabled)
        return frame
