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

Images made with Precise Reference (with or without cards) go through Forge too: its
references are the kept copies the PNG info points to, on Forge's own disk.

The format and defaults mirror Stagehand (extensions/forge-stagehand: lib_stagehand/characters.py
show/read/region_weights, scripts/precise_reference.py args_from_infotext); change them together.
Plain Python apart from the State's signal: python ai_diffusion/stagehand.py checks it.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field

from PyQt5.QtCore import QObject, pyqtSignal

GRID = 5  # Stagehand's (NovelAI's) 5x5 position grid: columns A-E, rows 1-5
SHARE = 50  # a character's default claim where her place overlaps someone else's, in percent
REFS = 4  # Precise Reference's cards (MAX_REFS)

# Stagehand's label (lib_stagehand/characters.py _LABEL): a character in several places is
# "at 0 0 0.4 1 + 0.6 0 1 1" (or "at B3 + D3"), one with a non-default share ends ", share 70%"
_CELL = r"[A-E][1-5]"
_PLACE = rf"(?:[\d.]+ ){{3}}[\d.]+|{_CELL}"
LABEL = re.compile(
    rf"^Character (\d+)(?: \((.*?)\))?(?: at ((?:{_PLACE})(?: \+ (?:{_PLACE}))*))?(?:, share (\d{{1,3}})%)?:[ \t]?(.*)$"
)
# NovelAI's interaction tags (source#hug, target#hug, mutual#hug): Stagehand reads them, a
# generator without it (ComfyUI) gets the plain tag
INTERACTION = re.compile(r"\b(?:source|target|mutual)#\s*")
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
    """Made with Stagehand: Character Prompts cards, or Precise Reference."""
    return any(LABEL.match(line) for line in infotext.split("\n")) or "PR 1 image" in params(
        infotext
    )


def params(infotext: str) -> dict[str, str]:
    """The settings line's "Key: value" pairs, quoted values unquoted (Forge's unquote)."""
    out = {}
    for key, value in _PARAM.findall(sections(infotext)[2]):
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        out[key] = value
    return out


def sections(infotext: str):
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
    positive, negative, settings = sections(infotext)
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


def _is_cell(place: str):
    return re.fullmatch(_CELL, place) is not None


def _cell_point(cell: str):
    return (ord(cell[0]) - 65 + 0.5) / GRID, (int(cell[1]) - 0.5) / GRID


def place_rect(place: str, width: int, height: int):
    """A place as a pixel rectangle (x, y, w, h): a box, or a grid cell's square."""
    if _is_cell(place):
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
    return any(_is_cell(p) for c in cards for p in c.places)


def _places(cards: list[Card], card: Card):
    """A card's places, or Stagehand's default for one without: equal columns in card order
    among the cards that are on (Grid: across the middle row, rows 2 and 4 past five)."""
    if card.places:
        return card.places
    on = [c for c in cards if c.enabled or c is card]
    k, count = on.index(card), len(on)
    if uses_grid(cards):
        row = 3 if count <= GRID else (2 if k % 2 == 0 else 4)
        col = min(int((k + 0.5) / count * GRID), GRID - 1)
        return [f"{chr(65 + col)}{row}"]
    return [f"{k / count:.4f} 0 {(k + 1) / count:.4f} 1"]


def card_area(cards: list[Card], card: Card, width: int, height: int):
    """Where a card's prompt applies, as pixel rectangles (x, y, w, h). Boxes are themselves;
    with Grid positions every spot belongs to the nearest dot of the cards that are on
    (Stagehand's region_weights, distances in fractions of the image), so her area is a row
    span per pixel row -- one straight-edged patch per dot."""
    mine = _places(cards, card)
    if not uses_grid(cards):
        return [place_rect(p, width, height) for p in mine]
    own = [_cell_point(p) for p in mine if _is_cell(p)]
    others = [
        _cell_point(p)
        for c in cards
        if c is not card and c.enabled
        for p in _places(cards, c)
        if _is_cell(p)
    ]
    rects = []
    for i, (px, py) in enumerate(own):
        rivals = others + own[:i] + own[i + 1 :]
        for row in range(height):
            y = (row + 0.5) / height
            lo, hi = 0.0, 1.0
            for qx, qy in rivals:  # nearer p than q: a*x <= c
                a, c = 2 * (qx - px), qx * qx - px * px + qy * qy - py * py - 2 * y * (qy - py)
                if a > 0:
                    hi = min(hi, c / a)
                elif a < 0:
                    lo = max(lo, c / a)
                elif c < 0:
                    lo, hi = 1.0, 0.0
            # the pixels whose centers (i + 0.5) / width lie in [lo, hi]
            x0, x1 = (
                max(math.ceil(lo * width - 0.5), 0),
                min(math.floor(hi * width - 0.5), width - 1),
            )
            if x0 <= x1:
                rects.append((x0, row, x1 - x0 + 1, 1))
    return rects


