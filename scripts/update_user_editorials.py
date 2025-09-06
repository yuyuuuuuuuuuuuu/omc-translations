#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import time
import json
import argparse
import subprocess
from pathlib import Path
from typing import List, Tuple

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Page, Browser
)
import openai

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

# ユーザー解説ページの本文候補セレクタ（順に試す）
EDITORIAL_SELECTORS: List[str] = [
    "#editorial_content",
    "#editorial-content",
    ".editorial-content",
    "article",
    "main article",
    "main .card-body",
    "#content",
    "div.container article",
    "div.container #content",
]

# ───────────────────────────────────────────────────────────
# ヘルパー関数
# ───────────────────────────────────────────────────────────

def get_all_contests() -> List[str]:
    """全コンテスト一覧を取得"""
    out: List[str] = []
    page = 1
    while True:
        url = f"{BASE_URL}/contests/all?page={page}"
        resp = requests.get(url)
        if resp.status_code != 200:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        tbl = soup.find("div", class_="table-responsive")
        if not tbl:
            break
        names = []
        for a in tbl.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/contests/"):
                name = href.split("/contests/")[1].strip().rstrip("/")
                if name and name not in names:
                    names.append(name)
        if not names:
            break
        out.extend(names)
        page += 1
    return out

def fetch_text(url: str, timeout: int = 30) -> str:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text

def list_user_editorials_in_contest(contest: str) -> List[Tuple[int, int]]:
    """
    /contests/{contest}/editorial から
    /contests/{contest}/editorial/{task_id}/{user_id} を全て抽出。
    """
    index_url = f"{BASE_URL}/contests/{contest}/editorial"
    try:
        html = fetch_text(index_url)
    except Exception as e:
        print(f"[Info] {contest} の解説一覧ページ取得失敗: {e}")
        return []
    # 公式解説は /editorial/{task_id}（数値が1個）
    # ユーザー解説は /editorial/{task_id}/{user_id}（数値が2個）
    pat = re.compile(rf"/contests/{re.escape(contest)}/editorial/(\d+)/(\d+)")
    pairs = sorted(set(pat.findall(html)))
    out: List[Tuple[int, int]] = [(int(t), int(u)) for (t, u) in pairs]
    return out

def extract_content_with_playwright(page: Page, url: str) -> str:
    """
    Playwright でページを開き、本文をセレクタ候補で順に抽出。
    候補が全て失敗したら body 全体を返す。
    """
    try:
        page.goto(url, wait_until="networkidle")
    except PlaywrightTimeoutError:
        print(f"[Timeout] goto: {url}", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"[Error] goto 例外: {url} -> {e}", file=sys.stderr)
        return ""

    for sel in EDITORIAL_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=6000)
            html = page.eval_on_selector(sel, "el => el.innerHTML") or ""
            if html.strip():
                return html
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue

    try:
        # 最後の手段：body 全体
        page.wait_for_selector("body", timeout=3000)
        return page.eval_on_selector("body", "el => el.innerHTML") or ""
    except Exception:
        return ""

def HtmlKatex(html: str) -> str:
    """KaTeX の <annotation encoding='application/x-tex'> を $...$ に戻す"""
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.select(".katex"):
        ann = katex.find("annotation", {"encoding": "application/x-tex"})
        if ann:
            katex.replace_with(f"${ann.text}$")
    return str(soup)

def ask_gpt(question: str, model: str, term: str) -> str:
    """OpenAI に HTML+KaTeX を壊さず翻訳させる（簡易リトライ付き）"""
    for i in range(5):
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
            print(f"[GPT error] {e} (retry {i+1}/5)", file=sys.stderr)
            time.sleep(2*(i+1))
    return ""

def translate_html_for_lang(jp_html: str, term: str, target_lang: str) -> str:
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

