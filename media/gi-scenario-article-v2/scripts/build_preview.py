#!/usr/bin/env python3
"""Build the article and its methods notes as linked, local HTML pages."""

from __future__ import annotations

import html
import hashlib
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from markdown_it import MarkdownIt


ARTICLE_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ARTICLE_ROOT / "dist"
DOCUMENTS = ("article", "methods")


def preview_url(value: str) -> str:
    """Keep source-relative asset links valid one directory below the Markdown."""
    parts = urlsplit(html.unescape(value))
    if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
        return html.unescape(value)
    target = (ARTICLE_ROOT / unquote(parts.path)).resolve()
    if target.parent == ARTICLE_ROOT and target.stem in DOCUMENTS and target.suffix == ".md":
        target = OUTPUT_ROOT / f"{target.stem}.html"
    relative = quote(os.path.relpath(target, OUTPUT_ROOT), safe="/-._~")
    return urlunsplit(("", "", relative, parts.query, parts.fragment))


def relocate_links(body: str) -> str:
    def asset_url(value: str) -> str:
        rewritten = preview_url(value)
        # SVGs used as <img> cannot reliably load their external local fonts.
        # The renderer makes these PNGs with the registered fonts first.
        if rewritten.startswith("../figures/") and rewritten.endswith(".svg"):
            return rewritten[:-4] + ".png"
        return rewritten

    def replace(match: re.Match[str]) -> str:
        attribute, delimiter, value = match.groups()
        if attribute == "srcset":
            items = []
            for item in value.split(","):
                url, *descriptor = item.strip().split()
                items.append(" ".join([asset_url(url), *descriptor]))
            rewritten = ", ".join(items)
        else:
            rewritten = asset_url(value) if attribute == "src" else preview_url(value)
        return f"{attribute}={delimiter}{html.escape(rewritten, quote=True)}{delimiter}"

    return re.sub(r"\b(href|src|srcset)=([\"'])(.*?)\2", replace, body)


def render_markdown(source: Path) -> tuple[str, str]:
    markdown = MarkdownIt("commonmark", {"html": True, "linkify": False}).enable("table")
    tokens = markdown.parse(source.read_text())
    title = None
    heading_ids: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        content = tokens[index + 1].content
        slug = re.sub(r"[^\w\s-]", "", content.lower())
        slug = re.sub(r"[\s_]+", "-", slug).strip("-")
        duplicate = heading_ids.get(slug, 0)
        heading_ids[slug] = duplicate + 1
        token.attrSet("id", f"{slug}-{duplicate}" if duplicate else slug)
        if token.tag == "h1" and title is None:
            title = content
    if not title:
        raise ValueError(f"{source.name} needs a document title in its first H1")
    return title, relocate_links(markdown.renderer.render(tokens, markdown.options, {}))