def characters_in(cards: list[Card], rect, width: int, height: int, share=0.2, samples=24):
    """The cards (on) an area is about: those whose area covers at least `share` of rect
    (x, y, w, h in pixels). A spot outside every box is nobody's (the main prompt alone)."""
    n = 64  # the areas on a coarse canvas: plenty to tell who is where
    areas = {c.number: card_area(cards, c, n, n) for c in cards if c.enabled}
    counts = dict.fromkeys(areas, 0)
    x, y, w, h = rect
    for i in range(samples):
        px = int((x + (i + 0.5) * w / samples) / width * n)
        for j in range(samples):
            py = int((y + (j + 0.5) * h / samples) / height * n)
            for number, rects in areas.items():
                if any(rx <= px < rx + rw and ry <= py < ry + rh for rx, ry, rw, rh in rects):
                    counts[number] += 1
    return [c for c in cards if c.enabled and counts[c.number] >= share * samples * samples]


def plain_prompts(base: str, negative: str, cards: list[Card]):
    """The main prompts with these cards' text added, for a generator without Stagehand
    (ComfyUI): each card on its own line after a comma, interaction tags as plain tags."""

    def merge(main: str, texts: list[str]):
        texts = [INTERACTION.sub("", t.strip()) for t in texts if t.strip()]
        return ",\n".join(p for p in [main.rstrip().rstrip(","), *texts] if p)

    return merge(base, [c.text for c in cards]), merge(negative, [c.uc for c in cards])


def job_name(chosen: list[Card], strength: float, masked: bool):
    """A Forge job in the history: what it did, how strongly, and to whom."""
    who = ", ".join(c.name or f"Character {c.number}" for c in chosen) or "main prompt"
    return f"Forge {'inpaint' if masked else 'img2img'} {round(strength * 100)}%: {who}"


def reference_args(infotext: str):
    """Precise Reference's script args for the image's references, or None without any --
    its args_from_infotext: [4 x (image, type, strength, fidelity), ADetailer (old), on,
    4 x (for, in Hires, in ADetailer)]. Forge's API drops the image when it pastes infotext,
    so the paths (its kept copies, on Forge's disk) go as args; the script loads them."""
    p = params(infotext)
    legacy = p.get("PR in ADetailer", "False")

    def number(key, default):
        try:
            return float(p[key])
        except (KeyError, ValueError):
            return default

    cards, extras = [], []
    for n in range(1, REFS + 1):
        old = f"PR {n} image" in p and f"PR {n} hires" not in p
        cards += [
            p.get(f"PR {n} image") or None,
            p.get(f"PR {n} type", "Character"),
            number(f"PR {n} strength", 1.0),
            number(f"PR {n} fidelity", 1.0),
        ]
        extras += [
            p.get(f"PR {n} for", "Whole image"),
            p.get(f"PR {n} hires", str(old)) == "True",
            p.get(f"PR {n} ADetailer", legacy if old else "False") == "True",
        ]
    if not any(cards[i * 4] for i in range(REFS)):
        return None
    return cards + [False, True] + extras