def git_add_and_push(paths: List[Path], message: str):
    subprocess.run(["git","config","--local","user.name","github-actions[bot]"], check=True)
    subprocess.run(["git","config","--local","user.email","github-actions[bot]@users.noreply.github.com"], check=True)
    subprocess.run(["git","add","-A"], check=True)
    # 差分なしなら何もしない
    if subprocess.run(["git","diff","--cached","--quiet"]).returncode == 0:
        return
    subprocess.run(["git","commit","-m", message], check=True)
    subprocess.run(["git","pull","--rebase"], check=True)
    subprocess.run(["git","push","origin","HEAD:main"], check=True)

# ───────────────────────────────────────────────────────────
# メイン：ユーザー解説取得＆翻訳
# ───────────────────────────────────────────────────────────

def save_user_editorials_for_contest(contest: str, page: Page, limit: int | None = None, dry_run: bool = False):
    """指定コンテストのユーザー解説をすべて取得・翻訳・保存"""
    pairs = list_user_editorials_in_contest(contest)  # [(task_id, user_id), ...]
    print(f"[UserEditorial] contest={contest} found={len(pairs)} links")
    if limit:
        pairs = pairs[:limit]

    committed: List[Path] = []

    for task_id, user_id in pairs:
        url = f"{BASE_URL}/contests/{contest}/editorial/{task_id}/{user_id}"
        print(f"[UserEditorial] target: {contest} task={task_id} user={user_id}")

        # 保存パス（task_id/user_id.html）に変更：衝突防止
        ja_path = JA_ROOT / contest / "user_editorial" / str(task_id) / f"{user_id}.html"
        en_path = EN_ROOT / contest / "user_editorial" / str(task_id) / f"{user_id}.html"
        ja_path.parent.mkdir(parents=True, exist_ok=True)
        en_path.parent.mkdir(parents=True, exist_ok=True)

        if dry_run:
            print(f"[DryRun] would fetch -> {url}")
            print(f"[DryRun] would save -> {ja_path} / {en_path}")
            continue

        # 日本語保存
        if not ja_path.exists():
            html = extract_content_with_playwright(page, url)
            if html.strip():
                ja_path.write_text(html, encoding="utf-8")
                print(f"[Saved JP USER] {ja_path}")
            else:
                print(f"[Warning] 取得できず: {url}")
                continue
        else:
            print(f"[Skip JP USER] {ja_path}")

        # 英語翻訳
        if not en_path.exists():
            jp_html = ja_path.read_text(encoding="utf-8")
            translated = translate_html_for_lang(jp_html, "user editorial", "en")
            if not translated.strip():
                print(f"[Warning] 翻訳が空: {ja_path}")
                continue
            en_path.write_text(translated, encoding="utf-8")
            print(f"[Saved EN USER] {en_path}")

            # KaTeX レンダリング
            try:
                render_html_with_playwright(page, en_path)
            except Exception as e:
                print(f"[Warn] KaTeX render skipped: {e}")

            committed.append(en_path)
            # 1件ずつ push でも良いが、まとめての方が速い
            if len(committed) >= 20:
                git_add_and_push(committed, f"Add user editorials for {contest} (batch)")
                committed.clear()
        else:
            print(f"[Skip EN USER] {en_path}")

    # 端数 push
    if committed:
        git_add_and_push(committed, f"Add user editorials for {contest}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contest", help="対象の Contest ID を指定（省略時は全コンテスト）")
    parser.add_argument("--limit", type=int, default=None, help="処理数の上限（デバッグ用）")
    parser.add_argument("--dry-run", action="store_true", help="取得のみで保存・翻訳・push しない")
    args = parser.parse_args()

    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=True)
        page: Page = browser.new_context().new_page()

        if args.contest:
            save_user_editorials_for_contest(args.contest, page, limit=args.limit, dry_run=args.dry_run)
        else:
            for c in get_all_contests():
                save_user_editorials_for_contest(c, page, limit=args.limit, dry_run=args.dry_run)

        browser.close()

if __name__ == "__main__":
    main()
