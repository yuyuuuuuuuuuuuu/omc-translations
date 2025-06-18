#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import argparse
import requests
import subprocess
from pathlib import Path
from bs4 import BeautifulSoup

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Browser
import openai

# ───────────────────────────────────────────────────────────
# 1) 設定項目
# ───────────────────────────────────────────────────────────

BASE_URL         = "https://onlinemathcontest.com"
HOMEPAGE_URL     = BASE_URL + "/"
LANG_CONFIG_PATH = Path(__file__).parents[1] / "languages" / "config.json"
OUTPUT_ROOT      = Path(__file__).parents[1] / "languages"

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("[Error] 環境変数 OPENAI_API_KEY が設定されていません。")
    sys.exit(1)
openai.api_key = OPENAI_KEY
GPT_MODEL = "gpt-4o-mini"

# ───────────────────────────────────────────────────────────
# 2) Git コミット＆プッシュ用ヘルパー
# ───────────────────────────────────────────────────────────

def git_add_and_push(file_path: Path):
    try:
        subprocess.run(["git", "config", "--local", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "--local", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", str(file_path)], check=True)
        subprocess.run(["git", "commit", "-m", f"Add {file_path}"], check=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
        print(f"[Git] {file_path} を commit & push")
    except subprocess.CalledProcessError as e:
        print(f"[Error] Git 操作中に例外発生: {e}")

# ───────────────────────────────────────────────────────────
# 3) 共通ヘルパー関数
# ───────────────────────────────────────────────────────────

def fetch_url_html(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent":"Mozilla/5.0"})
    resp.raise_for_status()
    return resp.text

def find_latest_ended_contest(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for header_div in soup.select("div.contest-header"):
        status_div = header_div.find("div", class_="contest-status")
        if status_div and "終了済" in status_div.get_text(strip=True):
            sib = header_div.find_next_sibling()
            while sib:
                if sib.name=="a" and "contest-name" in sib.get("class", []):
                    return sib["href"].rstrip("/").split("/")[-1]
                sib = sib.find_next_sibling()
    return None

def fetch_task_ids(contest_id: str) -> list[str]:
    url = f"{BASE_URL}/contests/{contest_id}"
    try:
        html = fetch_url_html(url)
    except Exception as e:
        print(f"[Error] {contest_id} ページ取得失敗 → {e}")
        return []
    soup = BeautifulSoup(html, "html.parser")
    ids = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        marker = f"/contests/{contest_id}/tasks/"
        if marker in href:
            parts = href.strip().rstrip("/").split("/")
            if len(parts)>=2 and parts[-2]=="tasks" and parts[-1].isdigit():
                ids.add(parts[-1])
    return sorted(ids, key=lambda x:int(x))

def fetch_editorial_html_with_playwright(page: Page, contest_id: str, task_id: str) -> str:
    url = f"{BASE_URL}/contests/{contest_id}/editorial/{task_id}"
    try:
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector("#editorial_content", timeout=8000)
        return page.eval_on_selector("#editorial_content", "el => el.innerHTML") or ""
    except PlaywrightTimeoutError:
        print(f"[Timeout] {url} に #editorial_content が現れませんでした。")
        return ""
    except Exception as e:
        print(f"[Error] Playwright 例外: {e}")
        return ""

def HtmlKatex(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.find_all(class_="katex"):
        tex = katex.find("annotation", {"encoding":"application/x-tex"})
        if tex:
            katex.replace_with(f"${tex.text}$")
    return str(soup)

def ask_gpt(question: str, model: str, term: str) -> str:
    resp = openai.ChatCompletion.create(
        model=model,
        messages=[
            {"role":"system","content":
                f"The text you are about to receive is HTML-formatted KaTeX math {term} written in Japanese.\n"
                "Please translate all sentences into the target language (e.g. English), preserving all KaTeX formatting\n"
                "(font size, line breaks, class=\"katex-display\" for display formulas, etc.).\n"
                "Return ONLY the translated HTML; do not wrap it in extra tags, do not alter KaTeX markup.\n"
                "If input is empty or none, return empty.\n"
            },
            {"role":"user","content":question}
        ],
        temperature=0.0
    )
    return resp.choices[0].message.content.strip()

def translate_html_for_lang(jp_html: str, term: str, target_lang: str) -> str:
    return ask_gpt(HtmlKatex(jp_html), GPT_MODEL, term)

def render_html_with_playwright(page: Page, file_path: Path):
    new_header = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.js"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/contrib/auto-render.min.js"
          onload="renderMathInElement(document.body,{delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}]});"></script>
  <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
</head>
<body>
"""
    orig = file_path.read_text(encoding="utf-8")
    file_path.write_text(new_header + orig + "\n</body></html>", encoding="utf-8")
    page.goto("file://" + str(file_path.resolve()), wait_until="networkidle")
    page.wait_for_load_state("networkidle")
    full = BeautifulSoup(page.content(), "html.parser")
    body = full.body.decode_contents()
    final = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
</head>
<body>
{body}
</body>
</html>"""
    file_path.write_text(final, encoding="utf-8")
    print(f"[Rendered KaTeX] {file_path}")

def change_editorial_display(contest_id: str, task_id: str, lang: str = "en"):
    file_path = OUTPUT_ROOT / lang / "contests" / contest_id / "editorial" / f"{task_id}.html"
    if not file_path.exists():
        print(f"[Error] {file_path} が存在しません。")
        return
    soup = BeautifulSoup(file_path.read_text(encoding="utf-8"), "html.parser")
    for elt in soup.find_all("span", class_="katex-display"):
        wrapper = soup.new_tag("div", style="text-align:center;")
        elt.wrap(wrapper)
    file_path.write_text(str(soup), encoding="utf-8")
    print(f"[Wrapped display] {file_path}")

def save_jp_editorial(contest_id: str, task_id: str, page: Page) -> Path | None:
    jp_folder = OUTPUT_ROOT / "ja" / "contests" / contest_id / "editorial"
    jp_folder.mkdir(parents=True, exist_ok=True)
    jp_path = jp_folder / f"{task_id}.html"
    if jp_path.exists():
        print(f"[Skip JP] {contest_id}/{task_id} の解説はすでに存在します。")
        return jp_path
    inner_html = fetch_editorial_html_with_playwright(page, contest_id, task_id)
    if not inner_html.strip():
        print(f"[Warning] {contest_id}/{task_id} の解説コンテンツ取得失敗。")
        return None
    jp_path.write_text(inner_html, encoding="utf-8")
    print(f"[Saved JP] {jp_path} (bytes={len(inner_html)})")
    return jp_path

def translate_editorials_for_contest(contest_id: str, page: Page):
    task_ids = fetch_task_ids(contest_id)
    if not task_ids:
        print(f"[Info] {contest_id} にタスクなし。")
        return
    jp_map = {}
    for tid in task_ids:
        path = save_jp_editorial(contest_id, tid, page)
        if path:
            jp_map[tid] = path

    langs = json.loads(LANG_CONFIG_PATH.read_text(encoding="utf-8")).get("languages", [])
    ordered = ["en"] + [l for l in langs if l!="en"]

    for tid, jp_path in jp_map.items():
        for term, lang in [("editorial", l) for l in ordered]:
            out_folder = OUTPUT_ROOT / lang / "contests" / contest_id / "editorial"
            out_folder.mkdir(parents=True, exist_ok=True)
            out_path = out_folder / f"{tid}.html"
            if out_path.exists():
                print(f"[Skip {lang}] {contest_id}/{tid} は既に翻訳済み。")
                continue
            print(f"[Translate → {lang}] contest={contest_id}, editorial={tid}")
            translated = translate_html_for_lang(jp_path.read_text(encoding="utf-8"), term, lang)
            if not translated.strip():
                print(f"[Error] {contest_id}/{tid} の {lang} 翻訳結果が空。")
                continue
            out_path.write_text(translated, encoding="utf-8")
            print(f"[Saved {lang}] {out_path}")
            render_html_with_playwright(page, out_path)
            change_editorial_display(contest_id, tid, lang)
            git_add_and_push(out_path)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contest", help="対象の Contest ID（省略時は最新の終了済）")
    args = parser.parse_args()

    if args.contest:
        contest = args.contest
    else:
        try:
            home_html = fetch_url_html(HOMEPAGE_URL)
        except Exception as e:
            print(f"[Error] トップページ取得失敗 → {e}")
            sys.exit(1)
        contest = find_latest_ended_contest(home_html)
        if not contest:
            print("最新の終了済コンテストが見つかりません。")
            return

    print(f"最新／指定の終了済コンテスト ID = {contest}")

    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=True)
        page: Page = browser.new_context().new_page()
        translate_editorials_for_contest(contest, page)
        browser.close()

if __name__ == "__main__":
    main()