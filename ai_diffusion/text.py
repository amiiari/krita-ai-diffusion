from __future__ import annotations

import random
import re
from typing import NamedTuple

from .backend.api import ConditioningInput, LoraInput, RegionInput
from .files import FileCollection, FileSource
from .localization import translate as _
from .model.jobs import JobParams
from .util import PluginError
from .util import client_logger as log

# Functions to convert between position in Python str objects (unicode) and
# index in QString char16 arrays (used in eg. QTextCursor).


def char16_len(text: str):
    return len(text.encode("utf-16")) // 2 - 1  # subtract BOM


def char16_index_to_str_index(text: str, c16_index: int):
    bytes_utf16 = text.encode("utf-16")
    byte_pos = 2 + c16_index * 2  # utf-16 text starts with 2-byte BOM
    text_until_pos = bytes_utf16[:byte_pos].decode("utf-16")
    return len(text_until_pos)


def str_index_to_char16_index(text: str, index: int):
    return char16_len(text[:index])


# Prompt processing utilities


class LoraId(NamedTuple):
    file: str
    name: str

    @staticmethod
    def normalize(original: str | None):
        if original is None:
            return LoraId("", "<Invalid LoRA>")
        return LoraId(original, original.replace("\\", "/").removesuffix(".safetensors"))


# "#" starts a comment, except escaped (\#), a hex color (#ff5733), or in one of Stagehand's
# interaction tags (source#hug, target#hug, mutual#hug -- NovelAI's syntax), which are prompt
pattern_comment = re.compile(r"(?<!\\)(?<!\bsource)(?<!\btarget)(?<!\bmutual)#(?![0-9a-fA-F]{6}).*")
pattern_lora = re.compile(r"<lora:([^:<>]+)(?::(-?[^:<>]*))?>", re.IGNORECASE)
pattern_layer = re.compile(r"<layer:([^>]+)>", re.IGNORECASE)
pattern_weight_expr = re.compile(r"\([^:()]+:(-?[\d.]+)\)")
pattern_wildcard = re.compile(r"(\{[^{}]+\|[^{}]+\})")


def strip_prompt_comments(prompt: str):
    """Strip comments (text after #) from the prompt, unless the # is escaped with a backslash,
    or it's a hex color code."""
    lines = prompt.splitlines()
    stripped_lines = [pattern_comment.sub("", line).replace(r"\#", "#").rstrip() for line in lines]
    return "\n".join(stripped_lines).strip()


def merge_prompt(prompt: str, style_prompt: str, language: str = ""):
    phrase = " ".join(style_prompt.strip().strip(",").split())
    if prompt and phrase and "{prompt}" not in style_prompt:
        # the style's phrase is already in the prompt (eg. a prompt pasted from an image
        # made with this style): don't append it a second time
        if re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", " ".join(prompt.split())):
            style_prompt = ""
    if language and prompt:
        prompt = f"lang:{language} {prompt} lang:en "
    if style_prompt == "":
        return prompt
    elif "{prompt}" in style_prompt:
        return style_prompt.replace("{prompt}", prompt)
    elif prompt == "":
        return style_prompt
    return f"{prompt}, {style_prompt}"


def extract_loras(prompt: str, lora_files: FileCollection):
    loras: list[LoraInput] = []

    def replace(match: re.Match[str]):
        input = match[1].lower()
        requested = lora_name(input)
        available = [f for f in lora_files if f.source is not FileSource.unavailable]
        # with its folder ("characters/ava"), or its name alone when that is unambiguous
        candidates = [f for f in available if requested in (lora_name(f.id), f.name.lower())]
        if not candidates:
            basename = requested.rsplit("/", 1)[-1]
            candidates = [f for f in available if lora_name(f.id).rsplit("/", 1)[-1] == basename]
        if len(candidates) > 1:
            error = _("Ambiguous LoRA name, include its folder") + f": {input}"
            log.warning(error)
            raise PluginError(error)
        if not candidates:
            error = _("LoRA not found") + f": {input}"
            log.warning(error)
            raise PluginError(error)
        lora_file = candidates[0]

        lora_strength: float = lora_file.meta("lora_strength", 1.0)
        if match[2]:
            try:
                lora_strength = float(match[2])
            except ValueError:
                error = _("Invalid LoRA strength for") + f" {input}: {lora_strength}"
                log.warning(error)
                raise ValueError(error)

        loras.append(LoraInput(lora_file.id, lora_strength))
        return ""

    prompt = pattern_lora.sub(replace, prompt)
    return prompt.strip(), loras


