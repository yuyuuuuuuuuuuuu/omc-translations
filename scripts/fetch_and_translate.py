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
# 1) 設定・定数
# ───────────────────────────────────────────────────────────

# 1-1) OMC の基本 URL
BASE_URL = "https://onlinemathcontest.com"
ALL_CONTESTS_URL = BASE_URL + "/contests/all"
HOMEPAGE_URL = BASE_URL + "/"

# 1-2) フォルダ構成のルート（この下に jp/ 以下と en/ 以下がある想定）
ROOT_LANG_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir, "languages"))

# 1-3) 翻訳対象言語の設定ファイルパス
LANG_CONFIG_PATH = os.path.join(ROOT_LANG_DIR, "config.json")

# 1-4) OpenAI API キー（環境変数から取得）
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    print("[Error] 環境変数 OPENAI_API_KEY が設定されていません。")
    sys.exit(1)
openai.api_key = OPENAI_API_KEY

# 1-5) 使用する GPT モデル
GPT_MODEL = "gpt-4o-mini"

# 1-6) KaTeX レンダリングに使用する CSS/JS のバージョン
KATEX_CSS = "https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css"
KATEX_JS = "https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.js"
KATEX_AUTORENDER_JS = "https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/contrib/auto-render.min.js"

# 1-7) User-Agent ヘッダ
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
)


# ───────────────────────────────────────────────────────────
# 2) ヘルパー関数群
# ───────────────────────────────────────────────────────────

def fetch_url_html(url: str, timeout: int = 15) -> str:
    """
    requests で URL に GET リクエストを送り、HTML レスポンスを返す。
    失敗したら例外を投げる。
    """
    try:
        resp = requests.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"[Error] {url} の取得に失敗: {e}")
        raise


def find_latest_ended_contest(html: str) -> str | None:
    """
    トップページ HTML を解析し、「終了済」と書いてある最新のコンテスト ID を返す。
    見つからなければ None を返す。
    """
    soup = BeautifulSoup(html, "html.parser")

    # 「div.contest-header」 要素をすべてスキャンし、
    # その直下に <div class="contest-status">終了済</div> があるものを見つける
    for header_div in soup.select("div.contest-header"):
        status_div = header_div.find("div", class_="contest-status")
        if status_div and "終了済" in status_div.get_text(strip=True):
            # 兄弟要素の中に <a class="contest-name" href="/contests/{ID}"> があるはず
            sib = header_div.find_next_sibling()
            while sib:
                if sib.name == "a" and "contest-name" in sib.get("class", []):
                    href = sib.get("href", "").strip()
                    if href:
                        return href.rstrip("/").split("/")[-1]
                sib = sib.find_next_sibling()
    return None


def fetch_task_ids(contest_id: str) -> list[str]:
    """
    https://onlinemathcontest.com/contests/{contest_id} のページを取得し、
    タスク一覧（/tasks/{task_id} のリンク）を拾って返す。
    """
    url = f"{BASE_URL}/contests/{contest_id}"
    try:
        html = fetch_url_html(url)
    except Exception:
        return []

    soup = BeautifulSoup(html, "html.parser")
    task_ids = set()

    # <a href="/contests/{contest_id}/tasks/{task_id}"> 含むものを探す
    for a in soup.find_all("a", href=True):
        href = a["href"]
        marker = f"/contests/{contest_id}/tasks/"
        if marker in href:
            parts = href.rstrip("/").split("/")
            if len(parts) >= 2 and parts[-2] == "tasks" and parts[-1].isdigit():
                task_ids.add(parts[-1])

    return sorted(task_ids, key=lambda x: int(x))


