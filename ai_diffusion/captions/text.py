"""Caption Studio's text layers: a caption in the foreground color, Kristen ITC 72 by
default, with the captured layer style (8px black stroke + soft bevel, text_style.aslxml)
applied automatically. Every layer it creates goes in a group named "CAPTIONS".

The user's files (presets.json, text_style.aslxml, assets/) live in DATA_DIR.
"""

import json
import os
import xml.sax.saxutils

from krita import InfoObject, Krita

from ..util import client_logger as log
from . import DATA_DIR

PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")
CAPTION_GROUP = "CAPTIONS"

# Krita's setLayerStyleFromAsl() takes ASL *XML*, not the binary 8BSL an .asl
# file contains (actual_default_text.asl starts with 8BSL). Only Krita can make
# that XML - via layerStyleToAsl() on a layer that already has the style - so we
# capture it once with the "remember style" button and cache it here.
TEXT_STYLE_XML = os.path.join(DATA_DIR, "text_style.aslxml")

KRITA_NS = "http://krita.org/namespaces/svg/krita"

DEFAULT_CONFIG = {
    "text_presets": [
        {
            "name": "Default (Kristen ITC 72)",
            "font": "Kristen ITC",
            "size": 72,
            "color": "wheel",
            "letter_spacing": 0,
            "bold": False,
        },
    ],
}


def load_config():
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("text_presets"):
                return cfg
        except Exception as e:  # unreadable: the default preset is written instead
            log.warning(f"Caption Studio: {PRESETS_FILE}: {e}")
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    return dict(DEFAULT_CONFIG)


def caption_group(doc):
    root = doc.rootNode()
    for n in root.childNodes():
        if n.name() == CAPTION_GROUP:
            return n
    grp = doc.createGroupLayer(CAPTION_GROUP)
    root.addChildNode(grp, None)
    return grp


def foreground_hex():
    """Current color-wheel (foreground) color as #rrggbb; white fallback."""
    try:
        view = Krita.instance().activeWindow().activeView()
        qcolor = view.foregroundColor().colorForCanvas(view.canvas())
        return qcolor.name()
    except Exception:
        return "#ffffff"


def activate_later(doc, node, delay_ms=200):
    """Select a freshly added node AFTER the event loop has caught up.

    Calling doc.setActiveNode() in the same breath as addChildNode() trips
    SAFE ASSERT "shape" (kis_node_manager.cpp:164) on a document with a live
    canvas - the canvas hasn't registered the new node's shape yet - and the
    'recovered' broken state then crashes/freezes the text tool. Reproduced
    and bisected 2026-07-17 via the ui_text_test bridge action."""
    from PyQt5.QtCore import QTimer

    def _go():
        try:
            doc.setActiveNode(node)
        except Exception as e:  # doc/node gone in the meantime - nothing to select
            log.debug(f"Caption Studio: nothing to select ({e})")

    QTimer.singleShot(delay_ms, _go)


