#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import time
import json
import requests
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Browser
import openai
import argparse

# ───────────────────────────────────────────────────────────
# 設定
# ───────────────────────────────────────────────────────────

BASE_URL = "https://onlinemathcontest.com"
THIS_DIR = Path(__file__).parent
JA_ROOT  = THIS_DIR.parent / "languages" / "ja" / "contests"
EN_ROOT  = THIS_DIR.parent / "languages" / "en" / "contests"

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("[Error] 環境変数 OPENAI_API_KEY が設定されていません。", file=sys.stderr)
    sys.exit(1)
openai.api_key = OPENAI_KEY

GPT_MODEL = "gpt-4o-mini"

# ───────────────────────────────────────────────────────────
# ヘルパー関数
# ───────────────────────────────────────────────────────────

def get_all_contests() -> list[str]:
    """全コンテスト一覧を取得"""
    out = []
    page = 1
    while True:
        url = f"{BASE_URL}/contests/all?page={page}"
        resp = requests.get(url); resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        tbl = soup.find("div", class_="table-responsive")
        if not tbl:
            break
        names = []
        for a in tbl.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/contests/"):
                name = href.split("/contests/")[1].strip().rstrip("/")
                if name:
                    names.append(name)
        if not names:
            break
        out.extend(names)
        page += 1
    return out

def extract_div_innerhtml_with_playwright(page: Page, url: str, div_id: str, timeout: int = 8000) -> str:
    """Playwright でページを開き、指定<div>の innerHTML を取得"""
    try:
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector(f"#{div_id}", timeout=timeout)
        return page.eval_on_selector(f"#{div_id}", "el => el.innerHTML") or ""
    except PlaywrightTimeoutError:
        print(f"[Timeout] {url} に #{div_id} が現れませんでした。", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"[Error] Playwright 例外: {url} -> {e}", file=sys.stderr)
        return ""

def HtmlKatex(html: str) -> str:
    """KaTeX 数式タグを $...$ 形式に戻す"""
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.select(".katex"):
        ann = katex.find("annotation", {"encoding":"application/x-tex"})
        if ann:
            katex.replace_with(f"${ann.text}$")
    return str(soup)

def ask_gpt(question: str, model: str, term: str) -> str:
    """OpenAI に HTML+KaTeX を壊さず翻訳させる"""
    try:
        resp = openai.ChatCompletion.create(
            model=model,
            messages=[
                {"role":"system","content":
                    f"The text you will receive is HTML-formatted KaTeX math {term} written in Japanese.\n"
                    "Translate all sentences into English, preserving all HTML and KaTeX markup.\n"
                    "Return only the translated HTML."},
                {"role":"user","content":question}
            ],
            temperature=0.0
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[GPT error] {e}, retryします...", file=sys.stderr)
        time.sleep(5)
        return ask_gpt(question, model, term)

def translate_html_for_lang(jp_html: str, term: str, target_lang: str) -> str:
    """日本語 HTML を target_lang に翻訳"""
    return ask_gpt(HtmlKatex(jp_html), GPT_MODEL, term)

def render_html_with_playwright(page: Page, file_path: Path):
    """KaTeX をレンダリングして最終 HTML を上書き"""
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
    content = file_path.read_text(encoding="utf-8")
    wrapped = header + content + "\n</body></html>"
    file_path.write_text(wrapped, encoding="utf-8")
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

# ───────────────────────────────────────────────────────────
# メイン：ユーザー解説取得＆翻訳
# ───────────────────────────────────────────────────────────

def save_user_editorials_for_contest(contest: str, page: Page):
    """指定コンテストのユーザー解説をすべて取得・翻訳・保存"""
    index_url = f"{BASE_URL}/contests/{contest}/editorial"
    resp = requests.get(index_url)
    if resp.status_code != 200:
        print(f"[Info] {contest} の解説一覧ページ取得失敗: {resp.status_code}")
        return
    soup = BeautifulSoup(resp.text, "html.parser")
    ul = soup.find("ul", id="editorials")
    if not ul:
        print(f"[Info] contest={contest} にユーザー解説なし")
        return

    links = []
    for a in ul.find_all("a", href=True):
        text = a.get_text(strip=True)
        if "公式解説" in text:
            continue
        m = re.match(rf"^/contests/{contest}/editorial/\d+/(\d+)$", a["href"])
        if m:
            links.append((a["href"], m.group(1)))

    for href, user_id in links:
        full_url = href if href.startswith("http") else BASE_URL + href

        # 日本語保存
        ja_path = JA_ROOT / contest / "user_editorial" / f"{user_id}.html"
        ja_path.parent.mkdir(parents=True, exist_ok=True)
        if not ja_path.exists():
            html = extract_div_innerhtml_with_playwright(page, full_url, "editorial_content")
            if html.strip():
                ja_path.write_text(html, encoding="utf-8")
                print(f"[Saved JP USER] {ja_path}")
            else:
                print(f"[Warning] {full_url} から取得できず")
                continue
        else:
            print(f"[Skip JP USER] {ja_path}")

        # 英語翻訳
        en_path = EN_ROOT / contest / "user_editorial" / f"{user_id}.html"
        en_path.parent.mkdir(parents=True, exist_ok=True)
        if not en_path.exists():
            jp_html = ja_path.read_text(encoding="utf-8")
            translated = translate_html_for_lang(jp_html, "user editorial", "en")
            if not translated.strip():
                print(f"[Warning] 翻訳が空: {ja_path}")
                continue
            en_path.write_text(translated, encoding="utf-8")
            print(f"[Saved EN USER] {en_path}")
            render_html_with_playwright(page, en_path)
            # git push
            subprocess.run(["git","config","--local","user.name","github-actions[bot]"], check=True)
            subprocess.run(["git","config","--local","user.email","github-actions[bot]@users.noreply.github.com"], check=True)
            subprocess.run(["git","add", str(en_path)], check=True)
            subprocess.run(["git","commit","-m", f"Add user_editorial {contest}/{user_id}"], check=True)
            subprocess.run(["git","push","origin","HEAD:main"], check=True)
        else:
            print(f"[Skip EN USER] {en_path}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contest", help="対象の Contest ID を指定（省略時は全コンテスト）")
    args = parser.parse_args()

    # Playwright セッション
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=True)
        page: Page = browser.new_context().new_page()

        if args.contest:
            save_user_editorials_for_contest(args.contest, page)
        else:
            for c in get_all_contests():
                save_user_editorials_for_contest(c, page)

        browser.close()

if __name__ == "__main__":
    main()
