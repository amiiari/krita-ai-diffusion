"""Caption Studio: caption text layers, stamps and Save All for the caption / edit passes of
the Content Manager's pipeline (formerly its own Krita plugin, caption_studio).

The user's files -- presets.json, text_style.aslxml (the captured layer style), assets/
(stamps), and the Save All bridge's request / result files -- live in DATA_DIR.
"""

from __future__ import annotations

import os

from ..util import user_data_dir

DATA_DIR = os.path.join(str(user_data_dir), "captions")
os.makedirs(os.path.join(DATA_DIR, "assets"), exist_ok=True)
