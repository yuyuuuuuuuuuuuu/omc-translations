#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
from pathlib import Path
import requests
from bs4 import BeautifulSoup

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Browser
import openai

# ───────────────────────────────────────────────────────────
# 1) ================ 設定項目 ===============================
# ───────────────────────────────────────────────────────────

BASE_URL     = "https://onlinemathcontest.com"
HOMEPAGE_URL = BASE_URL + "/"

# OpenAI API キー取得
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("[Error] 環境変数 OPENAI_API_KEY が設定されていません。")
    sys.exit(1)
openai.api_key = OPENAI_KEY
GPT_MODEL = "gpt-4o-mini"

# OMC ログイン情報
OMC_USERNAME = os.getenv("OMC_USERNAME")
OMC_PASSWORD = os.getenv("OMC_PASSWORD")
if not (OMC_USERNAME and OMC_PASSWORD):
    print("[Error] 環境変数 OMC_USERNAME / OMC_PASSWORD が設定されていません。")
    sys.exit(1)

# 翻訳先の言語リスト設定などは省略。省略部分（load_languages_list など）は元と同じ。


# ───────────────────────────────────────────────────────────
# 2) ============ Playwright ログイン & Helper ============
# ───────────────────────────────────────────────────────────

def login_omc_with_playwright(page: Page) -> bool:
    login_url = BASE_URL + "/login"
    print(f"→ OMC login ページを開きます: {login_url}")
    page.goto(login_url, wait_until="networkidle")

    try:
        page.wait_for_selector("form[action='https://onlinemathcontest.com/login']", timeout=8000)
    except PlaywrightTimeoutError:
        print("[Error] ログインフォームが見つかりませんでした。")
        return False

    csrf_token = page.get_attribute("input[name='_token']", "value")
    if not csrf_token:
        print("[Error] CSRF トークンを取得できませんでした。")
        return False

    page.fill("input[name='display_name']", OMC_USERNAME)
    page.fill("input[name='password']", OMC_PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")

    if page.url.endswith("/login"):
        print("[Error] ログインに失敗しました。認証情報を確認してください。")
        return False

    print(f"→ ログイン成功 (redirect to: {page.url})")
    return True


# ───────────────────────────────────────────────────────────
# 3) ============= タスク ID 取得 (Playwright 経由) ===========
# ───────────────────────────────────────────────────────────

def fetch_task_ids(page: Page, contest_id: str) -> list[str]:
    """
    Playwright の page (ログイン済み) を使って、
    /contests/{contest_id} ページを開き、その中の
    '/contests/{contest_id}/tasks/{task_id}' リンクをすべて集める。
    """
    url = f"{BASE_URL}/contests/{contest_id}"
    try:
        page.goto(url, wait_until="networkidle")
        html = page.content()
    except Exception as e:
        print(f"[Error] Playwright で {contest_id} ページ取得失敗 → {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    ids: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        marker = f"/contests/{contest_id}/tasks/"
        if marker in href:
            parts = href.strip().rstrip("/").split("/")
            if len(parts) >= 2 and parts[-2] == "tasks" and parts[-1].isdigit():
                ids.add(parts[-1])
    return sorted(ids, key=lambda x: int(x))


# ───────────────────────────────────────────────────────────
# 4) =========== 日本語版を取得 → 翻訳 & 保存 ============
# ───────────────────────────────────────────────────────────

# save_jp_problem, save_translated_html, check_existence_problem などは
# もともとの実装と同じなので省略します。


# ───────────────────────────────────────────────────────────
# 5) ============ main() 関数 ================================
# ───────────────────────────────────────────────────────────

def main():
    # Playwright を起動してログインする
    with sync_playwright() as p:
        print("→ Headless Chromium を起動します")
        browser: Browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # 1) OMC にログイン
        success = login_omc_with_playwright(page)
        if not success:
            browser.close()
            sys.exit(1)

        # 2) トップページを取得して「開催中コンテスト ID」を抽出
        print("→ 開催中コンテストを探すためトップページを取得します")
        page.goto(HOMEPAGE_URL, wait_until="networkidle")
        home_html = page.content()
        current_contest = find_current_contest(home_html)
        if current_contest is None:
            print("[Info] 開催中コンテストが見つかりません。終了します。")
            browser.close()
            sys.exit(0)
        print(f"→ 開催中コンテスト ID = {current_contest}")

        # 3) Playwright 経由で「各タスク ID」を取得
        contest_page_url = f"{BASE_URL}/contests/{current_contest}"
        print(f"→ Playwright でコンテストページを開きます: {contest_page_url}")
        page.goto(contest_page_url, wait_until="networkidle")
        task_ids = fetch_task_ids(page, current_contest)
        if not task_ids:
            print(f"[Warning] コンテスト {current_contest} にタスクが見つかりません。終了します。")
            browser.close()
            sys.exit(0)
        print(f"→ {current_contest} のタスク一覧 = {task_ids}")

        # 4) 翻訳対象言語を読み込む (例: ["en", "fr", ...])
        languages = load_languages_list()
        print(f"→ 翻訳対象言語: {languages}")

        # 5) 各タスクについて、日本語を取得→英語翻訳→他言語翻訳
        en_jp_map: dict[str, Path] = {}
        for tid in task_ids:
            jp_path = save_jp_problem(current_contest, tid, page)
            if jp_path is None:
                continue

            # 英語翻訳
            if not check_existence_problem(current_contest, tid, lang="en"):
                save_translated_html(current_contest, tid, lang="en", term="task", jp_filepath=jp_path, page=page)
            else:
                print(f"[Skip EN] {current_contest}/{tid} はすでに英語翻訳済みです。")
            en_jp_map[tid] = jp_path

        # 6) 残りの言語を順に翻訳
        remaining_langs = [l for l in languages if l != "en"]
        if remaining_langs:
            print(f"→ 英語翻訳完了 → 他言語 ({remaining_langs}) を処理します")
            for lang in remaining_langs:
                for tid, jp_path in en_jp_map.items():
                    if not check_existence_problem(current_contest, tid, lang=lang):
                        save_translated_html(current_contest, tid, lang=lang, term="task", jp_filepath=jp_path, page=page)
                    else:
                        print(f"[Skip {lang}] {current_contest}/{tid} はすでに {lang} 翻訳済みです。")

        print("→ すべてのタスク翻訳処理が完了しました。")

        # 7) コンテストページから「Contest Duration (分)」をパース
        try:
            contest_url = f"{BASE_URL}/contests/{current_contest}"
            print(f"→ Contest ページから duration を取得: {contest_url}")
            # Playwright で HTML をもう一度取得してもいいし、requests で Fetch してもよい
            # （ログイン不要な部分は requests でも可なので、ここでは requests を使っても OK）
            html_contest = fetch_url_html(contest_url)
            soup2 = BeautifulSoup(html_contest, "html.parser")

            duration_min = None
            for p_tag in soup2.find_all("p", class_="list-group-item-heading"):
                text = p_tag.get_text(strip=True)
                if text.endswith("分"):
                    sibling = p_tag.find_next_sibling("p", class_="list-group-item-text")
                    if sibling and "Contest Duration" in sibling.get_text():
                        digits = "".join(filter(str.isdigit, text))
                        if digits.isdigit():
                            duration_min = int(digits)
                            break

            if duration_min is None:
                print("[Warning] duration_min をパースできませんでした。デフォルトで 60 分とします。")
                duration_min = 60
            else:
                print(f"→ 取得した Contest Duration = {duration_min} 分")

        except Exception as e:
            print(f"[Warning] duration_min の取得中に例外発生: {e}")
            duration_min = 60

        # 8) contest_id と duration_min を JSON で出力 (ワークフローでセットできるように)
        result = {
            "contest_id": current_contest,
            "duration_min": duration_min
        }
        print(json.dumps(result, ensure_ascii=False))

        browser.close()


if __name__ == "__main__":
    main()