def add_text(doc, text, preset, color):
    """Create the caption text layer on doc. Returns (vector_layer, style_error);
    style_error is None unless the captured layer style failed to apply.

    Module-level so the docker button and the bridge self-test run the exact
    same code path."""
    grp = caption_group(doc)
    lines = text.splitlines() or [text]
    vlayer = doc.createVectorLayer("[cap] " + lines[0][:24])

    x = doc.width() // 2
    y = int(doc.height() * 0.8)
    # This clones, attribute for attribute, the markup the RICH TEXT editor
    # itself saves (read from the user's own comic .kra text shapes):
    #  - krita:useRichText="true" is what makes double-click open the rich
    #    tab instead of the SVG-source editor
    #  - position ONLY via transform; x/y attributes on <text> are what the
    #    rich converter chokes on (rich tab came up empty and wiped the text)
    #  - font family/size live in the style CSS, not presentation attributes
    #  - per-line tspans: x=0 (text-anchor centers on the transform origin),
    #    dy in px; the editor writes ~1.365 * font-size for Kristen ITC
    line_h = round(preset["size"] * 1.365, 2)
    tspans = "".join(
        '<tspan x="0"' + ("" if i == 0 else f' dy="{line_h}"') + ">"
        f"{xml.sax.saxutils.escape(line) or ' '}</tspan>"
        for i, line in enumerate(lines)
    )
    style_css = (
        "text-align: start;text-align-last: auto;"
        f"font-family: {preset['font']};font-size: {preset['size']};"
        + ("font-weight: 700;" if preset.get("bold") else "")
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:krita="{KRITA_NS}">'
        f'<text krita:useRichText="true" krita:textVersion="3" text-rendering="auto" '
        f'transform="matrix(1 0 0 1 {x} {y})" fill="{color}" '
        f'stroke-opacity="0" stroke="#000000" stroke-width="0" '
        f'stroke-linecap="square" stroke-linejoin="bevel" '
        f'kerning="none" letter-spacing="{preset.get("letter_spacing", 0)}" '
        f'text-anchor="middle" word-spacing="0" '
        f'style="{style_css}">{tspans}</text></svg>'
    )
    # shapes go in BEFORE the layer is attached; the canvas then registers the
    # finished layer once, instead of racing a shape-less vector layer
    vlayer.addShapesFromSvg(svg)
    grp.addChildNode(vlayer, None)

    style_error = None
    if os.path.exists(TEXT_STYLE_XML):
        try:
            with open(TEXT_STYLE_XML, encoding="utf-8") as f:
                ok = vlayer.setLayerStyleFromAsl(f.read())
            if ok is False:  # libkis returns False on a rejected style, no raise
                style_error = "Krita rejected the style XML (re-capture it?)"
        except Exception as e:
            style_error = str(e)

    activate_later(doc, vlayer)
    doc.refreshProjection()
    return vlayer, style_error


def self_test(doc):
    """End-to-end check of the text pipeline, driven through the Save All
    bridge ('style_test' action): adds a caption layer to doc and reports
    whether the shapes and the automatic layer style actually landed."""
    cfg = load_config()
    preset = cfg["text_presets"][0]
    vlayer, style_error = add_text(doc, "style test", preset, "#ff2266")
    try:
        shapes = len(vlayer.shapes())
    except Exception:
        shapes = -1
    asl = ""
    try:
        asl = vlayer.layerStyleToAsl() or ""
    except Exception as e:
        style_error = style_error or f"layerStyleToAsl: {e}"
    # diagnostics: what did the apply call actually return, and what does
    # Krita serialize back off the layer?
    apply_ret = "n/a"
    if os.path.exists(TEXT_STYLE_XML):
        try:
            with open(TEXT_STYLE_XML, encoding="utf-8") as f:
                apply_ret = repr(vlayer.setLayerStyleFromAsl(f.read()))
            asl = vlayer.layerStyleToAsl() or ""
        except Exception as e:
            apply_ret = f"raised: {e}"
    return {
        "test": "style_test",
        "font": preset["font"],
        "size": preset["size"],
        "shapes": shapes,
        "style_xml_present": os.path.exists(TEXT_STYLE_XML),
        # round trip: the style Krita reads BACK off the layer must contain
        # both effects (stroke + bevel), proving the apply really took
        "style_applied": ("FrFX" in asl and "ebbl" in asl),
        "style_error": style_error,
        "apply_ret": apply_ret,
        "asl_len": len(asl),
        "asl_head": asl[:600],
    }


def export_caption_jpg(doc, folder, stem):
    """Write <stem>-caption.jpg. Never touches the source file - Ctrl+S on a Finished jpg
    would flatten the captions into it instead. No caption-free twin: the caption pass works
    in a .ora and leaves the upscaled image alone, so the Finished jpg IS that copy."""
    doc.setBatchmode(True)
    doc.waitForDone()
    # .jpg, not .png: the Finished images this sits beside are jpg, and so is every
    # -caption in the library (317 jpg vs 2 png) and on MEGA
    out = os.path.join(folder, stem + "-caption.jpg")
    doc.exportImage(out, InfoObject())
    doc.setBatchmode(False)
    return out


def caption_destination(doc):
    """(folder, stem) from the document's own path, or None if unsaved."""
    src = doc.fileName()
    if not src:
        return None
    stem = os.path.splitext(os.path.basename(src))[0]
    for suf in ("-caption", "-nocap"):
        stem = stem.removesuffix(suf)
    return os.path.dirname(src), stem