def _extract_layers(prompt: str, start_index=1):
    layer_index = start_index
    layer_names: dict[str, int] = {}

    for match in pattern_layer.finditer(prompt):
        name = match[1]
        idx = layer_names.get(name)
        if idx is None:
            idx = layer_index
            layer_index += 1
        layer_names[name] = idx
    return layer_names


def extract_layers(cond: ConditioningInput, region: RegionInput | None = None):
    start_index = 2 if cond.edit_reference else 1
    start_index += sum(1 if c.mode.is_ip_adapter else 0 for c in cond.control)
    if region is None:
        return _extract_layers(cond.positive, start_index)
    else:
        start_index += sum(1 if c.mode.is_ip_adapter else 0 for c in region.control)
        return _extract_layers(region.positive, start_index)


def replace_layers(prompt: str, layer_mapping: dict[str, int], replacement="Picture {}"):
    def replace(match: re.Match[str]):
        return replacement.format(layer_mapping[match[1]])

    return pattern_layer.sub(replace, prompt).strip()


def eval_wildcards(text: str, seed: int):
    rng = random.Random(seed)

    def replace(match: re.Match[str]):
        wildcard_name = match[1]
        options = wildcard_name.split("|")
        return rng.choice(options).strip("{} ")

    for __ in range(10):
        prev = text
        text = pattern_wildcard.sub(replace, text)
        if text == prev:
            break

    return text


def select_current_parenthesis_block(
    text: str, cursor_pos: int, open_brackets: list[str], close_brackets: list[str]
) -> tuple[int, int] | None:
    """Select the current parenthesis block that the cursor points to."""
    # Ensure cursor position is within valid range
    cursor_pos = max(0, min(cursor_pos, len(text)))

    # Find the nearest '(' before the cursor
    start = -1
    for open_bracket in open_brackets:
        start = max(start, text.rfind(open_bracket, 0, cursor_pos))

    # If '(' is found, find the corresponding ')' after the cursor
    end = -1
    if start != -1:
        open_parens = 1
        for i in range(start + 1, len(text)):
            if text[i] in open_brackets:
                open_parens += 1
            elif text[i] in close_brackets:
                open_parens -= 1
                if open_parens == 0:
                    end = i
                    break

    # Return the indices only if both '(' and ')' are found
    if start != -1 and end >= cursor_pos:
        return start, end + 1
    else:
        return None


def select_current_word(text: str, cursor_pos: int) -> tuple[int, int]:
    """Select the word the cursor points to."""
    delimiters = r".,\/!?%^*;:{}=`~()<> " + "\t\r\n"
    start = end = cursor_pos

    # seek backward to find beginning
    while start > 0 and text[start - 1] not in delimiters:
        start -= 1

    # seek forward to find end
    while end < len(text) and text[end] not in delimiters:
        end += 1

    return start, end


def select_on_cursor_pos(text: str, cursor_pos: int) -> tuple[int, int]:
    """Return a range in the text based on the cursor_position."""
    return select_current_parenthesis_block(
        text, cursor_pos, ["(", "<"], [")", ">"]
    ) or select_current_word(text, cursor_pos)


class ExprNode:
    def __init__(self, type, value, weight=1.0, children=None):
        self.type = type  # 'text' or 'expr'
        self.value = value  # text or sub-expression
        self.weight = weight  # weight for 'expr' nodes
        self.children = children if children is not None else []  # child nodes

    def __repr__(self):
        if self.type == "text":
            return f"Text('{self.value}')"
        else:
            assert self.type == "expr"
            return f"Expr({self.children}, weight={self.weight})"


