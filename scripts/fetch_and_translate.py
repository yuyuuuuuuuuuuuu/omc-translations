# scripts/fetch_and_translate.py

import os
import sys
import time
import json
import requests
from bs4 import BeautifulSoup
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import openai
import tiktoken

# ───────────────────────────────────────────────────────────
# 1) ==================== 設定項目 =========================
# ───────────────────────────────────────────────────────────

# 1-1) OMC のベース URL
BASE_URL     = "https://onlinemathcontest.com"
HOMEPAGE_URL = BASE_URL + "/"

# 1-2) 言語設定ファイルのパス (この JSON から ["en", "fr", ...] を読み込む)
LANG_CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, "languages", "config.json")
)

# 1-3) 保存先ルート (languages/{lang}/contests/{contest_id}/tasks/{task_id}.html)
OUTPUT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, "languages")
)

# 1-4) OpenAI の API キーは環境変数から取得
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    print("[Error] 環境変数 OPENAI_API_KEY が設定されていません。")
    sys.exit(1)

# 旧バージョン用インターフェイス
openai.api_key = api_key

# 1-5) 使用モデル
GPT_MODEL = "gpt-4o-mini"


# ───────────────────────────────────────────────────────────
# 2) ==================== ヘルパー関数 ======================
# ───────────────────────────────────────────────────────────

def fetch_url_html(url: str) -> str:
    """
    requests を使って指定 URL を GET し、HTML を文字列で返す。
    失敗時は例外を投げる。
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/114.0.0.0 Safari/537.36"
        )
    })
    resp = session.get(url)
    resp.raise_for_status()
    return resp.text


def find_latest_ended_contest(html: str) -> str | None:
    """
    トップページ HTML を BeautifulSoup で解析し、
    最新の「終了済」コンテスト ID を返す。見つからなければ None。
    """
    soup = BeautifulSoup(html, "html.parser")

    # <div class="contest-header"> 内を走査
    for header_div in soup.select("div.contest-header"):
        status_div = header_div.find("div", class_="contest-status")
        if status_div and "終了済" in status_div.get_text(strip=True):
            # 見つかったら、その兄弟要素の中の <a class="contest-name">
            sib = header_div.find_next_sibling()
            while sib:
                if sib.name == "a" and "contest-name" in sib.get("class", []):
                    href = sib.get("href", "").strip()
                    if href:
                        # URL の末尾が contest_id
                        return href.rstrip("/").split("/")[-1]
                sib = sib.find_next_sibling()
    return None


def fetch_task_ids(contest_id: str) -> list[str]:
    """
    /contests/{contest_id} ページを取得し、
    <a href="/contests/{contest_id}/tasks/{task_id}"> をすべて抽出して
    task_id のリストを返す。空リストの場合もある。
    """
    url = f"{BASE_URL}/contests/{contest_id}"
    try:
        html = fetch_url_html(url)
    except Exception as e:
        print(f"[Error] {contest_id} ページ取得に失敗: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    ids: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        marker = f"/contests/{contest_id}/tasks/"
        if marker in href:
            parts = href.rstrip("/").split("/")
            if len(parts) >= 2 and parts[-2] == "tasks" and parts[-1].isdigit():
                ids.add(parts[-1])

    return sorted(ids, key=lambda x: int(x))


def extract_div_innerhtml_with_playwright(url: str, div_id: str, timeout: int = 8000) -> str:
    """
    Playwright を使って headless Chromium で url を開き、
    <div id="{div_id}"> が出現するまで待機して innerHTML を返す。
    失敗時は空文字を返す。
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle")
            try:
                page.wait_for_selector(f"#{div_id}", timeout=timeout)
            except PlaywrightTimeoutError:
                print(f"[Timeout] {url} に #{div_id} が現れませんでした。")
                browser.close()
                return ""
            content = page.eval_on_selector(f"#{div_id}", "el => el.innerHTML")
            browser.close()
            return content or ""
    except Exception as e:
        print(f"[Error] Playwright 例外: {url} -> {e}")
        return ""


def HtmlKatex(html: str) -> str:
    """
    BeautifulSoup で HTML をパースし、KaTeX の <span class="katex">…</span>
    から正規の LaTeX 形式 ($…$) に置き換えて返す。
    """
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.find_all(class_="katex"):
        tex = katex.find("annotation", {"encoding": "application/x-tex"})
        if tex:
            katex.replace_with(f"${tex.text}$")
    return str(soup)


def ask_gpt(question: str, model: str, term: str) -> str:
    """
    OpenAI API 旧インターフェイス(v0.28系)を使って翻訳を行う。
    """
    try:
        response = openai.ChatCompletion.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You will receive HTML-formatted KaTeX content for {term} written in Japanese. "
                        "Translate all text into the target language, preserving all KaTeX formatting (size, line breaks, classes). "
                        "Return only the translated content in valid HTML format; do not add extra tags or remove KaTeX structure.\n"
                        "Focus on keeping block formulas (class=\"katex-display\") intact. "
                        "If input is empty or none, return empty.\n"
                        "The content may be long; please complete the translation."
                    )
                },
                {"role": "user", "content": question}
            ],
            temperature=0.0
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[GPT error] {str(e)}")
        # リトライ間隔：エラー時は 10 秒待って再挑戦
        time.sleep(10)
        return ask_gpt(question, model, term)


def translate_html_for_lang(jp_html: str, term: str, target_lang: str) -> str:
    """
    日本語 HTML (KaTeX 部分含む) を GPT 経由で翻訳し、最終的に HTML 形式の文字列を返す。
    1) HtmlKatex により LaTeX 形式に変換 ($…$)
    2) ask_gpt で翻訳
    """
    latex_ready = HtmlKatex(jp_html)
    prompt = latex_ready
    translated = ask_gpt(prompt, GPT_MODEL, term)
    return translated


