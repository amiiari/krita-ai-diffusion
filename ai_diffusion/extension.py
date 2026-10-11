import sys
from collections.abc import Callable
from pathlib import Path

from krita import DockWidgetFactory, DockWidgetFactoryBase, Extension, Krita, Window  # type: ignore
from PyQt5.QtWidgets import QAction

from . import __version__, eventloop
from .captions.bridge import bridge as caption_bridge
from .captions.docker import CaptionsDocker
from .model.model import Workspace
from .model.root import root
from .persistence import export_prompt_to_file
from .settings import settings
from .ui import actions
from .ui.diffusion import ImageDiffusionWidget
from .ui.settings import SettingsDialog
from .util import client_logger as log


class AIToolsExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self._actions: dict[str, QAction] = {}

        log.info(f"Extension initialized, Version: {__version__}, Python: {sys.version}")

        extension_dir = Path(__file__).parent
        debugpy_path = extension_dir / "debugpy" / "src"
        if debugpy_path.exists():
            try:
                sys.path.insert(0, str(debugpy_path))
                import debugpy

                debugpy.listen(("127.0.0.1", 5678), in_process_debug_adapter=True)
                log.info("Developer mode: debugpy listening on port 5678")
            except ImportError:
                pass

        pykrita_dir = extension_dir.parent
        if pykrita_dir.name != "pykrita" and not (pykrita_dir / ".git").exists():
            log.warning(
                "Plugin is not installed in a 'pykrita' directory, this may break user files "
                f"and settings. Detected installation path is: {pykrita_dir}"
            )

        eventloop.setup()
        settings.load()
        root.init()
        self._settings_dialog = SettingsDialog(root.server)

        notifier = Krita.instance().notifier()
        notifier.setActive(True)
        notifier.applicationClosing.connect(self.shutdown)  # type: ignore
        notifier.imageSaved.connect(self.image_saved)  # type: ignore
        caption_bridge()  # Save All for the Content Manager, whether the docker is open or not

    def setup(self):
        eventloop.run(root.autostart(self._settings_dialog.connection.update_ui))

    def shutdown(self):
        root.server.terminate()
        eventloop.stop()

    def image_saved(self, filename: str):
        try:
            # Saving a copy (eg. "Save As" a png from a .kra) reports a filename which does
            # not belong to any open document, so fall back to the document being edited.
            model = root.model_for_filename(filename) or root.model_for_active_document()
            if model is not None:
                export_prompt_to_file(model, filename)
        except Exception as e:
            log.warning(f"Failed to embed metadata into {filename}: {e}")

    def _create_action(self, window: Window, name: str, func: Callable[[], None]):
        action = window.createAction(f"ai_diffusion_{name}", "", "")
        action.triggered.connect(func)
        self._actions[name] = action

    def createActions(self, window):
        self._create_action(window, "settings", self._settings_dialog.show)
        self._create_action(window, "generate", actions.generate)
        self._create_action(window, "cancel", actions.cancel_active)
        self._create_action(window, "cancel_queued", actions.cancel_queued)
        self._create_action(window, "cancel_all", actions.cancel_all)
        self._create_action(window, "toggle_preview", actions.toggle_preview)
        self._create_action(window, "apply", actions.apply)
        self._create_action(window, "apply_alternative", actions.apply_alternative)
        self._create_action(window, "create_region", actions.create_region)
        self._create_action(
            window, "switch_workspace_generation", actions.set_workspace(Workspace.generation)
        )
        self._create_action(
            window, "switch_workspace_upscaling", actions.set_workspace(Workspace.upscaling)
        )
        self._create_action(window, "switch_workspace_live", actions.set_workspace(Workspace.live))
        self._create_action(
            window, "switch_workspace_graph", actions.set_workspace(Workspace.custom)
        )
        self._create_action(window, "toggle_workspace", actions.toggle_workspace)
        self._create_action(window, "toggle_edit_mode", actions.toggle_edit_mode)


Krita.instance().addExtension(AIToolsExtension(Krita.instance()))
Krita.instance().addDockWidgetFactory(
    DockWidgetFactory("imageDiffusion", DockWidgetFactoryBase.DockRight, ImageDiffusionWidget)  # type: ignore
)
# the id Caption Studio had as its own plugin: Krita keeps it where it was docked
Krita.instance().addDockWidgetFactory(
    DockWidgetFactory("caption_studio", DockWidgetFactoryBase.DockRight, CaptionsDocker)  # type: ignore
)
