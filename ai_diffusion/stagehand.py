"""Forge Stagehand: images made with its Character Prompts, edited in Krita through Forge.

Stagehand writes each character into the PNG info as a labelled line under the main prompt
(and her Undesired Content under the main negative prompt):

    masterpiece, 2girls, cafe

    Character 1 (Ava): girl, black hair, ...
    Character 2 (Mei) at 0.500 0.000 1.000 0.500, share 70%: girl, orange hair, ...
    Negative prompt: worst quality

    Character 2: tan
    Steps: 30, Sampler: ...

Opening such an image makes a card per character (Krita's prompt box keeps the main prompt).
Generating sends the image's PNG info with the cards that are on to Forge's img2img API as
`infotext`: Forge parses it exactly as dropping the image into its UI would, and Stagehand
reads the Character lines back as cards. The pixels, the selection as inpaint mask, the
strength and the seed come from Krita.

Plain Python apart from the State's signal: python -m ai_diffusion.stagehand checks it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FORGE_URL = "http://127.0.0.1:7860"
GRID = 5  # Stagehand's (NovelAI's) 5x5 position grid: columns A-E, rows 1-5
SHARE = 50  # a character's default claim where her place overlaps someone else's, in percent

# Stagehand's label (lib_stagehand/characters.py _LABEL): a character in several places is
# "at 0 0 0.4 1 + 0.6 0 1 1" (or "at B3 + D3"), one with a non-default share ends ", share 70%"
_PLACE = r"(?:[\d.]+ ){3}[\d.]+|[A-E][1-5]"
LABEL = re.compile(
    rf"^Character (\d+)(?: \((.*?)\))?(?: at ((?:{_PLACE})(?: \+ (?:{_PLACE}))*))?(?:, share (\d{{1,3}})%)?:[ \t]?(.*)$"
)
# one "Key: value" of the settings line (Forge's infotext_utils.re_param)
_PARAM = re.compile(r'\s*(\w[\w \-/]+):\s*("(?:\\.|[^\\"])+"|[^,]*)(?:,|$)')


@dataclass
class Card:
    number: int
    name: str = ""
    text: str = ""
    uc: str = ""
    places: list[str] = field(default_factory=list)  # "x0 y0 x1 y1" (fractions) or a cell "C3"
    share: int = SHARE
    enabled: bool = True

    @property
    def title(self):
        return f"Character {self.number}" + (f" ({self.name})" if self.name else "")


def is_stagehand(infotext: str):
    return any(LABEL.match(line) for line in infotext.split("\n"))


def _sections(infotext: str):
    """(prompt section, negative section, settings line) of a PNG info text."""
    body, steps, settings = infotext.partition("\nSteps:")
    positive, marker, negative = body.partition("\nNegative prompt:")
    return positive.strip(), negative.strip() if marker else "", (steps + settings).strip()


def _read(section: str):
    """(main text, {number: (match, [lines])}): a line after a character's that isn't another
    character continues it."""
    lines = section.split("\n")
    chars: dict[int, tuple[re.Match, list[str]]] = {}
    current, first = None, None
    for i, line in enumerate(lines):
        if m := LABEL.match(line):
            first = i if first is None else first
            current = chars[int(m.group(1))] = (m, [m.group(5)])
        elif current is not None:
            current[1].append(line)
    if first is None:
        return section, {}
    return "\n".join(lines[:first]).rstrip(), chars


def parse(infotext: str):
    """(main prompt, main negative, cards, settings line) of a Stagehand image's PNG info."""
    positive, negative, settings = _sections(infotext)
    base, chars = _read(positive)
    negative_base, ucs = _read(negative)
    cards = []
    for n, (m, lines) in sorted(chars.items()):
        uc = "\n".join(ucs[n][1]).strip() if n in ucs else ""
        places = m.group(3).split(" + ") if m.group(3) else []
        share = min(int(m.group(4)), 100) if m.group(4) else SHARE
        cards.append(Card(n, m.group(2) or "", "\n".join(lines).strip(), uc, places, share))
    return base, negative_base, cards, settings


def _lines(base: str, lines: list[str]):
    return "\n\n".join(part for part in (base.rstrip(), "\n".join(lines)) if part)


