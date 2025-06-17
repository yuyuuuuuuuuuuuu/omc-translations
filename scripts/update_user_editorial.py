# scripts/update_user_editorials.py
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

import openai
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Browser
import argparse

BASE_URL  = "https://onlinemathcontest.com"
THIS_DIR  = Path(__file__).parent
JA_ROOT   = THIS_DIR.parent / "languages" / "ja" / "contests"
EN_ROOT   = THIS_DIR.parent / "languages" / "en" / "contests"

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("[Error] OPENAI_API_KEY が設定されていません。", file=sys.stderr)
    sys.exit(1)
openai.api_key = OPENAI_KEY
GPT_MODEL = "gpt-4o-mini"

def get_all_contests() -> list[str]:
    # 全コンテスト取得ロジック省略…（既存のまま）
    out = []
    page = 1
    while True:
        url = f"{BASE_URL}/contests/all?page={page}"
        r = requests.get(url); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        tbl = soup.find("div", class_="table-responsive")
        if not tbl:
            break
        links = tbl.find_all("a", href=True)
        names = [a["href"].split("/contests/")[1].strip() for a in links if "/contests/" in a["href"]]
        if not names:
            break
        out.extend(names)
        page += 1
    return out

# （既存の extract_div_innerhtml_with_playwright, HtmlKatex, render_html_with_playwright, ask_gpt, translate_html_for_lang が続く）

def save_user_editorials_for_contest(contest: str, page: Page):
    """
    contest の「ユーザー解説」を全て取得 → 英語翻訳 → 保存 → KaTeXレンダリング → git push
    """
    # (1) editorials index ページを取得
    ul_url = f"{BASE_URL}/contests/{contest}/editorial"
    resp = requests.get(ul_url); resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    ul = soup.find("ul", id="editorials")
    if not ul:
        print(f"[Info] contest={contest} のユーザー解説一覧なし")
        return

    # (2) 各ユーザー解説リンクをたどる
    links = []
    for a in ul.find_all("a", href=True):
        text = a.get_text(strip=True)
        # 「ユーザー解説」だけ
        if "公式解説" in text:
            continue
        href = a["href"]
        # /contests/{contest}/editorial/{task_id}/{user_id}
        m = re.match(rf"^/contests/{contest}/editorial/\d+/(\d+)$", href)
        if m:
            user_id = m.group(1)
            links.append((href, user_id))

    for href, user_id in links:
        full_url = href.startswith("http") and href or BASE_URL + href
        # 保存先
        ja_path = JA_ROOT / contest / "user_editorial" / f"{user_id}.html"
        ja_path.parent.mkdir(parents=True, exist_ok=True)

        # (A) JP 保存（なければ）
        if not ja_path.exists():
            content = extract_div_innerhtml_with_playwright(page, full_url, "editorial_content")
            if content.strip():
                ja_path.write_text(content, encoding="utf-8")
                print(f"[Saved JP USER] {ja_path}")
            else:
                print(f"[Warning] {full_url} のユーザー解説取得失敗")
                continue
        else:
            print(f"[Skip JP USER] {ja_path}")

        # (B) 英語翻訳
        en_path = EN_ROOT / contest / "user_editorial" / f"{user_id}.html"
        en_path.parent.mkdir(parents=True, exist_ok=True)
        if not en_path.exists():
            jp_html = ja_path.read_text(encoding="utf-8")
            translated = translate_html_for_lang(jp_html, term="user editorial", target_lang="en")
            if not translated.strip():
                print(f"[Warning] translation empty for {ja_path}")
                continue
            en_path.write_text(translated, encoding="utf-8")
            print(f"[Saved EN USER] {en_path}")
            # KaTeXレンダリング
            render_html_with_playwright(page, en_path)
            # push
            subprocess.run(["git","config","--local","user.name","github-actions[bot]"], check=True)
            subprocess.run(["git","config","--local","user.email","github-actions[bot]@users.noreply.github.com"], check=True)
            subprocess.run(["git","add", str(en_path)], check=True)
            subprocess.run(["git","commit","-m", f"Add user_editorial {contest}/{user_id}"], check=True)
            subprocess.run(["git","push","origin","HEAD:main"], check=True)
        else:
            print(f"[Skip EN USER] {en_path}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contest", help="対象Contest IDのみ更新（省略時は全コンテスト）")
    args = parser.parse_args()

    # Playwright 準備
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=True)
        page: Page = browser.new_context().new_page()

        if args.contest:
            save_user_editorials_for_contest(args.contest, page)
        else:
            # 全コンテストを対象
            for contest in get_all_contests():
                save_user_editorials_for_contest(contest, page)

        browser.close()

if __name__ == "__main__":
    main()