def parse_expr(expression: str) -> list[ExprNode]:
    """
    Parses following attention syntax language.
    expr = text | (expr:number)
    expr = text + expr | expr + text
    """

    def parse_segment(segment):
        match = re.match(r"^[([{<](.*?):(-?[\d.]+)[\]})>]$", segment, flags=re.DOTALL)
        if match:
            inner_expr = match.group(1)
            number = float(match.group(2))
            return ExprNode("expr", None, weight=number, children=parse_expr(inner_expr))
        else:
            return ExprNode("text", segment)

    segments = []
    stack = []
    start = 0
    bracket_pairs = {"(": ")", "<": ">"}

    for i, char in enumerate(expression):
        if char in bracket_pairs:
            if not stack:
                if start != i:
                    segments.append(ExprNode("text", expression[start:i]))
                start = i

            stack.append(bracket_pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                node = parse_segment(expression[start : i + 1])
                if node.type == "expr":
                    segments.append(node)
                    start = i + 1
                else:
                    stack.append(char)

    if start < len(expression):
        remaining_text = expression[start:].strip()
        if remaining_text:
            segments.append(ExprNode("text", remaining_text))

    return segments


def edit_attention(text: str, positive: bool) -> str:
    """Edit the attention of text within the prompt."""
    if text == "":
        return text

    segments = parse_expr(text)
    if len(segments) == 1 and segments[0].type == "expr":
        attention_string = text[1 : text.rfind(":")]
        weight = segments[0].weight
        open_bracket = text[0]
        close_bracket = text[-1]
    elif text[0] == "<":
        attention_string = text[1:-1]
        weight = 1.0
        open_bracket = "<"
        close_bracket = ">"
    else:
        attention_string = text
        weight = 1.0
        open_bracket = "("
        close_bracket = ")"

    weight = weight + 0.1 * (1 if positive else -1)
    weight = max(weight, -2.0)
    weight = min(weight, 2.0)

    return (
        attention_string
        if weight == 1.0 and open_bracket == "("
        else f"{open_bracket}{attention_string}:{weight:.1f}{close_bracket}"
    )


# creates the img text metadata for embedding in PNG files in style like Automatic1111
def lora_name(name: str):
    """A LoRA file id or tag as "folder/name": forward slashes, lower case, no .safetensors."""
    name = name.replace("\\", "/").lower()
    return name.removesuffix(".safetensors")


def normalize_lora_tags(prompt: str):
    """<lora:folder/Name.safetensors:0.8> as <lora:Name:0.8>, the way Forge writes them."""

    def replace(match: re.Match[str]):
        name = match[1].replace("\\", "/").rsplit("/", 1)[-1]
        name = re.sub(r"\.safetensors$", "", name, flags=re.IGNORECASE)
        return (
            match[0][: match.start(1) - match.start()]
            + name
            + match[0][match.end(1) - match.start() :]
        )

    return pattern_lora.sub(replace, prompt)


def create_img_metadata(params: JobParams):
    """PNG info as Forge writes it, for the prompts as typed: LoRA tags where they were typed
    (style-only LoRAs and the style's text aren't the user's prompt, so they're left out)."""
    meta = params.metadata

    prompt = normalize_lora_tags(meta.get("prompt", meta.get("prompt_final", "")))
    neg_prompt = meta.get("negative_prompt", meta.get("negative_prompt_final", ""))
    sampler = meta.get("sampler", "")
    steps = meta.get("steps", 0)
    cfg_scale = meta.get("guidance", 0.0)
    model = meta.get("checkpoint", "Unknown")
    seed = params.seed
    width = params.bounds.width
    height = params.bounds.height
    strength = meta.get("strength", None)

    # Construct output
    lines = []
    lines.append(prompt.strip())
    lines.append(f"Negative prompt: {neg_prompt}")
    lines.append(
        f"Steps: {steps}, Sampler: {sampler}, CFG scale: {cfg_scale}, Seed: {seed}, Size: {width}x{height}, Model hash: unknown, Model: {model}"
    )

    if strength is not None and strength != 1.0:
        lines[-1] += f", Denoising strength: {strength}"

    return "\n".join(lines)
