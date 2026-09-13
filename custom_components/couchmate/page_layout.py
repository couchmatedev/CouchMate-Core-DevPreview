"""Shared visual shell for the local Core administration pages."""
from __future__ import annotations

from html import escape


PAGE_STYLES = """
:root {
  color-scheme: dark;
  --bg: #0f1416;
  --surface: #171d20;
  --surface2: #20282c;
  --card: var(--surface);
  --card2: var(--surface2);
  --muted: #a7b1b6;
  --accent: #35c5b6;
  --accent-rgb: 53,197,182;
  --line: #39464c;
  --danger: #ff9b9b;
  --ok: #79d8a8;
  --warning: #ffd88a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: #f5f7f7;
  font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}
.wrap { max-width: 1480px; margin: auto; padding: 38px 50px 86px; }
.top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 28px;
  position: sticky;
  top: 0;
  z-index: 5;
  padding: 0 0 24px;
  background: linear-gradient(var(--bg) 88%,transparent);
}
.page-heading { min-width: 0; }
.eyebrow {
  color: var(--accent);
  font-weight: 750;
  letter-spacing: .04em;
  text-transform: uppercase;
  font-size: 13px;
}
h1 { font-size: 40px; line-height: 1.08; margin: 8px 0 10px; }
h2 { font-size: 28px; margin: 0 0 20px; }
h3 { font-size: 19px; margin: 0; }
.sub { color: var(--muted); font-size: 17px; line-height: 1.45; }
.section { margin-top: 46px; }
.top-actions { display: flex; flex-direction: column; align-items: flex-end; gap: 14px; }
.nav {
  display: flex;
  gap: 8px;
  background: #101719;
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 6px;
}
.nav a {
  color: var(--muted);
  text-decoration: none;
  padding: 11px 15px;
  border-radius: 11px;
  font-weight: 700;
  text-align: center;
  white-space: nowrap;
}
.nav a:hover { background: var(--surface); color: #fff; }
.nav a[aria-current="page"] { background: var(--surface2); color: #fff; }
.button, button.primary {
  border: 0;
  border-radius: 14px;
  padding: 13px 17px;
  font: inherit;
  font-weight: 750;
  cursor: pointer;
  background: var(--accent);
  color: #071411;
}
.button:disabled, button.primary:disabled { opacity: .48; cursor: not-allowed; }
.button:focus-visible, button.primary:focus-visible, .nav a:focus-visible {
  outline: 3px solid var(--accent);
  outline-offset: 3px;
}
@media (max-width: 1100px) {
  .top { flex-direction: column; gap: 22px; }
  .top-actions { width: 100%; flex-direction: row; align-items: center; justify-content: space-between; }
}
@media (max-width: 760px) {
  .wrap { padding: 26px 18px 70px; }
  .top { position: static; }
  .top-actions { flex-direction: column; align-items: stretch; gap: 12px; }
  .nav a { flex: 1; padding: 11px 8px; white-space: normal; }
  h1 { font-size: 32px; }
}
"""

_PAGES = {
    "configurator": ("Geräte & Funktionen", "Räume, Quellen und Funktionen"),
    "flow": ("Flow-Leiste", "Vorschläge und eigene Inhalte unter der Hero Card"),
    "management": (
        "Apple TVs & Design",
        "Gemeinsame Einstellungen bleiben lokal in deinem Home Assistant.",
    ),
}


def render_page(document: str, section: str) -> str:
    """Apply the same header and styles without changing each page's controls."""
    title, description = _PAGES[section]
    links = "".join(
        f'<a href="{href}" data-core-section="{key}"'
        + (' aria-current="page"' if key == section else "")
        + f'>{escape(label)}</a>'
        for key, (label, _) in _PAGES.items()
        for href in ["/couchmate/configurator?section=flow" if key == "flow" else f"/couchmate/{key}"]
    )
    action = (
        '<button id="save" class="primary" type="button">Auswahl speichern</button>'
        if section in ("configurator", "flow")
        else ""
    )
    header = (
        '<header class="top"><div class="page-heading">'
        '<div class="eyebrow">CouchMate Core Dev Preview</div>'
        f'<h1 id="pageTitle">{escape(title)}</h1><div id="pageDescription" class="sub">{escape(description)}</div>'
        '</div><div class="top-actions"><nav class="nav" aria-label="Core-Bereiche">'
        f'{links}</nav>{action}</div></header>'
    )
    return document.replace(
        "<!--CORE_PAGE_STYLES-->", f"<style>{PAGE_STYLES}</style>"
    ).replace("<!--CORE_PAGE_HEADER-->", header)
