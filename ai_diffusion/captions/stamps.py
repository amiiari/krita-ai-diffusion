"""Caption Studio's stamps: png/webp images and .kra assets from DATA_DIR/assets, inserted
as layers in the CAPTIONS group. thoughts.kra (the thought bubbles) has its own button. A .kra
that is a tool (a layer + a filter mask) is rebuilt as that structure; any other .kra is
inserted from its merged image, the copy that carries the real transparency.
"""

import os
import re
import zipfile

from krita import Krita
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QMessageBox

from ..util import client_logger as log
from . import DATA_DIR
from .text import activate_later, caption_group

ASSETS_DIR = os.path.join(DATA_DIR, "assets")

IMG_EXTS = (".png", ".webp")
# the thought-bubble overlay; shadow.kra is the old name for the same asset
THOUGHTS_NAMES = ("thoughts.kra", "shadow.kra")


def _insert_qimage(doc, img, name):
    """Insert a QImage as a centered paint layer inside the CAPTIONS group."""
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    layer = doc.createNode(name, "paintlayer")
    ptr = img.constBits()
    ptr.setsize(img.byteCount())
    x = (doc.width() - w) // 2
    y = (doc.height() - h) // 2
    layer.setPixelData(bytes(ptr), x, y, w, h)
    caption_group(doc).addChildNode(layer, None)
    activate_later(doc, layer)  # immediate setActiveNode races the canvas - see caption_studio
    doc.refreshProjection()


def _kra_merged(path, quiet=False):
    """The flattened image inside a .kra - it keeps the alpha, unlike the layers."""
    try:
        with zipfile.ZipFile(path) as z:
            data = z.read("mergedimage.png")
    except Exception as e:
        if not quiet:
            QMessageBox.warning(None, "Stamps", f"Could not read {os.path.basename(path)}:\n{e}")
        return None
    img = QImage.fromData(data)
    if img.isNull():
        if not quiet:
            QMessageBox.warning(
                None, "Stamps", f"{os.path.basename(path)} has no usable merged image."
            )
        return None
    return img


def _kra_filter_template(path):
    """A .kra that is a *tool*, not a picture: an (often empty) paint layer whose
    real content is a filter mask.

    thoughts.kra is exactly this - a layer literally named "add black for shadow"
    carrying a Gradient Map mask, so you paint black and the mask turns it into a
    dithered shadow. Its mergedimage.png is 100% transparent, so inserting the
    merged image inserts *nothing*. The structure IS the asset.

    Returns (layer_name, filter_name, mask_name, {param: value}) or None.
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            xml = z.read("maindoc.xml").decode("utf-8", "replace")
    except Exception:
        return None

    mask_tag = re.search(r'<mask\b[^>]*nodetype="filtermask"[^>]*/?>', xml)
    if not mask_tag:
        return None

    def attr(tag, name):
        m = re.search(rf'\b{name}="([^"]*)"', tag)
        return m.group(1) if m else None

    tag = mask_tag.group(0)
    filter_name = attr(tag, "filtername")
    if not filter_name:
        return None
    mask_name = attr(tag, "name") or "Filter Mask"
    cfg_file = attr(tag, "filename")

    layer_tag = re.search(r"<layer\b[^>]*>", xml)
    layer_name = attr(layer_tag.group(0), "name") if layer_tag else "layer"

    params = {}
    cfg_path = next(
        (n for n in names if cfg_file and n.endswith("/" + cfg_file + ".filterconfig")), None
    )
    if cfg_path:
        with zipfile.ZipFile(path) as z:
            cfg_xml = z.read(cfg_path).decode("utf-8", "replace")
        for m in re.finditer(
            r'<param\b[^>]*\bname="([^"]+)"[^>]*>\s*<!\[CDATA\[(.*?)\]\]>\s*</param>',
            cfg_xml,
            re.DOTALL,
        ):
            params[m.group(1)] = m.group(2)
    return layer_name, filter_name, mask_name, params


def _thoughts_asset():
    for n in THOUGHTS_NAMES:
        p = os.path.join(ASSETS_DIR, n)
        if os.path.exists(p):
            return p
    return None


def insert_kra(doc, path):
    """Rebuild a .kra asset in the document.

    If it's a tool (a layer + a filter mask, like the shadow/gradient-map
    template) rebuild that structure so it arrives ready to paint into.
    Otherwise it's a picture, so insert its flattened image.
    """
    tpl = _kra_filter_template(path)
    if tpl is None:
        img = _kra_merged(path)
        if img is not None:
            _insert_qimage(doc, img, "[cap] " + os.path.splitext(os.path.basename(path))[0])
        return

    layer_name, filter_name, mask_name, params = tpl
    flt = Krita.instance().filter(filter_name)
    if flt is None:
        QMessageBox.warning(
            None,
            "Stamps",
            f"Krita has no '{filter_name}' filter — cannot rebuild {os.path.basename(path)}.",
        )
        return
    cfg = flt.configuration()
    for k, v in params.items():
        try:
            cfg.setProperty(k, v)
        except Exception as e:  # a param this Krita build doesn't know: keep its default
            log.debug(f"Stamps: {filter_name} has no '{k}' ({e})")
    flt.setConfiguration(cfg)

    layer = doc.createNode(layer_name, "paintlayer")
    caption_group(doc).addChildNode(layer, None)

    # Fill the layer white FIRST. A gradient-map template maps luminance to
    # the gradient, and this one is "Foreground to Transparent": white -> fully
    # transparent, black -> shadow. So a white base is invisible until you
    # paint black on it - that's the whole tool. An empty (transparent) layer
    # gives the map nothing to act on, which is why this had to be filled by
    # hand. The source .kra stores that white as painted pixels, not as its
    # default pixel, so it can't just be copied off the layer header.
    w, h = doc.width(), doc.height()
    try:
        layer.setPixelData(b"\xff\xff\xff\xff" * (w * h), 0, 0, w, h)
    except Exception as e:
        QMessageBox.warning(
            None,
            "Stamps",
            f"Could not fill the white base:\n{e}\n\nFill the layer white by hand before painting.",
        )

    try:
        mask = doc.createFilterMask(mask_name, flt, layer)
        layer.addChildNode(mask, None)
    except Exception as e:
        QMessageBox.warning(None, "Stamps", f"Layer added, but the {filter_name} mask failed:\n{e}")
    activate_later(doc, layer)  # immediate setActiveNode races the canvas - see caption_studio
    doc.refreshProjection()


def insert_stamp(doc, path):
    """Insert an asset: a .kra rebuilt (insert_kra), a png/webp as a centered layer."""
    if path.lower().endswith(".kra"):
        insert_kra(doc, path)
        return
    img = QImage(path)
    if img.isNull():
        QMessageBox.warning(None, "Stamps", f"Could not read {os.path.basename(path)}")
        return
    _insert_qimage(doc, img, "[cap] " + os.path.splitext(os.path.basename(path))[0])
