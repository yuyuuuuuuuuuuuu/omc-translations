#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import argparse
from pathlib import Path

from bs4 import BeautifulSoup
import openai
from playwright.sync_api import sync_playwright, Page, Browser

# ───────────────────────────────────────────────────────────
# 設定
# ───────────────────────────────────────────────────────────
BASE_URL = "https://onlinemathcontest.com"
THIS_DIR = Path(__file__).parent
EN_ROOT  = THIS_DIR.parent / "languages" / "en" / "contests"

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("[Error] environment variable OPENAI_API_KEY is not set", file=sys.stderr)
    sys.exit(1)
openai.api_key = OPENAI_KEY

GPT_MODEL = "gpt-4o-mini"

# ───────────────────────────────────────────────────────────
# ヘルパー関数
# ───────────────────────────────────────────────────────────
def fetch_content_with_playwright(page: Page, url: str) -> str:
    """
    Headless Chromium でページを開き、
    JS コンテキストの global `content` 変数を取得する。
    """
    page.goto(url, wait_until="networkidle")
    page.wait_for_load_state("networkidle")
    return page.evaluate("content")

def apply_markdown(html: str) -> str:
    """
    Markdown ライクな
      *italic* → <em>
      **bold**  → <strong>
      単独行に ***   → <hr>
    を置換する。
    """
    lines = html.splitlines()
    out = []
    for line in lines:
        if re.match(r'^\s*\*{3}\s*$', line):
            out.append('<hr>')
        else:
            out.append(line)
    html = "\n".join(out)
    # bold
    html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
    # italic
    html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', html)
    return html

def HtmlKatex(html: str) -> str:
    """
    KaTeX レンダリング済みタグから TeX 部分を "$...$" に戻す。
    """
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.select(".katex"):
        ann = katex.find("annotation", {"encoding": "application/x-tex"})
        if ann:
            katex.replace_with(f"${ann.text}$")
    return str(soup)

def ask_gpt(question: str, model: str, term: str) -> str:
    """
    OpenAI に HTML+KaTeX を壊さず翻訳させる。
    """
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
    """
    日本語 HTML → KaTeX を $...$ に置換 → GPT 翻訳 → HTML 返却
    """
    latex_ready = HtmlKatex(jp_html)
    return ask_gpt(latex_ready, GPT_MODEL, term)

def render_html_with_playwright(page: Page, file_path: Path):
    """
    翻訳後 HTML を headless Chromium で開き KaTeX をレンダリングし、
    <body> 部分だけを抜き出して上書き保存。
    """
    header = """<!DOCTYPE html>
<html><head>
  <meta charset="utf-8">
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
  <script defer
          src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.js"></script>
  <script defer
          src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/contrib/auto-render.min.js"
          onload="renderMathInElement(document.body,{
            delimiters:[
              {left:'$$',right:'$$',display:true},
              {left:'$', right:'$', display:false}
            ]
          });"></script>
</head><body>
"""
    content = file_path.read_text(encoding="utf-8")
    file_path.write_text(header + content + "\n</body></html>", encoding="utf-8")

    page.goto(f"file://{file_path.resolve()}", wait_until="networkidle")
    page.wait_for_load_state("networkidle")
    soup = BeautifulSoup(page.content(), "html.parser")
    body = soup.body.decode_contents()

    final = f"""<!DOCTYPE html>
<html><head>
  <meta charset="utf-8">
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
</head><body>
{body}
</body></html>"""
    file_path.write_text(final, encoding="utf-8")

def wrap_display(file_path: Path):
    """
    <span class="katex-display"> を中央寄せの <div> でラップ。
    """
    html = file_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    for elt in soup.select("span.katex-display"):
        wrapper = soup.new_tag("div", style="text-align:center;")
        elt.wrap(wrapper)
    file_path.write_text(str(soup), encoding="utf-8")

# ───────────────────────────────────────────────────────────
# translate_specific_remote 関数
# ───────────────────────────────────────────────────────────
def translate_specific_remote(contest: str, item_id: str, kind: str, user_id: str = None):
    """
    contest: コンテストID
    item_id: task/editorial の ID
    kind: 'task' | 'editorial' | 'user_editorial'
    user_id: user_editorial の場合に指定
    """
    if kind == "task":
        url = f"{BASE_URL}/contests/{contest}/tasks/{item_id}"
        term = "task"
        subdir = "tasks"
        outfile = item_id
    elif kind == "editorial":
        url = f"{BASE_URL}/contests/{contest}/editorial/{item_id}"
        term = "editorial"
        subdir = "editorial"
        outfile = item_id
    elif kind == "user_editorial":
        if not user_id:
            print("[Error] user_id is required for user_editorial", file=sys.stderr)
            return
        url = f"{BASE_URL}/contests/{contest}/editorial/{item_id}/{user_id}"
        term = "user editorial"
        subdir = "user_editorial"
        outfile = f"{item_id}/{user_id}"
    else:
        print("[Error] kind must be one of 'task','editorial','user_editorial'", file=sys.stderr)
        return

    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()

        # 1) 動的に埋め込まれた HTML を取得
        print(f"→ Fetching content from {url}")
        raw_html = fetch_content_with_playwright(page, url)

        # 2) Markdown ライクな装飾を HTML に
        md_html = apply_markdown(raw_html)

        # 3) KaTeX→$...$化して GPT へ入力
        latex_ready = HtmlKatex(md_html)
        print("=== KaTeX Input to GPT ===")
        print(latex_ready)
        print("=== End of Input ===\n")

        # 4) 翻訳
        translated = ask_gpt(latex_ready, GPT_MODEL, term)
        print("=== GPT Output ===")
        print(translated)
        print("=== End of Output ===\n")

        # 5) languages/en/... に上書き保存
        en_path = EN_ROOT / contest / subdir / f"{outfile}.html"
        en_path.parent.mkdir(parents=True, exist_ok=True)
        en_path.write_text(translated, encoding="utf-8")
        print(f"→ Saved EN: {en_path}")

        # 6) レンダリング & センターラップ
        render_html_with_playwright(page, en_path)
        wrap_display(en_path)

        browser.close()

    print(f"[Done] translate_specific_remote({kind}, {contest}, {item_id})")

# ───────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind",
                        choices=["task","editorial","user_editorial"],
                        help="Translate kind")
    parser.add_argument("contest", help="Contest ID (e.g. omcb047)")
    parser.add_argument("item_id", help="Task or editorial ID")
    parser.add_argument("--user_id", help="User editorial ID (for kind=user_editorial)")
    args = parser.parse_args()

    translate_specific_remote(args.contest, args.item_id, args.kind, args.user_id)