def extract_div_innerhtml_with_playwright(url: str, div_id: str, timeout: int = 8000) -> str:
    """
    Playwright を使って headless Chromium で指定 URL を開き、
    <div id="{div_id}"> の innerHTML を文字列で返す。
    存在しない、もしくはタイムアウトした場合は空文字列を返す。
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
            inner_html = page.eval_on_selector(f"#{div_id}", "el => el.innerHTML")
            browser.close()
            return inner_html or ""
    except Exception as e:
        print(f"[Error] Playwright 例外: {url} → {e}")
        return ""


def HtmlKatex(html: str) -> str:
    """
    BeautifulSoup を使って渡された HTML をパースし、
    KaTeX の <span class="katex">…</span> を LaTeX 形式 ($…$) に置き換えて返す。
    (ChatGPT に渡すプロンプト用)
    """
    soup = BeautifulSoup(html, "html.parser")
    for katex_span in soup.find_all(class_="katex"):
        annotation = katex_span.find("annotation", {"encoding": "application/x-tex"})
        if annotation:
            latex = annotation.text.strip()
            katex_span.replace_with(f"${latex}$")
    return str(soup)


def ask_gpt_translated_html(jp_html: str, term: str, target_lang: str) -> str:
    """
    OpenAI API (v1.0.0 以降) を使って、HTML 形式の日本語文章 (KaTeX 部分あり) を
    target_lang(例: "en", "fr") に翻訳した HTML 文字列を返す。
    失敗したら空文字列を返すかリトライする。
    """
    # まず、HTML の中にある KaTeX を LaTeX ($…$) に変換
    prompt_html = HtmlKatex(jp_html)

    system_prompt = (
        f"You will receive HTML-formatted KaTeX content for {term} written in Japanese. "
        f"Translate all visible Japanese text into the target language ({target_lang}), preserving all KaTeX markup exactly as-is (including class names, line breaks, display math). "
        "Return ONLY the valid HTML that includes the translated text. Do NOT add extra tags, do not remove KaTeX structure.\n"
        "If input is empty or none, return empty."
    )

    try:
        response = openai.ChatCompletion.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": prompt_html}
            ],
            temperature=0.0
        )
        translated = response.choices[0].message.content.strip()
        return translated
    except Exception as e:
        print(f"[GPT error] {e}")
        time.sleep(10)
        return ask_gpt_translated_html(jp_html, term, target_lang)


def render_to_final_html(file_path: str):
    """
    1) 既存の file_path (翻訳後の HTML) に KaTeX の CSS/JS を読み込む <head> を一度付与し、
    2) Playwright で該当ファイルを開き JS-render 後の HTML を取得
    3) <body> 内のみを抜き出して「最終 HTML」として file_path を上書き保存
    """
    # 3-1) 最低限の <head> テンプレートを作る
    header_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="{KATEX_CSS}">
  <script defer src="{KATEX_JS}"></script>
  <script defer src="{KATEX_AUTORENDER_JS}"
    onload="renderMathInElement(document.body, {{ delimiters: [{{left: '$$', right: '$$', display: true}}, {{left: '$', right: '$', display: false}}] }});"></script>
</head>
<body>
"""

    # 3-2) ファイルを読み込んでヘッダーを先頭に付与していったん保存
    with open(file_path, "r", encoding="utf-8") as f:
        orig = f.read()
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(header_html + orig + "\n</body>\n</html>")

    # 3-3) ローカルファイル URL 化
    local_url = f"file://{os.path.abspath(file_path)}"

    # 3-4) Playwright で JS レンダリング後のページを取得
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(local_url, wait_until="networkidle")
            page.wait_for_load_state("networkidle")
            rendered = page.content()
            browser.close()
    except Exception as e:
        print(f"[Error] Playwright render error: {e}")
        return

    # 3-5) BeautifulSoup で <body> 部分のみを取り出す
    soup = BeautifulSoup(rendered, "html.parser")
    body_only = soup.body.decode_contents()  # <body> 内のすべて

    final_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="{KATEX_CSS}">