def infotext(base: str, negative: str, cards: list[Card], settings: str):
    """PNG info with the main prompts, the cards that are on, and the settings line --
    Stagehand's own layout (characters.show), so Forge's paste turns it into cards."""
    on = [c for c in cards if c.enabled and c.text.strip()]
    labels = []
    for c in on:
        at = f" at {' + '.join(c.places)}" if c.places else ""
        share = f", share {c.share}%" if c.share != SHARE else ""
        labels.append(f"{c.title}{at}{share}: {c.text.strip()}")
    ucs = [f"Character {c.number}: {c.uc.strip()}" for c in on if c.uc.strip()]
    text = _lines(base, labels)
    if negative := _lines(negative, ucs):
        text += "\nNegative prompt: " + negative
    return text + ("\n" + settings if settings else "")


def place_rect(place: str, width: int, height: int):
    """A place as a pixel rectangle (x, y, w, h): a box, or a grid cell's square."""
    if re.fullmatch(r"[A-E][1-5]", place):
        col, row = ord(place[0]) - 65, int(place[1]) - 1
        x0, y0, x1, y1 = col / GRID, row / GRID, (col + 1) / GRID, (row + 1) / GRID
    else:
        x0, y0, x1, y1 = (float(v) for v in place.split())
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
    x, y = round(x0 * width), round(y0 * height)
    return x, y, max(round(x1 * width) - x, 1), max(round(y1 * height) - y, 1)


def rect_place(rect: tuple[int, int, int, int], width: int, height: int, grid: bool):
    """A pixel rectangle as a place: a box in fractions, or (grid) the cell of its center."""
    x, y, w, h = rect
    if grid:
        col = min(int((x + w / 2) / width * GRID), GRID - 1)
        row = min(int((y + h / 2) / height * GRID), GRID - 1)
        return f"{chr(65 + col)}{row + 1}"
    return " ".join(f"{v:.3f}" for v in (x / width, y / height, (x + w) / width, (y + h) / height))


def uses_grid(cards: list[Card]):
    return any(re.fullmatch(r"[A-E][1-5]", p) for c in cards for p in c.places)


def card_rects(cards: list[Card], card: Card, width: int, height: int):
    """Where a card stands, in pixels: her places, or Stagehand's default for a card without
    one -- equal columns in card order among the cards that are on (Grid: across the middle
    row, rows 2 and 4 past five)."""
    if card.places:
        return [place_rect(p, width, height) for p in card.places]
    on = [c for c in cards if c.enabled or c is card]
    k, count = on.index(card), len(on)
    if uses_grid(cards):
        row = 3 if count <= GRID else (2 if k % 2 == 0 else 4)
        col = min(int((k + 0.5) / count * GRID), GRID - 1)
        return [place_rect(f"{chr(65 + col)}{row}", width, height)]
    x0, x1 = round(k / count * width), round((k + 1) / count * width)
    return [(x0, 0, x1 - x0, height)]


def payload(
    text: str,
    image_b64: str,
    mask_b64: str | None,
    strength: float,
    seed: int,
    width: int,
    height: int,
    padding: int = 32,
):
    """Forge img2img request: settings and characters from the PNG info, the rest from Krita.
    With a mask it's Forge's "Inpaint masked, Only masked, original content" (Stagehand moves
    the characters' places into the crop's frame).

    Forge applies `infotext` like its paste button -- sampler, steps, model and the scripts'
    fields -- except img2img's own fields, which have no API names there: the prompts (with
    the Character lines Stagehand reads), CFG and Shift are sent explicitly."""
    positive, negative, settings = _sections(text)
    params = dict(_PARAM.findall(settings))
    request = {
        "prompt": positive,
        "negative_prompt": negative,
        "infotext": text,
        "init_images": [image_b64],
        "denoising_strength": strength,
        "seed": seed,
        "width": width,
        "height": height,
        "batch_size": 1,
        "n_iter": 1,
        "save_images": False,
        "send_images": True,
    }
    for key, label in (("cfg_scale", "CFG scale"), ("distilled_cfg_scale", "Shift"),
                       ("distilled_cfg_scale", "Distilled CFG Scale"), ("image_cfg_scale", "Image CFG scale")):
        try:
            request[key] = float(params[label])
        except (KeyError, ValueError):
            pass
    if mask_b64:
        request.update(
            mask=mask_b64,
            inpainting_fill=1,
            inpaint_full_res=True,
            inpaint_full_res_padding=padding,
            inpainting_mask_invert=0,
            mask_blur=4,
        )
    return request


