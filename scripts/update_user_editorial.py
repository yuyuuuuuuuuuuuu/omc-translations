#!/usr/bin/env python3
# scripts/fetch_and_translate_all_user_editorials.py

import os
import sys
import re
import time
import json
import requests
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup

import openai
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Browser

# ───────────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────────
BASE_URL     = "https://onlinemathcontest.com"
THIS_SCRIPT  = Path(__file__).resolve()
BASE_DIR     = THIS_SCRIPT.parent.parent        # parent of scripts/
LANG_ROOT    = BASE_DIR / "languages"
JA_ROOT      = LANG_ROOT / "ja" / "contests"
EN_ROOT      = LANG_ROOT / "en" / "contests"

OPENAI_KEY   = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("[Error] environment variable OPENAI_API_KEY is not set", file=sys.stderr)
    sys.exit(1)
openai.api_key = OPENAI_KEY
GPT_MODEL    = "gpt-4o-mini"

# ───────────────────────────────────────────────────────────
# 1) fetch all contest names
# ───────────────────────────────────────────────────────────
def get_all_contests() -> list[str]:
    i = 1
    out = []
    while True:
        url = f"{BASE_URL}/contests/all?page={i}"
        resp = requests.get(url); resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        tbl = soup.find("div", class_="table-responsive")
        if not tbl:
            break
        links = tbl.find_all("a", href=True)
        names = []
        for a in links:
            href = a["href"]
            if "/contests/" in href:
                name = href.split("/contests/")[1].strip()
                if name:
                    names.append(name)
        if not names:
            break
        out.extend(names)
        i += 1
    return out

# ───────────────────────────────────────────────────────────
# 2) Playwright helper to extract innerHTML
# ───────────────────────────────────────────────────────────
def extract_div_innerhtml_with_playwright(page: Page, url: str, div_id: str, timeout: int = 8000) -> str:
    """Navigate to url and return the innerHTML of <div id="{div_id}">"""
    try:
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector(f"#{div_id}", timeout=timeout)
        content = page.eval_on_selector(f"#{div_id}", "el => el.innerHTML")
        return content or ""
    except PlaywrightTimeoutError:
        print(f"[Timeout] #{div_id} not found at {url}", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"[Error] Playwright error fetching {url}: {e}", file=sys.stderr)
        return ""

# ───────────────────────────────────────────────────────────
# 3) KaTeX ↔ HTML helpers
# ───────────────────────────────────────────────────────────
def HtmlKatex(html: str) -> str:
    """Convert rendered KaTeX back to $...$ placeholders"""
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.select(".katex"):
        ann = katex.find("annotation", {"encoding": "application/x-tex"})
        if ann:
            katex.replace_with(f"${ann.text}$")
    return str(soup)

def render_html_with_playwright(page: Page, file_path: Path):
    """Inject KaTeX scripts, render, then extract only the body content."""
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
    wrapped  = header + original + "\n</body></html>"
    file_path.write_text(wrapped, encoding="utf-8")

    page.goto(f"file://{file_path.resolve()}", wait_until="networkidle")
    page.wait_for_load_state("networkidle")
    full     = page.content()
    soup     = BeautifulSoup(full, "html.parser")
    body     = soup.body.decode_contents()
    final    = f"""<!DOCTYPE html>
<html><head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
</head><body>
{body}
</body></html>"""
    file_path.write_text(final, encoding="utf-8")

def change_display_wrap(file_path: Path):
    """Wrap display-mode KaTeX spans in a centered div."""
    soup = BeautifulSoup(file_path.read_text(encoding="utf-8"), "html.parser")
    for span in soup.select("span.katex-display"):
        wrapper = soup.new_tag("div", style="text-align:center;")
        span.wrap(wrapper)
    file_path.write_text(str(soup), encoding="utf-8")

# ───────────────────────────────────────────────────────────
# 4) OpenAI translation
# ───────────────────────────────────────────────────────────
def ask_gpt(question: str, model: str, term: str) -> str:
    try:
        resp = openai.ChatCompletion.create(
            model=model,
            messages=[
                {"role":"system","content":
                    f"The HTML you will receive contains KaTeX formulas labeled as {term}.\n"
                    "Translate all Japanese text to English, preserve all HTML and KaTeX markup exactly.\n"
                    "Do NOT add or remove tags, return only the translated HTML."
                },
                {"role":"user", "content": question}
            ],
            temperature=0.0
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[GPT error] {e}, retrying...", file=sys.stderr)
        time.sleep(5)
        return ask_gpt(question, model, term)

def translate_html_for_lang(jp_html: str, term: str, target_lang: str) -> str:
    return ask_gpt(HtmlKatex(jp_html), GPT_MODEL, term)

# ───────────────────────────────────────────────────────────
# 5) Main loop
# ───────────────────────────────────────────────────────────
def main():
    contests = get_all_contests()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page: Page = browser.new_page()

        for contest in contests:
            print(contest)
            index_url = f"{BASE_URL}/contests/{contest}/editorial"
            resp = requests.get(index_url)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            ul   = soup.find("ul", id="editorials")
            if not ul:
                continue

            for a in ul.find_all("a", href=True):
                # skip official
                if "公式解説" in a.get_text(strip=True):
                    continue

                raw = a["href"]
                parsed = urlparse(raw)
                path = parsed.path
                # only user editorials: /contests/{contest}/editorial/{task}/{user}
                m = re.match(rf"^/contests/{re.escape(contest)}/editorial/(\d+)/(\d+)$", path)
                if not m:
                    continue
                task_id, user_id = m.groups()

                # --- save JP if missing ---
                ja_path = JA_ROOT / contest / "user_editorial" / f"{user_id}.html"
                ja_path.parent.mkdir(parents=True, exist_ok=True)
                if not ja_path.exists():
                    full_url = parsed.scheme and raw or BASE_URL + path
                    content = extract_div_innerhtml_with_playwright(page, full_url, div_id="editorial_content")
                    if content.strip():
                        ja_path.write_text(content, encoding="utf-8")
                        print(f"[Saved JP] {ja_path}")
                    else:
                        print(f"[Warning] no content at {full_url}", file=sys.stderr)
                        continue
                else:
                    print(f"[Skip JP] {ja_path}")

                # --- translate to EN if missing ---
                en_path = EN_ROOT / contest / "user_editorial" / f"{user_id}.html"
                en_path.parent.mkdir(parents=True, exist_ok=True)
                if not en_path.exists():
                    jp_html = ja_path.read_text(encoding="utf-8")
                    trans  = translate_html_for_lang(jp_html, term="user editorial", target_lang="en")
                    if not trans.strip():
                        print(f"[Warning] translation empty for {ja_path}", file=sys.stderr)
                        continue
                    en_path.write_text(trans, encoding="utf-8")
                    print(f"[Saved EN] {en_path}")

                    # render KaTeX and center display math
                    render_html_with_playwright(page, en_path)
                    change_display_wrap(en_path)
                    print(f"[Rendered & Wrapped] {en_path}")
                else:
                    print(f"[Skip EN] {en_path}")

        browser.close()

if __name__ == "__main__":
    main()