</head>
<body>
{body_only}
</body>
</html>"""

    # 3-6) file_path を上書き保存
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(final_html)

    print(f"[Rendered] KaTeX をレンダリング済みにして保存: {file_path}")


def wrap_display_math_center(file_path: str):
    """
    出力先 HTML (KaTeX を含む) を読み込み、
    <span class="katex-display">…</span> 要素をすべて <div style="text-align:center;"> で囲む。
    """
    with open(file_path, "r", encoding="utf-8") as f:
        html = f.read()
    soup = BeautifulSoup(html, "html.parser")

    for span in soup.find_all("span", class_="katex-display"):
        parent_div = soup.new_tag("div", style="text-align:center;")
        span.wrap(parent_div)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(str(soup))


# ───────────────────────────────────────────────────────────
# 4) ファイル操作・列挙関数
# ───────────────────────────────────────────────────────────

def list_problem_files_jp(contest_id: str) -> list[str]:
    """
    jp/problems/{contest_id} フォルダ配下の .html ファイル名 (拡張子なし) のリストを返す。
    例: ['12373', '12712', …]
    """
    dir_path = os.path.join(ROOT_LANG_DIR, "jp", "problems", contest_id)
    if not os.path.isdir(dir_path):
        return []
    result = []
    for fn in os.listdir(dir_path):
        if fn.lower().endswith(".html"):
            result.append(fn[:-5])
    return sorted(result, key=lambda x: int(x))


def list_explanation_files_jp(contest_id: str) -> list[str]:
    """
    jp/explanations/{contest_id} 配下の .html ファイル名 (拡張子なし) のリストを返す。
    """
    dir_path = os.path.join(ROOT_LANG_DIR, "jp", "explanations", contest_id)
    if not os.path.isdir(dir_path):
        return []
    result = []
    for fn in os.listdir(dir_path):
        if fn.lower().endswith(".html"):
            result.append(fn[:-5])
    return sorted(result, key=lambda x: int(x))


def ensure_dir(path: str):
    """
    path が存在しなければ作成する (ディレクトリ向け)。
    """
    os.makedirs(path, exist_ok=True)


# ───────────────────────────────────────────────────────────
# 5) 実際のファイル書き込み関数
# ───────────────────────────────────────────────────────────

def save_jp_task(contest_id: str, task_id: str) -> str | None:
    """
    contests/{contest_id}/tasks/{task_id} から <div id="problem_content"> を抽出し、
    languages/jp/problems/{contest_id}/{task_id}.html に保存する。
    成功時は保存先ファイルパスを返す。すでに存在していればスキップしてそのパスを返す。
    取得失敗時は None を返す。
    """
    out_dir = os.path.join(ROOT_LANG_DIR, "jp", "problems", contest_id)
    ensure_dir(out_dir)

    out_path = os.path.join(out_dir, f"{task_id}.html")
    if os.path.exists(out_path):
        print(f"[Skip JP-Task] {contest_id}/{task_id} は既に存在します。")
        return out_path

    url = f"{BASE_URL}/contests/{contest_id}/tasks/{task_id}"
    print(f"[Fetch JP-Task] {url} → extracting problem_content…")
    inner_html = extract_div_innerhtml_with_playwright(url, div_id="problem_content")
    if not inner_html.strip():
        print(f"[Warn] {contest_id}/{task_id} の problem_content が取得できませんでした。")
        return None

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(inner_html)
    print(f"[Saved JP-Task] {out_path} (bytes={len(inner_html)})")
    return out_path


def save_jp_explanation(contest_id: str, task_id: str) -> str | None:
    """
    contests/{contest_id}/editorial/{task_id} から <div id="editorial_content"> を抽出し、
    languages/jp/explanations/{contest_id}/{task_id}.html に保存する。
    成功時は保存先パスを返す。存在済みならスキップしてそのパスを返す。失敗時は None。
    """
    out_dir = os.path.join(ROOT_LANG_DIR, "jp", "explanations", contest_id)
    ensure_dir(out_dir)

    out_path = os.path.join(out_dir, f"{task_id}.html")
    if os.path.exists(out_path):
        print(f"[Skip JP-Expl] {contest_id}/{task_id} は既に存在します。")
        return out_path

    url = f"{BASE_URL}/contests/{contest_id}/editorial/{task_id}"
    print(f"[Fetch JP-Expl] {url} → extracting editorial_content…")
    inner_html = extract_div_innerhtml_with_playwright(url, div_id="editorial_content")
    if not inner_html.strip():
        print(f"[Warn] {contest_id}/{task_id} の editorial_content が取得できませんでした。")
        return None

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(inner_html)
    print(f"[Saved JP-Expl] {out_path} (bytes={len(inner_html)})")
    return out_path


def save_translated_task(contest_id: str, task_id: str, lang: str, jp_path: str):
    """
    jp_path (languages/jp/problems/{contest_id}/{task_id}.html) を読み込み、
    GPT 翻訳 → KaTeX レンダリング → 表示整形 して
    languages/{lang}/problems/{contest_id}/{task_id}.html に書き出す。
    """
    out_dir = os.path.join(ROOT_LANG_DIR, lang, "problems", contest_id)
    ensure_dir(out_dir)

    out_path = os.path.join(out_dir, f"{task_id}.html")
    if os.path.exists(out_path):
        print(f"[Skip {lang}-Task] {contest_id}/{task_id} は既に存在します。")
        return

    with open(jp_path, "r", encoding="utf-8") as f:
        jp_html = f.read()

    print(f"[Translate Task → {lang}] {contest_id}/{task_id}")
    translated_html = ask_gpt_translated_html(jp_html, term="task", target_lang=lang)
    if not translated_html.strip():
        print(f"[Error] {contest_id}/{task_id} の {lang} 翻訳が空です。")
        return

    # まず「翻訳後の HTML」をファイルに書き出す
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(translated_html)
    print(f"[Saved {lang}-Raw] {out_path} (bytes={len(translated_html)})")

    # KaTeX を含むレンダリング済み HTML に変換
    render_to_final_html(out_path)
    # display-math を中央寄せに包む
    wrap_display_math_center(out_path)


def save_translated_explanation(contest_id: str, task_id: str, lang: str, jp_path: str):
    """
    jp_path (languages/jp/explanations/{contest_id}/{task_id}.html) を読み込み、
    翻訳 → レンダリング → 表示整形 して
    languages/{lang}/explanations/{contest_id}/{task_id}.html に書き出す。
    """
    out_dir = os.path.join(ROOT_LANG_DIR, lang, "explanations", contest_id)
    ensure_dir(out_dir)

    out_path = os.path.join(out_dir, f"{task_id}.html")
    if os.path.exists(out_path):
        print(f"[Skip {lang}-Expl] {contest_id}/{task_id} は既に存在します。")
        return

    with open(jp_path, "r", encoding="utf-8") as f:
        jp_html = f.read()

    print(f"[Translate Expl → {lang}] {contest_id}/{task_id}")
    translated_html = ask_gpt_translated_html(jp_html, term="editorial", target_lang=lang)
    if not translated_html.strip():
        print(f"[Error] {contest_id}/{task_id} の {lang} 解説翻訳が空です。")
        return

    # まず「翻訳後の HTML」をファイルに書き出す
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(translated_html)
    print(f"[Saved {lang}-RawExpl] {out_path} (bytes={len(translated_html)})")

    # KaTeX を含むレンダリング済み HTML に変換
    render_to_final_html(out_path)
    # display-math を中央寄せに包む
    wrap_display_math_center(out_path)


# ───────────────────────────────────────────────────────────
# 6) 言語リストを読み込む
# ───────────────────────────────────────────────────────────

def load_languages_list() -> list[str]:
    """
    languages/config.json から翻訳対象言語（例えば ["en","fr","zh"]）を読み込んで返す。
    config.json は以下のような構造を想定:
      {
        "languages": ["en", "fr", "zh", …]
      }
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
    # 常に「en」を先頭に置き、それ以降ユーザーが追加した順番で
    ordered = ["en"] + [l for l in langs if l != "en"]
    return ordered


