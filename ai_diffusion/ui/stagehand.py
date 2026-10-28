"""The cards of an image made with Forge's Stagehand (see ..stagehand): on/off, prompt,
Undesired Content, and its area -- shown as a selection, so it's never part of the image.

Room in a docker is short, so everything folds: the main prompt (the cards say who's who),
each card down to its header, and the panel and every prompt box are dragged to the size
wanted (kept in settings)."""

from __future__ import annotations

from collections.abc import Callable

from PyQt5.QtCore import QMetaObject, QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .. import stagehand
from ..localization import translate as _
from ..model.model import DocumentModel
from ..settings import settings
from ..util import ensure
from . import theme
from .widget import ResizeHandle, TextPromptWidget


class StagehandWidget(QWidget):
    main_prompt_hidden = pyqtSignal(bool)
    forge_toggled = pyqtSignal(bool)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._model: DocumentModel | None = None
        self._connection: QMetaObject.Connection | None = None
        self._selection_connection: QMetaObject.Connection | None = None
        self._box_buttons: list[QToolButton] = []  # enabled while part of the image is selected
        self._collapsed: set[int] = set()  # cards folded to their header (this session)
        self._prompts: list[TextPromptWidget] = []
        self._folds: dict[int, Callable[[bool], None]] = {}  # card number -> fold it (or not)
        self._uc_buttons: list[tuple[QToolButton, stagehand.Card]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        self._forge = QCheckBox(_("Forge"), self)
        self._forge.setToolTip(
            _(
                "On: generate and inpaint through Forge with this image's own settings and the cards"
                " that are on. Off (the default): Krita's own inpainting (ComfyUI), with the cards of"
                " the characters under the selection added to the prompt."
            )
        )
        self._main = QToolButton(self)
        self._main.setText(_("Prompt"))
        self._main.setCheckable(True)
        self._main.setToolTip(_("Show or hide the main prompt box, for more room for the cards"))
        self._main.toggled.connect(self._show_main_prompt)
        # AI Diffusion's own "Negative Prompt" setting: hidden, it is still used
        self._negative = QToolButton(self)
        self._negative.setText(_("Negative"))
        self._negative.setCheckable(True)
        self._negative.setChecked(settings.show_negative_prompt)
        self._negative.setToolTip(
            _(
                "Show or hide every negative prompt: the main one and the characters' Undesired"
                " Content (hidden, they are still used). A card's UC button shows just hers."
            )
        )
        self._negative.toggled.connect(self._show_negative)
        settings.changed.connect(self._settings_changed)
        self._fold = QToolButton(self)
        self._fold.setToolTip(_("Fold every card to its header, or unfold them all"))
        self._fold.clicked.connect(self._fold_all)
        self._show_all = QToolButton(self)
        self._show_all.setText(_("All"))
        self._show_all.setToolTip(_("Select every character's area (the cards that are on)"))
        self._show_all.clicked.connect(self._select_all)
        self._forge.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._forge.setMinimumWidth(48)
        # the bar goes at the very top of the docker (GenerationWidget puts it there): it
        # shows / hides what's below it
        self.header = QWidget(parent)
        head = QHBoxLayout(self.header)
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self._forge, 1)
        head.addWidget(self._main)
        head.addWidget(self._negative)
        head.addWidget(self._fold)
        head.addWidget(self._show_all)
        self.header.setVisible(False)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        # a narrow docker squeezes the cards (titles clip) instead of scrolling sideways
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(settings.stagehand_panel_height)
        content = QWidget(self._scroll)
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._scroll.setWidget(content)
        outer.addWidget(self._scroll)
        outer.addSpacing(8)  # room for the panel's resize handle

        self._handle = ResizeHandle(self)
        self._handle.setToolTip(_("Drag to make the cards panel taller or shorter"))
        self._handle.handle_dragged.connect(self._drag_panel)
        # only as tall as it needs: spare room in the docker goes to the history
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.setVisible(False)

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: DocumentModel):
        for connection in (self._connection, self._selection_connection):
            if connection:
                QObject.disconnect(connection)
        self._model = model
        self._collapsed.clear()
        self._connection = model.stagehand_changed.connect(self._rebuild)
        self._selection_connection = model.document.selection_bounds_changed.connect(
            self._update_box_buttons
        )
        self._rebuild()

    def resizeEvent(self, a0):
        super().resizeEvent(a0)
        self._handle.move(
            (self.width() - self._handle.width()) // 2, self.height() - self._handle.height()
        )
        self._handle.raise_()

    def _rebuild(self, *_args):
        while self._layout.count():
            if widget := self._layout.takeAt(0).widget():
                widget.deleteLater()
        self._prompts = []
        self._folds = {}
        self._uc_buttons = []
        self._box_buttons = []
        state = self._model.stagehand if self._model else None
        self.setVisible(state is not None)
        self.header.setVisible(state is not None)
        self.main_prompt_hidden.emit(state is not None and settings.stagehand_hide_main_prompt)
        if state is None:
            return
        with theme.SignalBlocker(self._forge), theme.SignalBlocker(self._main):
            self._forge.setChecked(state.enabled)
            self._main.setChecked(not settings.stagehand_hide_main_prompt)
        try:
            self._forge.toggled.disconnect()
        except TypeError:
            pass  # nothing connected yet
        self._forge.toggled.connect(lambda on: self._edit(state, setattr(state, "enabled", on)))
        self._forge.toggled.connect(self.forge_toggled)
        for card in state.cards:
            self._layout.addWidget(self._card(state, card))
        self._layout.addStretch()
        self._update_fold_button()
        self._update_box_buttons()
        self._fit()

    @staticmethod
    def _edit(state: stagehand.State, _change=None):
        state.changed.emit()  # saved with the document

    def _show_main_prompt(self, shown: bool):
        settings.stagehand_hide_main_prompt = not shown
        settings.save()
        self.main_prompt_hidden.emit(not shown)

    def _show_negative(self, shown: bool):
        settings.show_negative_prompt = shown
        settings.save()

    def _settings_changed(self, key: str, value):
        if key == "show_negative_prompt":  # also when changed in AI Diffusion's settings
            with theme.SignalBlocker(self._negative):
                self._negative.setChecked(bool(value))
            if self._model and self._model.stagehand is not None:  # the main negative with it
                self.main_prompt_hidden.emit(settings.stagehand_hide_main_prompt)
            # the characters' Undesired Content with it (those that have any)
            for button, card in self._uc_buttons:
                button.setChecked(bool(value) and bool(card.uc.strip()))

    def _select_all(self):
        state = self._model.stagehand if self._model else None
        if state is None:
            return
        doc = self._model.document
        rects = [
            r
            for c in state.cards
            if c.enabled
            for r in stagehand.card_area(state.cards, c, *doc.extent)
        ]
        _note_whole_image(self._show_all, doc, doc.select_rects(rects))

    def _update_box_buttons(self):
        enabled = self._model is not None and bool(self._model.document.partial_selection_bounds())
        for button in self._box_buttons:
            button.setEnabled(enabled)

    def _fold_all(self):
        state = self._model.stagehand if self._model else None
        if state is None:
            return
        fold = not self._collapsed >= {c.number for c in state.cards}
        for set_folded in self._folds.values():
            set_folded(fold)

    def _update_fold_button(self):
        state = self._model.stagehand if self._model else None
        folded = state is not None and self._collapsed >= {c.number for c in state.cards}
        self._fold.setText(_("Unfold") if folded else _("Fold"))

    def _drag_panel(self, y_pos: int):
        height = max(60, min(y_pos - self._scroll.y(), 2000))
        height -= height % 10
        if height != settings.stagehand_panel_height:
            settings.stagehand_panel_height = height
            settings.save()
            self._fit()

    def _fit(self):
        # measured once Qt has laid out what just folded / unfolded
        QTimer.singleShot(0, self._fit_now)

    def _fit_now(self):
        content = self._layout.sizeHint().height()
        self._scroll.setFixedHeight(max(min(settings.stagehand_panel_height, content), 40))

    def _drag_prompt(self, y_pos: int):
        fm = (
            QFontMetrics(ensure(self._prompts[0].document()).defaultFont())
            if self._prompts
            else None
        )
        if fm is None:
            return
        lines = round((y_pos - 5) / fm.lineSpacing())
        if 1 <= lines <= theme.prompt_max_line_count and lines != settings.stagehand_line_count:
            settings.stagehand_line_count = lines
            settings.save()
            for prompt in self._prompts:  # every card's box: they line up
                prompt.line_count = lines
            self._fit()

    def _card(self, state: stagehand.State, card: stagehand.Card):
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        fold = QToolButton(frame)
        fold.setAutoRaise(True)
        folded = card.number in self._collapsed
        fold.setArrowType(Qt.ArrowType.RightArrow if folded else Qt.ArrowType.DownArrow)
        fold.setToolTip(_("Fold or unfold this card"))
        on = QCheckBox(card.title, frame)
        on.setChecked(card.enabled)
        # a long name clips rather than pushing the buttons out of the docker
        on.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        on.setMinimumWidth(48)
        on.setToolTip(
            card.title
            + "\n"
            + _("Off: she's left out of the generation, as switching her card off in Forge")
        )
        select = QToolButton(frame)
        select.setText(_("Select"))
        select.setToolTip(_("Select where her prompt applies -- inpaint it with Generate"))
        place = QToolButton(frame)
        place.setText(_("Box"))
        place.setToolTip(
            _("Make the current selection's bounds her box (select an area first, any tool)")
        )
        self._box_buttons.append(place)
        uc_button = QToolButton(frame)
        uc_button.setText(_("UC"))
        uc_button.setCheckable(True)
        uc_button.setToolTip(_("Her Undesired Content"))
        head = QHBoxLayout()
        head.addWidget(fold)
        head.addWidget(on, 1)
        for button in (select, place, uc_button):
            head.addWidget(button)
        layout.addLayout(head)

        prompt = TextPromptWidget(line_count=settings.stagehand_line_count, parent=frame)
        prompt.text = card.text
        prompt.setPlaceholderText(_("Her prompt"))
        prompt.is_resizable = True
        prompt.handle_dragged.connect(self._drag_prompt)
        uc = TextPromptWidget(line_count=2, is_negative=True, parent=frame)
        uc.text = card.uc
        uc.setPlaceholderText(_("Her Undesired Content"))
        shown = settings.show_negative_prompt and bool(card.uc.strip())
        uc_button.setChecked(shown)
        self._uc_buttons.append((uc_button, card))
        prompt.setVisible(not folded)
        uc.setVisible(not folded and shown)
        layout.addWidget(prompt)
        layout.addWidget(uc)
        self._prompts.append(prompt)

        def set_enabled(value: bool):
            card.enabled = value
            prompt.setEnabled(value)

        def set_folded(now: bool):
            if now:
                self._collapsed.add(card.number)
            else:
                self._collapsed.discard(card.number)
            fold.setArrowType(Qt.ArrowType.RightArrow if now else Qt.ArrowType.DownArrow)
            prompt.setVisible(not now)
            uc.setVisible(not now and uc_button.isChecked())
            self._update_fold_button()

        self._fit()

        self._folds[card.number] = set_folded

        def select_area():
            assert self._model is not None
            doc = self._model.document
            selected = doc.select_rects(stagehand.card_area(state.cards, card, *doc.extent))
            _note_whole_image(select, doc, selected)

        def set_box():
            assert self._model is not None
            doc = self._model.document
            bounds = doc.partial_selection_bounds()  # read now: the polled bounds can lag
            if not bounds:  # Box is disabled then; a selection changed since the last poll
                return
            card.places = [stagehand.rect_place(tuple(bounds), *doc.extent, grid=state.grid)]
            self._edit(state)
            select_area()  # show her new box
            place.setText("✓")  # and that it took
            QTimer.singleShot(1200, lambda: place.setText(_("Box")))

        def edit(field: str, text: str):
            setattr(card, field, text)
            self._edit(state)

        fold.clicked.connect(lambda: set_folded(card.number not in self._collapsed))
        on.toggled.connect(lambda value: self._edit(state, set_enabled(value)))
        select.clicked.connect(select_area)
        place.clicked.connect(set_box)
        uc_button.toggled.connect(
            lambda shown: uc.setVisible(shown and card.number not in self._collapsed)
        )
        uc_button.toggled.connect(lambda _: self._fit())  # no gap where a hidden box was
        prompt.text_changed.connect(lambda text: edit("text", text))
        uc.text_changed.connect(lambda text: edit("uc", text))
        if self._model:
            prompt.activated.connect(self._model.generate)  # Shift+Enter, as in the main prompt
            uc.activated.connect(self._model.generate)
        set_enabled(card.enabled)
        return frame


def _note_whole_image(button: QWidget, doc, selected: bool):
    """Select / All gave no area, or all of the image: then Generate repaints everything (no
    inpaint mask), so say so where the click was."""
    if selected and doc.partial_selection_bounds():
        return
    if selected:
        text = _("That is the whole image: Generate repaints everything")
    else:
        text = _("No area to select: Generate repaints the whole image")
    QToolTip.showText(button.mapToGlobal(QPoint(0, button.height())), text, button)
