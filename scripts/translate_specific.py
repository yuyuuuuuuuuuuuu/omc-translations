#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import argparse
from pathlib import Path
from bs4 import BeautifulSoup
import openai
from playwright.sync_api import sync_playwright, Page, Browser

# ───────────────────────────────────────────────────────────
# 設定
# ───────────────────────────────────────────────────────────
THIS_DIR    = Path(__file__).parent
JA_ROOT     = THIS_DIR.parent / "languages" / "ja" / "contests"
EN_ROOT     = THIS_DIR.parent / "languages" / "en" / "contests"

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("[Error] environment variable OPENAI_API_KEY is not set", file=sys.stderr)
    sys.exit(1)
openai.api_key = OPENAI_KEY

GPT_MODEL = "gpt-4o-mini"

# ───────────────────────────────────────────────────────────
# ヘルパー関数
# ───────────────────────────────────────────────────────────
def HtmlKatex(html: str) -> str:
    """Embedded KaTeX → $...$."""
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.select(".katex"):
        ann = katex.find("annotation", {"encoding": "application/x-tex"})
        if ann:
            katex.replace_with(f"${ann.text}$")
    return str(soup)

def ask_gpt(question: str, model: str, term: str) -> str:
    """Translate HTML+KaTeX preserving markup."""
    resp = openai.ChatCompletion.create(
        model=model,
        messages=[
            {"role":"system","content":(
                f"The text you are about to receive is HTML-formatted KaTeX math {term} written in Japanese.\n"
                "Please translate all sentences into English, preserving all KaTeX formatting\n"
                "(font size, line breaks, class=\"katex-display\" for display formulas, etc.).\n"
                "Return ONLY the translated HTML; do not wrap it in extra tags."
            )},
            {"role":"user","content":question}
        ],
        temperature=0.0
    )
    return resp.choices[0].message.content.strip()

def translate_html_for_lang(jp_html: str, term: str) -> str:
    """Run HTML→KaTeX extraction→GPT→HTML."""
    return ask_gpt(HtmlKatex(jp_html), GPT_MODEL, term)

def render_html_with_playwright(page: Page, file_path: Path):
    """Inject KaTeX, render in headless browser, extract body."""
    header = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/contrib/auto-render.min.js"
 onload="renderMathInElement(document.body,{
   delimiters:[
     {left:'$$',right:'$$',display:true},
     {left:'$', right:'$', display:false}
   ]
 });"></script>
</head><body>
"""
    original = file_path.read_text(encoding="utf-8")
    file_path.write_text(header + original + "\n</body></html>", encoding="utf-8")
    page.goto(f"file://{file_path.resolve()}", wait_until="networkidle")
    page.wait_for_load_state("networkidle")
    soup = BeautifulSoup(page.content(), "html.parser")
    body = soup.body.decode_contents()
    final = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
</head><body>
{body}
</body></html>"""
    file_path.write_text(final, encoding="utf-8")

def wrap_display(file_path: Path):
    """Center display-mode formulas."""
    html = file_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    for elt in soup.select("span.katex-display"):
        wrapper = soup.new_tag("div", style="text-align:center;")
        elt.wrap(wrapper)
    file_path.write_text(str(soup), encoding="utf-8")

# ───────────────────────────────────────────────────────────
# translate_specific 関数
# ───────────────────────────────────────────────────────────
def translate_specific(contest: str, item_id: str, kind: str):
    """
    contest: コンテストID
    item_id: task_id, editorial_id, or user_id
    kind: 'task'|'editorial'|'user_editorial'
    """
    subdirs = {"task":"tasks",
               "editorial":"editorial",
               "user_editorial":"user_editorial"}
    terms   = {"task":"task",
               "editorial":"editorial",
               "user_editorial":"user editorial"}

    if kind not in subdirs:
        print(f"[Error] kind must be one of {list(subdirs)}", file=sys.stderr)
        return

    # paths
    ja_path = JA_ROOT / contest / subdirs[kind] / f"{item_id}.html"
    en_path = EN_ROOT / contest / subdirs[kind] / f"{item_id}.html"

    if not ja_path.exists():
        print(f"[Error] Japanese file not found: {ja_path}", file=sys.stderr)
        return

    # 1) extract KaTeX-ready text
    jp_html = ja_path.read_text(encoding="utf-8")
    latex_ready = HtmlKatex(jp_html)
    print("=== KaTeX Input to GPT ===")
    print(latex_ready)
    print("=== End of Input ===\n")

    # 2) GPT translation
    translated = ask_gpt(latex_ready, GPT_MODEL, terms[kind])
    print("=== GPT Output ===")
    print(translated)
    print("=== End of Output ===\n")

    # 3) write translated HTML
    en_path.parent.mkdir(parents=True, exist_ok=True)
    en_path.write_text(translated, encoding="utf-8")
    print(f"[Saved EN] {en_path}")

    # 4) render & wrap
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        render_html_with_playwright(page, en_path)
        wrap_display(en_path)
        browser.close()

    print(f"[Done] translate_specific({kind}, {contest}, {item_id})")

# ───────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind",
                        choices=["task","editorial","user_editorial"],
                        help="Translate kind")
    parser.add_argument("contest", help="Contest ID (e.g. omcb047)")
    parser.add_argument("item_id", help="Task ID, editorial ID, or user editorial ID")
    args = parser.parse_args()

    translate_specific(args.contest, args.item_id, args.kind)