try:
    from PyQt5.QtCore import QObject, pyqtSignal

    class State(QObject):
        """A document's Stagehand cards and the settings line its PNG info came with."""

        changed = pyqtSignal()

        def __init__(self, cards: list[Card], settings: str):
            super().__init__()
            self.cards = cards
            self.settings = settings
            self.enabled = True  # off: generate with ComfyUI as usual

        @property
        def grid(self):
            return uses_grid(self.cards)

        def infotext(self, base: str, negative: str):
            return infotext(base, negative, self.cards, self.settings)

except ImportError:  # outside Krita without PyQt: the plain functions still check
    pass


if __name__ == "__main__":
    info = (
        "<lora:style:0.9>, @artist,\nmasterpiece, 2girls, cafe\n\n"
        "Character 1 (Ava (Game)) at 0.000 0.000 0.524 1.000 + 0.518 0.000 1.000 0.561: girl, purple hair\n"
        "blush\n"
        "Character 2 at 0.518 0.561 1.000 1.000, share 70%: girl, red hair, source#hug\n"
        "Negative prompt: worst quality,\n\nCharacter 2: tan, dark skin\n"
        "Steps: 30, Sampler: ER SDE, Seed: 1, Size: 768x1024"
    )
    assert is_stagehand(info) and not is_stagehand("1girl\nSteps: 20")
    base, negative, cards, settings = parse(info)
    assert base == "<lora:style:0.9>, @artist,\nmasterpiece, 2girls, cafe", base
    assert negative == "worst quality,", negative
    assert settings == "Steps: 30, Sampler: ER SDE, Seed: 1, Size: 768x1024"
    ava, two = cards
    assert (ava.number, ava.name, ava.text, ava.places) == (
        1, "Ava (Game)", "girl, purple hair\nblush", ["0.000 0.000 0.524 1.000", "0.518 0.000 1.000 0.561"]
    ), ava
    assert (two.share, two.uc, two.text) == (70, "tan, dark skin", "girl, red hair, source#hug")
    # unchanged cards write the PNG info back as Stagehand wrote it
    assert infotext(base, negative, cards, settings) == info, infotext(base, negative, cards, settings)
    # a card switched off leaves her lines and her Undesired Content out
    two.enabled = False
    off = infotext(base, negative, cards, settings)
    assert "Character 2" not in off and "red hair" not in off and "tan" not in off, off
    assert off.endswith("blush\nNegative prompt: worst quality,\nSteps: 30, Sampler: ER SDE, Seed: 1, Size: 768x1024"), off
    # boxes <-> pixels
    assert place_rect("0.500 0.000 1.000 0.500", 768, 1024) == (384, 0, 384, 512)
    assert rect_place((384, 0, 384, 512), 768, 1024, grid=False) == "0.500 0.000 1.000 0.500"
    assert place_rect("C3", 500, 500) == (200, 200, 100, 100)
    assert rect_place((210, 380, 60, 60), 500, 500, grid=True) == "C5"
    assert uses_grid([Card(1, places=["B3"])]) and not uses_grid(cards)
    assert card_rects(cards, ava, 100, 100) == [(0, 0, 52, 100), (52, 0, 48, 56)]
    plain = [Card(1, text="a"), Card(2, text="b"), Card(3, text="c")]
    assert card_rects(plain, plain[2], 300, 100) == [(200, 0, 100, 100)]
    plain[1].enabled = False  # off: the others share the width
    assert card_rects(plain, plain[2], 300, 100) == [(150, 0, 150, 100)]
    # the request: inpaint only with a mask
    assert "mask" not in payload("t", "img", None, 0.5, 7, 64, 64)
    inpaint = payload(info, "img", "m", 0.4, 7, 64, 64)
    assert inpaint["inpaint_full_res"] and inpaint["inpainting_fill"] == 1 and inpaint["infotext"] == info
    # the fields Forge's img2img API doesn't take from infotext
    assert inpaint["prompt"].endswith("source#hug") and inpaint["negative_prompt"].endswith("tan, dark skin")
    shifted = payload("p\nSteps: 30, Sampler: ER SDE, CFG scale: 4.5, Shift: 3, Seed: 1", "img", None, 1, 7, 64, 64)
    assert (shifted["cfg_scale"], shifted["distilled_cfg_scale"]) == (4.5, 3.0), shifted
    print("ok")
