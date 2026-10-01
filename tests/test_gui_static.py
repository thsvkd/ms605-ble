"""ms605/gui/static: the committed SPA build (docs/GUI_API.md 3.3). index.html must exist and every
file it references must be shipped next to it, so `uv run ms605 gui` works without Node."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "ms605" / "gui" / "static"
_REF = re.compile(r"""(?:src|href)=["']/?([^"':?#]+)["']""")


def test_index_html_exists() -> None:
    assert (STATIC / "index.html").is_file(), "run: cd web && npm ci && npm run build"


def test_every_referenced_file_exists() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    refs = _REF.findall(html)
    assets = [r for r in refs if r.startswith("assets/")]
    assert assets, "index.html references no assets/ files"
    missing = [r for r in refs if not (STATIC / r).is_file()]
    assert missing == []
