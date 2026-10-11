"""Save All: saves every open, modified .ora in one go (the Content Manager's watcher then
exports each: -hires-edited.png for the edit pass, -caption.jpg for the caption pass). Only .ora
is saved: anything else open is a real deliverable that saving would flatten layers into.

The Content Manager presses it remotely through a request file in DATA_DIR; the bridge polls
for it whenever Krita runs (it used to live in the Save All docker, so a closed docker meant
no answer). A JSON body picks another action (style_test, svg_probe, ui_text_test,
add_text_active) -- the in-Krita checks of the caption pipeline.
"""

import json
import os

from krita import Krita
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from ..util import client_logger as log
from . import DATA_DIR

BRIDGE_REQUEST = os.path.join(DATA_DIR, ".save_all_request")
BRIDGE_RESULT = os.path.join(DATA_DIR, ".save_all_result")


class Bridge(QObject):
    """Save All, and the Content Manager's remote button for it."""

    status_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status = ""
        # lets the Content Manager's SAVE ALL button drive this same action
        self._bridge = QTimer(self)
        self._bridge.timeout.connect(self._poll_bridge)
        self._bridge.start(1000)

    def save_all(self):
        saved, unchanged, skipped = 0, 0, 0
        for doc in Krita.instance().documents():
            name = doc.fileName() or ""
            if not name.lower().endswith(".ora"):
                skipped += 1
                continue
            if not doc.modified():
                unchanged += 1
                continue
            doc.setBatchmode(True)
            try:
                doc.save()
                doc.waitForDone()
                saved += 1
            finally:
                doc.setBatchmode(False)
        parts = [f"saved {saved}"]
        if unchanged:
            parts.append(f"{unchanged} unchanged")
        if skipped:
            parts.append(f"{skipped} non-ora skipped")
        self.status = " · ".join(parts)
        self.status_changed.emit(self.status)
        return {"saved": saved, "unchanged": unchanged, "skipped": skipped}

    def _poll_bridge(self):
        if not os.path.exists(BRIDGE_REQUEST):
            return
        try:
            with open(BRIDGE_REQUEST, encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError:
            raw = ""
        try:
            os.remove(BRIDGE_REQUEST)
        except OSError:
            return
        # empty file = save_all (what the Content Manager sends); a JSON body
        # can pick another action, e.g. {"action": "style_test"}
        action, payload = "save_all", {}
        if raw:
            try:
                payload = json.loads(raw)
                action = payload.get("action", "save_all")
            except Exception as e:  # not JSON: a plain Save All
                log.warning(f"Save All bridge: unreadable request ({e}), saving all")
        try:
            if action == "style_test":
                res = self._style_test()
            elif action == "svg_probe":
                res = self._svg_probe(payload.get("svgs") or [])
            elif action == "ui_text_test":
                res = self._ui_text_test(payload.get("variant", "current"))
            elif action == "add_text_active":
                res = self._add_text_active(payload.get("text", "test"))
            else:
                res = self.save_all()
        except Exception as e:
            res = {"saved": 0, "unchanged": 0, "skipped": 0, "error": str(e)}
        try:
            with open(BRIDGE_RESULT, "w", encoding="utf-8") as f:
                json.dump(res, f)
        except Exception as e:
            log.warning(f"Save All bridge: could not write the result: {e}")

    def _svg_probe(self, svgs):
        """Feed SVG fragments through a scratch vector layer and return what
        Krita normalizes each one to (toSvg with krita attrs kept) - the way to
        learn this build's canonical text markup."""
        doc = Krita.instance().createDocument(480, 320, "svg probe", "RGBA", "U8", "", 120.0)
        doc.setBatchmode(True)
        probes = []
        try:
            for svg in svgs:
                entry = {"in": svg[:120]}
                try:
                    layer = doc.createVectorLayer("probe")
                    doc.rootNode().addChildNode(layer, None)
                    layer.addShapesFromSvg(svg)
                    shapes = layer.shapes()
                    entry["shapes"] = len(shapes)
                    if shapes:
                        try:
                            entry["out"] = shapes[0].toSvg(False, False)
                        except TypeError:
                            entry["out"] = shapes[0].toSvg()
                except Exception as e:
                    entry["error"] = str(e)
                probes.append(entry)
        finally:
            doc.close()
        return {"test": "svg_probe", "probes": probes}

    def _add_text_active(self, text):
        """Add a caption text layer to the ACTIVE document - lets the flow test
        caption a real caption-stage .ora exactly like the docker button."""
        from . import text as cs

        doc = Krita.instance().activeDocument()
        if not doc:
            return {"test": "add_text_active", "error": "no active document"}
        cfg = cs.load_config()
        _v, err = cs.add_text(doc, text, cfg["text_presets"][0], "#ffffff")
        return {"test": "add_text_active", "doc": doc.fileName(), "style_error": err}

    def _ui_text_test(self, variant):
        """Reproduce the user's crash path: a document WITH a real view (the
        headless self-test never triggers the kis_node_manager 'shape' assert),
        then add text a particular way. The runner reads krita.log for new
        SAFE ASSERT lines after each variant."""
        from . import text as cs

        app = Krita.instance()
        doc = app.createDocument(1280, 1024, f"ui test {variant}", "RGBA", "U8", "", 120.0)
        app.activeWindow().addView(doc)
        cfg = cs.load_config()
        preset = cfg["text_presets"][0]
        out = {"test": "ui_text_test", "variant": variant}

        if variant == "current":
            _v, err = cs.add_text(doc, "ui test", preset, "#ff2266")
            out["style_error"] = err
            return out

        # hand-rolled variants: build the shape BEFORE attaching the layer,
        # optionally skip the group / style / activation
        x, y = doc.width() // 2, int(doc.height() * 0.8)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg">'
            f'<text x="{x}" y="{y}" text-anchor="middle" '
            f'font-family="{preset["font"]}" font-size="{preset["size"]}" '
            f'fill="#ff2266"><tspan x="{x}">ui test</tspan></text></svg>'
        )
        vlayer = doc.createVectorLayer("[cap] ui test")
        if variant != "attach_first":
            vlayer.addShapesFromSvg(svg)
        parent = doc.rootNode() if variant == "no_group" else cs.caption_group(doc)
        parent.addChildNode(vlayer, None)
        if variant == "attach_first":
            vlayer.addShapesFromSvg(svg)
        if variant not in ("no_style", "no_group"):
            try:
                with open(cs.TEXT_STYLE_XML, encoding="utf-8") as f:
                    vlayer.setLayerStyleFromAsl(f.read())
            except Exception as e:
                out["style_error"] = str(e)
        if variant == "activate":
            doc.setActiveNode(vlayer)
        doc.refreshProjection()
        doc.waitForDone()
        out["shapes"] = len(vlayer.shapes())
        return out

    def _style_test(self):
        """Prove the caption text pipeline inside a real Krita: throwaway
        document, real add_text, real style apply - then report."""
        from . import text as caption_studio

        doc = Krita.instance().createDocument(
            480, 320, "caption self-test", "RGBA", "U8", "", 120.0
        )
        doc.setBatchmode(True)
        try:
            return caption_studio.self_test(doc)
        finally:
            doc.close()


_bridge: Bridge | None = None


def bridge() -> Bridge:
    """The one Save All bridge, polling from Krita's start (AIToolsExtension)."""
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
    return _bridge