# ───────────────────────────────────────────────────────────
# 7) メインループ
# ───────────────────────────────────────────────────────────

def main():
    # 7-1) トップページを取得して最新終了済コンテストを探す
    try:
        homepage_html = fetch_url_html(HOMEPAGE_URL)
    except Exception:
        return

    latest_contest = find_latest_ended_contest(homepage_html)
    if not latest_contest:
        print("最新の終了済コンテストが見つかりませんでした。")
        return

    print(f"最新の終了済コンテスト ID = {latest_contest}")

    # 7-2) そのコンテストのタスク一覧を取得
    task_ids = fetch_task_ids(latest_contest)
    if not task_ids:
        print(f"[Warn] {latest_contest} にタスクが見つかりません。終了。")
        return

    print(f"{latest_contest} のタスク一覧 = {task_ids}")

    # 7-3) 翻訳対象言語リストをロード (最初に "en" が来る)
    languages = load_languages_list()
    print(f"翻訳対象の言語リスト = {languages}")

    # --- (1) JP 保存 & EN 翻訳 のループ ---
    en_jp_map = {}  # { task_id: jp_task_filepath }

    for tid in task_ids:
        # (a) 日本語版問題を保存
        jp_path = save_jp_task(latest_contest, tid)
        if not jp_path:
            continue
        # (b) 英語タスクを翻訳・保存
        save_translated_task(latest_contest, tid, lang="en", jp_path=jp_path)
        en_jp_map[tid] = jp_path

        # (c) 日本語版解説がある場合は保存
        jp_expl_path = save_jp_explanation(latest_contest, tid)
        if jp_expl_path:
            save_translated_explanation(latest_contest, tid, lang="en", jp_path=jp_expl_path)

    # --- (2) EN 翻訳が一通り終わったら、残りの言語を順次翻訳 ---
    remaining = [l for l in languages if l != "en"]
    if remaining:
        print(f"EN 翻訳完了後、残り言語の翻訳を開始します: {remaining}")
        for lang in remaining:
            for tid, jp_path in en_jp_map.items():
                save_translated_task(latest_contest, tid, lang=lang, jp_path=jp_path)
                # 英語版解説があればそちらも残り言語に翻訳
                expl_jp = os.path.join(ROOT_LANG_DIR, "jp", "explanations", latest_contest, f"{tid}.html")
                if os.path.exists(expl_jp):
                    save_translated_explanation(latest_contest, tid, lang=lang, jp_path=expl_jp)

    print("すべてのタスク＆解説の保存・翻訳処理が完了しました。")


if __name__ == "__main__":
    main()
