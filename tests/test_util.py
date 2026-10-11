from pathlib import Path
from tempfile import TemporaryDirectory

from ai_diffusion.util import batched, find_unused_path, sanitize_prompt, unique_latest


def test_batched():
    iterable = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    n = 3
    expected_output = [(1, 2, 3), (4, 5, 6), (7, 8, 9)]
    assert list(batched(iterable, n)) == expected_output


def test_sanitize_prompt():
    assert sanitize_prompt("") == "no prompt"
    assert sanitize_prompt("a" * 50) == "a" * 40
    assert sanitize_prompt("bla\nblub\n<x:2:4> (neg) [pos]") == "blablubx24 neg pos"


def test_unused_path():
    with TemporaryDirectory() as dir:
        file = Path(dir) / "test.txt"
        assert find_unused_path(file) == file
        file.touch()
        assert find_unused_path(file) == Path(dir) / "test-1.txt"
        (Path(dir) / "test-1.txt").touch()
        assert find_unused_path(file) == Path(dir) / "test-2.txt"


def test_unique_latest():
    # first-seen order, the last item per key wins (a typed LoRA weight over the style's)
    items = [("a", 1), ("b", 1), ("a", 2)]
    assert unique_latest(items, key=lambda x: x[0]) == [("a", 2), ("b", 1)]