def document_html(title: str, body: str, kind: str, source_sha256: str) -> str:
    companion = "methods" if kind == "article" else "article"
    companion_label = "Methods and evidence" if kind == "article" else "Read the article"
    edition = "Article draft" if kind == "article" else "Methods notes"
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="source-document" content="{kind}.md">
  <meta name="source-sha256" content="{source_sha256}">
  <title>{html.escape(title)}</title>
  <style>
    @font-face {{ font-family: Atkinson; src: url('../../gi-scenario-film/assets/fonts/AtkinsonHyperlegible-Regular.ttf'); font-display: swap; }}
    @font-face {{ font-family: Atkinson; src: url('../../gi-scenario-film/assets/fonts/AtkinsonHyperlegible-Bold.ttf'); font-weight: 700; font-display: swap; }}
    @font-face {{ font-family: PlexCondensed; src: url('../../gi-scenario-film/assets/fonts/IBMPlexSansCondensed-SemiBold.ttf'); font-weight: 600; font-display: swap; }}
    @font-face {{ font-family: PlexMono; src: url('../../gi-scenario-film/assets/fonts/IBMPlexMono-Regular.ttf'); font-display: swap; }}
    :root {{ --paper: #F5F7F6; --ink: #17313A; --teal: #08766F; --blue: #2F6F9F; --muted: #48616B; --line: #CBD8D8; }}
    * {{ box-sizing: border-box; }}
    html {{ background: var(--paper); }}
    body {{ margin: 0; color: var(--ink); font: 20px/1.67 Atkinson, system-ui, sans-serif; }}
    .masthead {{ border-top: 5px solid var(--teal); border-bottom: 1px solid var(--line); }}
    .masthead-inner {{ width: min(1120px, calc(100% - 48px)); margin: auto; padding: 17px 0; display: flex; justify-content: space-between; gap: 24px; align-items: center; }}
    .brand {{ font-family: PlexCondensed, sans-serif; font-size: 21px; font-weight: 600; }}
    .masthead nav {{ display: flex; align-items: center; gap: 20px; font-size: 15px; }}
    .edition {{ color: var(--muted); }}
    main {{ display: block; }}
    article {{ width: min(760px, calc(100% - 48px)); margin: 0 auto; padding: 70px 0 90px; overflow-wrap: break-word; }}
    h1, h2, h3 {{ font-family: PlexCondensed, Atkinson, sans-serif; letter-spacing: -.012em; text-wrap: balance; }}
    h1 {{ font-size: clamp(44px, 5.2vw, 64px); line-height: 1.06; margin: 0 0 35px; }}
    h1 + p {{ font-size: 23px; line-height: 1.55; }}
    h2 {{ font-size: 36px; line-height: 1.15; margin: 65px 0 22px; }}
    h3 {{ font-size: 27px; line-height: 1.2; margin: 42px 0 18px; }}
    p {{ margin: 0 0 25px; }}
    ul, ol {{ margin: 0 0 28px; padding-left: 28px; }}
    li {{ padding-left: 5px; margin: 8px 0; }}
    a {{ color: var(--teal); text-decoration-thickness: 1.3px; text-underline-offset: 3px; overflow-wrap: anywhere; }}
    a:hover {{ color: var(--blue); }}
    a:focus-visible {{ outline: 3px solid var(--blue); outline-offset: 4px; border-radius: 2px; }}
    code {{ font-family: PlexMono, monospace; font-size: .81em; background: #E7EEEE; border-radius: 4px; padding: .1em .25em; overflow-wrap: anywhere; }}
    pre {{ padding: 20px; background: #E7EEEE; white-space: pre-wrap; overflow-wrap: anywhere; }}
    pre code {{ padding: 0; }}
    blockquote {{ margin: 35px 0; padding: 8px 0 8px 24px; border-left: 3px solid var(--teal); color: var(--muted); }}
    blockquote p:last-child {{ margin: 0; }}
    figure {{ width: min(1120px, calc(100vw - 48px)); margin: 45px 0 48px; position: relative; left: 50%; transform: translateX(-50%); }}
    picture, picture img {{ display: block; width: 100%; }}
    figure img {{ border: 1px solid var(--line); border-radius: 14px; background: var(--paper); }}
    figcaption {{ max-width: 860px; margin: 16px auto 0; color: var(--muted); font-size: 16px; line-height: 1.5; }}
    table {{ width: 100%; table-layout: fixed; border-collapse: collapse; margin: 35px 0 40px; font-size: 17px; line-height: 1.48; background: #FFFFFF; }}
    caption {{ caption-side: top; text-align: left; font-size: 18px; line-height: 1.5; color: var(--muted); padding: 0 0 14px; }}
    th, td {{ border: 1px solid var(--line); padding: 13px 14px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
    thead th {{ background: #E7EEEE; font-weight: 700; }}
    tbody th {{ font-weight: 700; background: #F4F8F7; }}
    table p:last-child {{ margin-bottom: 0; }}
    .rules-table th:nth-child(1), .rules-table td:nth-child(1) {{ width: 32%; }}
    .rules-table th:nth-child(2), .rules-table td:nth-child(2) {{ width: 50%; }}
    .rules-table th:nth-child(3), .rules-table td:nth-child(3) {{ width: 18%; }}
    .results-table th:nth-child(1), .results-table td:nth-child(1) {{ width: 60%; }}
    .results-table th:nth-child(2), .results-table td:nth-child(2), .results-table th:nth-child(3), .results-table td:nth-child(3) {{ width: 20%; }}
    .results-table td:nth-child(n+2) {{ font-variant-numeric: tabular-nums; }}
    #call-accounting th:nth-child(1), #call-accounting td:nth-child(1) {{ width: 28%; }}
    #call-accounting th:nth-child(2), #call-accounting td:nth-child(2) {{ width: 58%; }}
    #call-accounting th:nth-child(3), #call-accounting td:nth-child(3) {{ width: 14%; }}
    hr {{ border: 0; border-top: 1px solid var(--line); margin: 45px 0; }}
    .page-footer {{ border-top: 1px solid var(--line); padding-top: 24px; margin-top: 50px; font-size: 17px; }}
    @media (max-width: 760px) {{
      body {{ font-size: 18px; line-height: 1.62; }}
      .masthead-inner {{ width: calc(100% - 32px); align-items: flex-start; flex-direction: column; gap: 6px; }}
      .masthead nav {{ width: 100%; justify-content: space-between; font-size: 14px; }}
      article {{ width: calc(100% - 32px); padding: 42px 0 64px; }}
      h1 {{ font-size: 43px; line-height: 1.08; margin-bottom: 30px; }}
      h1 + p {{ font-size: 20px; line-height: 1.58; }}
      h2 {{ font-size: 31px; margin-top: 48px; }}
      h3 {{ font-size: 25px; }}
      figure {{ width: calc(100vw - 24px); margin: 34px 0 38px; }}
      figure img {{ border-radius: 10px; }}
      figcaption {{ padding: 0 4px; font-size: 15px; }}
      table {{ margin: 28px 0 32px; font-size: 14px; line-height: 1.45; }}
      caption {{ font-size: 16px; padding-bottom: 12px; }}
      th, td {{ padding: 9px 7px; }}
      .rules-table th:nth-child(1), .rules-table td:nth-child(1) {{ width: 31%; }}
      .rules-table th:nth-child(2), .rules-table td:nth-child(2) {{ width: 45%; }}
      .rules-table th:nth-child(3), .rules-table td:nth-child(3) {{ width: 24%; }}
      .results-table th:nth-child(1), .results-table td:nth-child(1) {{ width: 56%; }}
      .results-table th:nth-child(2), .results-table td:nth-child(2), .results-table th:nth-child(3), .results-table td:nth-child(3) {{ width: 22%; }}
    }}
    @media print {{ .masthead {{ display: none; }} article {{ width: 100%; padding: 0; }} figure {{ width: 100%; left: auto; transform: none; }} a {{ color: inherit; }} }}
  </style>
</head>
<body>
  <header class="masthead">
    <div class="masthead-inner">
      <div class="brand">GI Scenario Studio</div>
      <nav aria-label="Article navigation"><span class="edition">{edition}</span><a href="{companion}.html">{companion_label}</a></nav>
    </div>
  </header>
  <main><article>{body}<footer class="page-footer"><a href="{companion}.html">{companion_label}</a></footer></article></main>
</body>
</html>'''


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    for name in DOCUMENTS:
        source = ARTICLE_ROOT / f"{name}.md"
        title, body = render_markdown(source)
        output = OUTPUT_ROOT / f"{name}.html"
        source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        output.write_text(document_html(title, body, name, source_sha256))
        print(f"built {output.relative_to(ARTICLE_ROOT)}")


if __name__ == "__main__":
    main()