def payload(
    text: str,
    image_b64: str,
    mask_b64: str | None,
    strength: float,
    seed: int,
    width: int,
    height: int,
    task_id: str,
    count: int = 1,
    only_masked: bool = True,
    padding: int = 32,
    mask_blur: int = 4,
):
    """Forge img2img request: settings and characters from the PNG info, the rest from Krita.
    With a mask it's Forge's "Inpaint masked, original content", Only masked by default
    (Stagehand moves the characters' places into the crop's frame). `task_id` is how Forge's
    progress tells this job apart from someone else's on a shared Forge.

    Forge applies `infotext` like its paste button -- sampler, steps, model and the scripts'
    fields -- except img2img's own fields, which have no API names there: the prompts (with
    the Character lines Stagehand reads), CFG and Shift are sent explicitly."""
    positive, negative, _settings = sections(text)
    pasted = params(text)
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
        "n_iter": count,
        "save_images": False,
        "send_images": True,
        "force_task_id": task_id,
    }
    for key, label in (
        ("cfg_scale", "CFG scale"),
        ("distilled_cfg_scale", "Shift"),
        ("distilled_cfg_scale", "Distilled CFG Scale"),
        ("image_cfg_scale", "Image CFG scale"),
    ):
        try:
            request[key] = float(pasted[label])
        except (KeyError, ValueError):
            pass
    if mask_b64:
        request.update(
            mask=mask_b64,
            inpainting_fill=1,
            inpaint_full_res=only_masked,
            inpaint_full_res_padding=padding,
            inpainting_mask_invert=0,
            mask_blur=mask_blur,
        )
    if refs := reference_args(text):
        request["alwayson_scripts"] = {"Precise Reference": {"args": refs}}
    return request


