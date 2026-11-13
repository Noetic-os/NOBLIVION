# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rebuild the PDFs in ``docs/`` from their Markdown files.

Run it with no arguments from any folder:

    uv run --with weasyprint --with markdown python tools/build_pdfs.py

This is the one tool that needs packages outside the standard library:
WeasyPrint (with the pango library on the system) and Markdown.
"""

import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"

# Markdown file name (without ``.md``) and the PDF title.
BOOKS = (
    ("executive-summary", "NOBLIVION executive summary"),
    ("noblivion-against-claude-code-memory", "NOBLIVION against Claude Code memory"),
    ("long-run-report", "NOBLIVION long-run test report"),
    ("memory-improvements", "Where NOBLIVION improves Claude Code memory"),
)

CSS = """
@page {
  size: A4; margin: 20mm 18mm;
  @bottom-center { content: counter(page); font: 9pt sans-serif; color: #666 }
}
body { font: 10pt/1.45 'DejaVu Sans', sans-serif; color: #1b2530 }
h1 { font-size: 20pt; margin: 0 0 8pt }
h2 { font-size: 14pt; margin: 18pt 0 6pt }
h3 { font-size: 11.5pt; margin: 12pt 0 4pt }
table { border-collapse: collapse; width: 100%; font-size: 8pt; margin: 8pt 0 }
th, td {
  border: 0.5pt solid #b8c2cc; padding: 3pt 4pt;
  vertical-align: top; text-align: left; overflow-wrap: anywhere
}
th { background: #eef3f6; overflow-wrap: normal; word-break: normal; hyphens: none }
tr { break-inside: avoid }
code { font: 8.5pt 'DejaVu Sans Mono', monospace; overflow-wrap: anywhere }
img { max-width: 100%; height: auto }
a { color: #0b6e74 }
"""


def build(name: str, title: str) -> Path:
    import markdown
    from weasyprint import CSS as Style
    from weasyprint import HTML

    text = (DOCS / f"{name}.md").read_text(encoding="utf-8")
    text = re.sub(r"^<!--.*?-->\s*", "", text, flags=re.S)  # the SPDX comment
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    page = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
        f"</head><body>{body}</body></html>"
    )
    out = DOCS / f"{name}.pdf"
    HTML(string=page, base_url=f"{DOCS}/").write_pdf(out, stylesheets=[Style(string=CSS)])
    return out


def main() -> int:
    try:
        import markdown  # noqa: F401
        import weasyprint  # noqa: F401
    except ImportError:
        print(
            "usage: uv run --with weasyprint --with markdown python tools/build_pdfs.py",
            file=sys.stderr,
        )
        return 2
    for name, title in BOOKS:
        print("built", build(name, title))
    return 0


if __name__ == "__main__":
    sys.exit(main())