def load_languages_list() -> list[str]:
    """
    languages/config.json から翻訳対象言語（'en', 'fr', ...）を読み込み、返す。
    """
    if not os.path.exists(LANG_CONFIG_PATH):
        print(f"[Error] 言語設定ファイルが見つかりません: {LANG_CONFIG_PATH}")
        sys.exit(1)
    with open(LANG_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    langs = data.get("languages", [])
    if "en" not in langs:
        print("[Error] 言語リストに 'en' が含まれていません。")
        sys.exit(1)
    ordered = ["en"] + [l for l in langs if l != "en"]
    return ordered


def save_jp_problem(contest_id: str, task_id: str) -> str | None:
    """
    contest_id/task_id について <div id="problem_content"> の innerHTML を
    languages/ja/contests/{contest_id}/tasks/{task_id}.html に保存。
    すでにあればスキップ。成功したら保存先パスを返す。失敗時は None を返す。
    """
    folder = os.path.join(OUTPUT_ROOT, "ja", "contests", contest_id, "tasks")
    os.makedirs(folder, exist_ok=True)

    file_path = os.path.join(folder, f"{task_id}.html")
    if os.path.exists(file_path):
        print(f"[Skip JP] {contest_id}/tasks/{task_id}.html は既に存在します。")
        return file_path

    url = f"{BASE_URL}/contests/{contest_id}/tasks/{task_id}"
    print(f"[Fetch JP] {url} → extracting problem_content …")
    inner_html = extract_div_innerhtml_with_playwright(url, div_id="problem_content")

    if not inner_html.strip():
        print(f"[Warning] {contest_id}/tasks/{task_id} の problem_content が空、または取得失敗")
        return None

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(inner_html)
    print(f"[Saved JP] {file_path} (bytes={len(inner_html)})")
    return file_path


def save_translated_html(contest_id: str, task_id: str, lang: str, term: str, jp_filepath: str):
    """
    contest_id, task_id, target language (en/fr/...),
    term ("task" or "editorial"), jp_filepath (ja で保存済みファイルパス) を受けて、
    翻訳後 HTML を languages/{lang}/contests/{contest_id}/tasks/{task_id}.html に保存。
    すでにファイルがあればスキップ。
    """
    out_folder = os.path.join(OUTPUT_ROOT, lang, "contests", contest_id, "tasks")
    os.makedirs(out_folder, exist_ok=True)

    out_path = os.path.join(out_folder, f"{task_id}.html")
    if os.path.exists(out_path):
        print(f"[Skip {lang}] {contest_id}/tasks/{task_id}.html は既に存在します。")
        return

    with open(jp_filepath, "r", encoding="utf-8") as f:
        jp_html = f.read()

    print(f"[Translate → {lang}] contest={contest_id}, task={task_id}, term={term}")
    translated_html = translate_html_for_lang(jp_html, term, lang)

    if not translated_html.strip():
        print(f"[Error] {contest_id}/tasks/{task_id} の {lang} 翻訳が空。スキップ")
        return

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(translated_html)
    print(f"[Saved {lang}] {out_path} (bytes={len(translated_html)})")


# ───────────────────────────────────────────────────────────
# 3) ============== メイン処理ロジック ====================
# ───────────────────────────────────────────────────────────

def main():
    # 1) Top ページ取得 → 最新終了済コンテストを探す
    try:
        homepage_html = fetch_url_html(HOMEPAGE_URL)
    except Exception as e:
        print("[Error] トップページ取得に失敗:", e)
        sys.exit(1)

    latest_contest = find_latest_ended_contest(homepage_html)
    if latest_contest is None:
        print("最新の終了済コンテストが見つかりませんでした。")
        return

    print("最新の終了済コンテスト ID =", latest_contest)

    # 2) タスク ID 一覧を取得
    task_ids = fetch_task_ids(latest_contest)
    if not task_ids:
        print(f"[Warning] {latest_contest} にタスクが 1 つも見つかりません。終了。")
        return

    print(f"{latest_contest} のタスク一覧 = {task_ids}")

    # 3) 言語リストを読み込み（ex. ["en", "fr", "zh"]）
    languages = load_languages_list()
    print("翻訳対象の言語リスト =", languages)

    # 4) tasks の処理ループ (1タスクずつ「JP保存 → EN翻訳」)
    en_jp_map: dict[str, str] = {}  # task_id → JP filepath (for EN)
    for tid in task_ids:
        # (i) JP 保存
        jp_path = save_jp_problem(latest_contest, tid)
        if not jp_path:
            continue  # 取得失敗 or 既存ファイルなしならスキップ

        # (ii) EN 翻訳
        save_translated_html(latest_contest, tid, lang="en", term="task", jp_filepath=jp_path)

        # 以降の言語で使うためにパスを覚えておく
        en_jp_map[tid] = jp_path

    # 5) EN 翻訳完了後、残り言語の翻訳を開始
    remaining_langs = [l for l in languages if l != "en"]
    if remaining_langs:
        print("EN 翻訳完了後、残り言語の翻訳を開始します。順序 =", remaining_langs)

        for lang in remaining_langs:
            for tid, jp_path in en_jp_map.items():
                save_translated_html(latest_contest, tid, lang=lang, term="task", jp_filepath=jp_path)

    print("すべてのタスクの翻訳処理が完了しました。")


if __name__ == "__main__":
    main()