class State(QObject):
    """A document's Stagehand cards and the settings line its PNG info came with. `changed`
    fires on edits (the document keeps a copy, so a .kra reopens with its cards)."""

    changed = pyqtSignal()

    def __init__(self, cards: list[Card], settings: str, enabled=False):
        super().__init__()
        self.cards = cards
        self.settings = settings
        # on: generate through Forge. Off (the default): Krita's own ComfyUI inpainting, with
        # the cards of the characters under the selection added to the prompt
        self.enabled = enabled

    @property
    def grid(self):
        return uses_grid(self.cards)

    def infotext(self, base: str, negative: str):
        return infotext(base, negative, self.cards, self.settings)

    def to_json(self):
        cards = [asdict(c) for c in self.cards]
        return json.dumps({"cards": cards, "settings": self.settings, "forge": self.enabled})

    @staticmethod
    def from_json(text: str):
        data = json.loads(text)
        cards = [Card(**c) for c in data["cards"]]
        return State(cards, data["settings"], data.get("forge", False))


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
        1,
        "Ava (Game)",
        "girl, purple hair\nblush",
        ["0.000 0.000 0.524 1.000", "0.518 0.000 1.000 0.561"],
    ), ava
    assert (two.share, two.uc, two.text) == (70, "tan, dark skin", "girl, red hair, source#hug")
    # unchanged cards write the PNG info back as Stagehand wrote it
    assert infotext(base, negative, cards, settings) == info, infotext(
        base, negative, cards, settings
    )
    # a card switched off leaves her lines and her Undesired Content out
    two.enabled = False
    off = infotext(base, negative, cards, settings)
    assert "Character 2" not in off and "red hair" not in off and "tan" not in off, off
    assert off.endswith(
        "blush\nNegative prompt: worst quality,\nSteps: 30, Sampler: ER SDE, Seed: 1, Size: 768x1024"
    ), off
    # boxes <-> pixels
    assert place_rect("0.500 0.000 1.000 0.500", 768, 1024) == (384, 0, 384, 512)
    assert rect_place((384, 0, 384, 512), 768, 1024, grid=False) == "0.500 0.000 1.000 0.500"
    assert place_rect("C3", 500, 500) == (200, 200, 100, 100)
    assert rect_place((210, 380, 60, 60), 500, 500, grid=True) == "C5"
    assert uses_grid([Card(1, places=["B3"])]) and not uses_grid(cards)
    assert card_area(cards, ava, 100, 100) == [(0, 0, 52, 100), (52, 0, 48, 56)]
    plain = [Card(1, text="a"), Card(2, text="b"), Card(3, text="c")]
    assert card_area(plain, plain[2], 300, 100) == [(200, 0, 100, 100)]
    plain[1].enabled = False  # off: the others share the width
    assert card_area(plain, plain[2], 300, 100) == [(150, 0, 150, 100)]
    # Grid: nearest dot. B3 and D3 split at the middle; C1 over C5 split at the middle row
    side = [Card(1, places=["B3"]), Card(2, places=["D3"])]
    assert set(card_area(side, side[0], 10, 4)) == {(0, r, 5, 1) for r in range(4)}
    stack = [Card(1, places=["C1"]), Card(2, places=["C5"])]
    assert card_area(stack, stack[1], 4, 10) == [(0, r, 4, 1) for r in range(5, 10)]
    diag = [Card(1, places=["A1"]), Card(2, places=["E5"])]  # the diagonal x + y = 1
    assert all(x + w <= 10 - r for x, r, w, _ in card_area(diag, diag[0], 10, 10))
    two_dots = [Card(1, places=["A3", "E3"]), Card(2, places=["C3"])]  # both ends are hers
    assert {x for x, *_ in card_area(two_dots, two_dots[0], 10, 1)} == {0, 7}
    # quoted values (Windows paths have colons) and Precise Reference's args
    pr = 'p\nSteps: 30, PR 1 image: "C:\\\\refs\\\\a b.png", PR 1 strength: 0.77, PR 1 for: Character 2'
    assert is_stagehand(pr) and params(pr)["PR 1 image"] == "C:\\refs\\a b.png"
    args = reference_args(pr)
    assert len(args) == REFS * 4 + 2 + REFS * 3 and args[:4] == [
        "C:\\refs\\a b.png",
        "Character",
        0.77,
        1.0,
    ]
    assert args[16:20] == [False, True, "Character 2", True], args[16:20]  # an old image: in Hires
    assert reference_args(info) is None
    # the request: inpaint only with a mask; the task id, batch count and inpaint settings
    assert "mask" not in payload("t", "img", None, 0.5, 7, 64, 64, "id")
    inpaint = payload(
        info, "img", "m", 0.4, 7, 64, 64, "forge-1", count=3, only_masked=False, padding=8
    )
    assert (
        inpaint["inpainting_fill"] == 1
        and inpaint["infotext"] == info
        and not inpaint["inpaint_full_res"]
    )
    assert (inpaint["force_task_id"], inpaint["n_iter"], inpaint["inpaint_full_res_padding"]) == (
        "forge-1",
        3,
        8,
    )
    assert (
        "alwayson_scripts" not in inpaint
        and payload(pr, "i", None, 1, 7, 8, 8, "x")["alwayson_scripts"]
    )
    # the fields Forge's img2img API doesn't take from infotext
    assert inpaint["prompt"].endswith("source#hug") and inpaint["negative_prompt"].endswith(
        "tan, dark skin"
    )
    shifted = payload(
        "p\nSteps: 30, Sampler: ER SDE, CFG scale: 4.5, Shift: 3, Seed: 1",
        "img",
        None,
        1,
        7,
        64,
        64,
        "x",
    )
    assert (shifted["cfg_scale"], shifted["distilled_cfg_scale"]) == (4.5, 3.0), shifted
    # who an inpaint is about: Lux's face is hers alone, the gap between boxes is nobody's
    lux = [Card(1, "Leona", places=["0.6 0 1 1"]), Card(2, "Lux", places=["0 0 0.5 1"])]
    assert [c.name for c in characters_in(lux, (100, 50, 80, 80), 768, 1024)] == ["Lux"]
    assert characters_in(lux, (400, 50, 50, 50), 768, 1024) == []
    both = characters_in(lux, (200, 0, 400, 300), 768, 1024)
    assert [c.name for c in both] == ["Leona", "Lux"], both
    lux[0].enabled = False  # off: never chosen
    assert [c.name for c in characters_in(lux, (0, 0, 768, 1024), 768, 1024)] == ["Lux"]
    pos, neg = plain_prompts(
        "main, a,", "bad", [Card(1, text="girl, source#hug", uc="tan"), Card(2, text=" ")]
    )
    assert (pos, neg) == ("main, a,\ngirl, hug", "bad,\ntan"), (pos, neg)
    assert plain_prompts("main", "", []) == ("main", "")
    assert job_name(lux[1:], 0.4, True) == "Forge inpaint 40%: Lux"
    assert job_name([], 1, False) == "Forge img2img 100%: main prompt"
    # a .kra keeps the cards
    two.enabled = False
    back = State.from_json(State(cards, settings, enabled=False).to_json())
    assert back.cards == cards and back.settings == settings and back.enabled is False
    # ComfyUI by default; Forge when switched on, kept by a .kra -- but not from documents
    # saved while Forge was the default (their "enabled")
    assert State(cards, settings).enabled is False
    assert State.from_json(State(cards, settings, enabled=True).to_json()).enabled is True
    old = json.dumps({"cards": [], "settings": "", "enabled": True})
    assert State.from_json(old).enabled is False
    print("ok")
