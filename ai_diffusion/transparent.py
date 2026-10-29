"""Transparency with ComfyUI (the Transparent button, or an input that has some): ComfyUI
reads a transparent pixel as the black it's stored as, and its results are opaque. So every
canvas ComfyUI gets goes in on grey (DocumentModel._for_comfy; as Forge does,
img2img_background_color), and the results come back with their background cut away by
Forge Stagehand's ToonOut (POST /stagehand/v1/matte). Krita's own result mask stays:
the cut only takes away from it.

(Editing through Forge, Stagehand does all of this on Forge's side.)
"""

from __future__ import annotations

from PyQt5.QtGui import QColor, QImage, QPainter

from .backend.network import RequestManager
from .image import Image, ImageCollection

GREY = QColor(128, 128, 128)  # Forge's img2img_background_color


def has_transparency(image: Image) -> bool:
    if not image.is_rgba:
        return False
    import numpy as np

    alpha = image._qimage.convertToFormat(QImage.Format.Format_Alpha8)
    bits = alpha.constBits()
    assert bits is not None
    bits.setsize(alpha.sizeInBytes())
    rows = np.frombuffer(bits, np.uint8).reshape(alpha.height(), alpha.bytesPerLine())
    return bool(rows[:, : alpha.width()].min() < 255)  # not the rows' padding


def flatten(image: Image) -> Image:
    out = Image.create(image.extent, GREY)
    out.draw_image(image)
    return out


def within(cut: Image, result: Image) -> Image:
    """`cut` (the cutout, RGBA) inside `result`'s own alpha: Krita's mask for the result."""
    out = Image.copy(cut)
    out.to_krita_format()
    painter = QPainter(out._qimage)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, result._qimage)
    painter.end()
    return out


async def cut(forge: str, images: ImageCollection) -> ImageCollection:
    net = RequestManager()
    out = []
    for image in images:
        r = await net.post(f"{forge.rstrip('/')}/stagehand/v1/matte", {"image": image.to_base64()})
        out.append(within(Image.from_base64(r["image"]), image))  # edge colours cleaned too
    return ImageCollection(out)
