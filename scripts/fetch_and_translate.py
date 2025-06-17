# scripts/fetch_and_translate.py

import os
import sys
import time
import json
import requests
from bs4 import BeautifulSoup
from pathlib import Path
import subprocess
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Browser
import openai

# ───────────────────────────────────────────────────────────
# 1) ==================== 設定項目 =========================
# ───────────────────────────────────────────────────────────
BASE_URL     = "https://onlinemathcontest.com"
HOMEPAGE_URL = BASE_URL + "/"
LANG_CONFIG_PATH = Path(__file__).parents[1] / "languages" / "config.json"
OUTPUT_ROOT      = Path(__file__).parents[1] / "languages"
# OpenAI API キー
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("[Error] 環境変数 OPENAI_API_KEY が設定されていません。")
    sys.exit(1)
openai.api_key = OPENAI_KEY
GPT_MODEL = "gpt-4o-mini"
# OMC ログイン
OMC_USERNAME = os.getenv("OMC_USERNAME")
OMC_PASSWORD = os.getenv("OMC_PASSWORD")
if not (OMC_USERNAME and OMC_PASSWORD):
    print("[Error] OMC_USERNAME / OMC_PASSWORD が設定されていません。")
    sys.exit(1)


# ───────────────────────────────────────────────────────────
# 2) ================= KaTeX レンダリング ====================
# ───────────────────────────────────────────────────────────

def render_html_with_playwright(page: Page, file_path: Path):
    """
    (翻訳済み HTML に含まれる KaTeX 数式をレンダリングするため)
    file_path に一時的に KaTeX の <head> と <script> を挿入し、
    headless Chromium (Playwright) で開いて JS による数式描画を行い、
    最終的には <body> 部分だけを抜き出して上書き保存する。
    """
    new_header = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
  <script defer
          src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.js"></script>
  <script defer
          src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/contrib/auto-render.min.js"
          onload="renderMathInElement(document.body, { 
                    delimiters: [
                      {left: '$$', right: '$$', display: true}, 
                      {left: '$', right: '$', display: false}
                    ] 
                  });"></script>
  <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
</head>
<body>
"""
    original_content = file_path.read_text(encoding="utf-8")
    wrapped = new_header + original_content + "\n</body></html>"
    file_path.write_text(wrapped, encoding="utf-8")

    file_url = "file://" + str(file_path.resolve())
    page.goto(file_url, wait_until="networkidle")
    page.wait_for_load_state("networkidle")
    full_html = page.content()

    soup = BeautifulSoup(full_html, "html.parser")
    body_content = soup.body.decode_contents()

    final_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
</head>
<body>
{body_content}
</body>
</html>"""

    file_path.write_text(final_html, encoding="utf-8")
    print(f"[Rendered KaTeX] {file_path} をレンダリング済み HTML に変換しました。")


def change_problem_display(contest_id: str, task_id: str, lang: str = "en"):
    """
    翻訳済みタスク HTML のうち、display 数式 (<span class="katex-display">) を
    <div style="text-align:center;"> でラップして中央寄せにする。
    """
    file_path = OUTPUT_ROOT / lang / "contests" / contest_id / "tasks" / f"{task_id}.html"
    if not file_path.exists():
        print(f"[Error] {file_path} が存在しません。")
        return

    html = file_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    for elt in soup.find_all("span", class_="katex-display"):
        wrapper = soup.new_tag("div", style="text-align:center;")
        elt.wrap(wrapper)

    file_path.write_text(str(soup), encoding="utf-8")
    print(f"[Wrapped display] {file_path} の <span class=\"katex-display\"> を中央寄せに変更")


# ───────────────────────────────────────────────────────────
# 3) ================ Playwright Helper =====================
# ───────────────────────────────────────────────────────────

def login_omc_with_playwright(page: Page) -> bool:
    login_url = BASE_URL + "/login"
    print("→ OMC login ページを開きます:", login_url)
    page.goto(login_url, wait_until="networkidle")

    try:
        page.wait_for_selector("form[action='https://onlinemathcontest.com/login']", timeout=8000)
    except PlaywrightTimeoutError:
        print("[Error] ログインフォームが見つかりませんでした。")
        return False

    csrf_token = page.get_attribute("input[name='_token']", "value")
    if not csrf_token:
        print("[Error] CSRF トークン(input[name='_token']) を取得できませんでした。")
        return False
    print(f"→ 取得した CSRF トークン: {csrf_token}")

    page.fill("input[name='display_name']", OMC_USERNAME)
    page.fill("input[name='password']", OMC_PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")

    if page.url.endswith("/login"):
        print("[Error] ログインに失敗しました。認証情報を確認してください。")
        return False

    print("→ ログイン成功 (リダイレクト先:", page.url, ")")
    return True


def extract_div_innerhtml_with_playwright(page: Page, url: str, div_id: str, timeout: int = 8000) -> str:
    try:
        page.goto(url, wait_until="networkidle")
        try:
            page.wait_for_selector(f"#{div_id}", timeout=timeout)
        except PlaywrightTimeoutError:
            print(f"[Timeout] {url} に #{div_id} が現れませんでした。")
            return ""
        content = page.eval_on_selector(f"#{div_id}", "el => el.innerHTML")
        return content or ""
    except Exception as e:
        print(f"[Error] Playwright 例外: {url} -> {e}")
        return ""


def fetch_task_ids_playwright(page: Page, contest_id: str) -> list[str]:
    """
    Playwright の page を使ってログイン Cookie を付与したまま、
    /contests/{contest_id} ページを開き、タスクリンクをすべて抽出する。
    """
    url = f"{BASE_URL}/contests/{contest_id}"
    print(f"→ (Playwright) ページを開きます: {url}")
    page.goto(url, wait_until="networkidle")
    html = page.content()

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
# 4) ============== その他ヘルパー関数群 =====================
# ───────────────────────────────────────────────────────────

def fetch_url_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/114.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.text


def find_current_contest(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for header_div in soup.select("div.contest-header"):
        status_div = header_div.find("div", class_="contest-status")
        if status_div and "開催中" in status_div.get_text(strip=True):
            sib = header_div.find_next_sibling()
            while sib:
                if sib.name == "a" and "contest-name" in sib.get("class", []):
                    href = sib.get("href", "")
                    if href:
                        return href.strip().rstrip("/").split("/")[-1]
                sib = sib.find_next_sibling()
    return None


def HtmlKatex(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.find_all(class_="katex"):
        tex = katex.find("annotation", {"encoding": "application/x-tex"})
        if tex:
            katex.replace_with(f"${tex.text}$")
    return str(soup)


def ask_gpt(question: str, model: str, term: str) -> str:
    try:
        resp = openai.ChatCompletion.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"The text you are about to receive is HTML-formatted KaTeX math {term} written in Japanese.\n"
                        "Please translate all sentences into the target language (e.g. English), preserving all KaTeX formatting\n"
                        "(font size, line breaks, class=\"katex-display\" for display formulas, etc.).\n"
                        "Return ONLY the translated HTML; do not wrap it in extra tags, do not alter KaTeX markup.\n"
                        "If input is empty or none, return empty. The content may be long: please output fully.\n"
                    )
                },
                {"role": "user", "content": question}
            ],
            temperature=0.0
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[GPT error] {e}")
        time.sleep(5)
        return ask_gpt(question, model, term)


def translate_html_for_lang(jp_html: str, term: str, target_lang: str) -> str:
    latex_ready = HtmlKatex(jp_html)
    translated = ask_gpt(latex_ready, GPT_MODEL, term)
    return translated


def load_languages_list() -> list[str]:
    if not LANG_CONFIG_PATH.exists():
        print(f"[Error] {LANG_CONFIG_PATH} が見つかりません。")
        sys.exit(1)

    data = json.loads(LANG_CONFIG_PATH.read_text(encoding="utf-8"))
    langs = data.get("languages", [])
    if "en" not in langs:
        print("[Error] config.json の languages に 'en' が含まれていません。")
        sys.exit(1)

    ordered = ["en"] + [l for l in langs if l != "en"]
    return ordered


def save_jp_problem(contest_id: str, task_id: str, page: Page) -> Path | None:
    jp_folder = OUTPUT_ROOT / "ja" / "contests" / contest_id / "tasks"
    jp_folder.mkdir(parents=True, exist_ok=True)

    jp_path = jp_folder / f"{task_id}.html"
    if jp_path.exists():
        print(f"[Skip JP] {contest_id}/{task_id} は既に存在します。")
        return jp_path

    url = f"{BASE_URL}/contests/{contest_id}/tasks/{task_id}"
    print(f"[Fetch JP] {url} → extracting problem_content …")
    inner_html = extract_div_innerhtml_with_playwright(page, url, div_id="problem_content")
    if not inner_html.strip():
        print(f"[Warning] {contest_id}/{task_id} の problem_content が空、または取得失敗")
        return None

    jp_path.write_text(inner_html, encoding="utf-8")
    print(f"[Saved JP] {jp_path} (bytes={len(inner_html)})")
    return jp_path


def save_translated_html(contest_id: str,
                         task_id: str,
                         lang: str,
                         term: str,
                         jp_filepath: Path,
                         page: Page):
    out_folder = OUTPUT_ROOT / lang / "contests" / contest_id / "tasks"
    out_folder.mkdir(parents=True, exist_ok=True)

    out_path = out_folder / f"{task_id}.html"
    if out_path.exists():
        print(f"[Skip {lang}] {contest_id}/{task_id} はすでに {lang} 翻訳済みです。")
        return

    jp_html = jp_filepath.read_text(encoding="utf-8")
    print(f"[Translate → {lang}] contest={contest_id}, task={task_id}, term={term}")
    translated_html = translate_html_for_lang(jp_html, term, lang)
    if not translated_html.strip():
        print(f"[Error] {contest_id}/{task_id} の {lang} 翻訳結果が空でした。スキップ")
        return

    out_path.write_text(translated_html, encoding="utf-8")
    print(f"[Saved {lang}] {out_path} (bytes={len(translated_html)})")

    render_html_with_playwright(page, out_path)
    change_problem_display(contest_id, task_id, lang)


def check_existence_problem(contest_id: str, task_id: str, lang: str = "en") -> bool:
    return (OUTPUT_ROOT / lang / "contests" / contest_id / "tasks" / f"{task_id}.html").exists()

def translate_tasks_for_contest(contest_id: str, page: Page):
    task_ids = fetch_task_ids_playwright(page, contest_id)
    if not task_ids:
        print(f"[Warning] コンテスト {contest_id} にタスクが見つかりません。")
        return
    languages = load_languages_list() 
    for tid in task_ids:
        save_jp_problem(contest_id, tid, page)
    for tid in task_ids:
        if not check_existence_problem(contest_id, tid, lang="en"):
            save_translated_html(contest_id, tid, lang="en", term="task",
                                 jp_filepath=OUTPUT_ROOT/"ja"/"contests"/contest_id/"tasks"/f"{tid}.html", page=page)
            git_add_and_push(f"languages/en/contests/{contest_id}/tasks/{tid}.html")
    other_langs = [l for l in languages if l != "en"]
    for tid in task_ids:
        for lang in other_langs:
            if not check_existence_problem(contest_id, tid, lang=lang):
                save_translated_html(contest_id, tid, lang=lang, term="task",
                                     jp_filepath=OUTPUT_ROOT/"ja"/"contests"/contest_id/"tasks"/f"{tid}.html", page=page)
                git_add_and_push(f"languages/{lang}/contests/{contest_id}/tasks/{tid}.html")

def git_add_and_push(path: str):
    try:
        subprocess.run(["git", "add", path], check=True)
        subprocess.run(["git", "commit", "-m", f"Add {path}"], check=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
        print(f"[Git] {path} をコミット＆プッシュしました。")
    except subprocess.CalledProcessError as e:
        print(f"[Error] Git 操作中に例外発生: {e}")

# ───────────────────────────────────────────────────────────
# 3) main() + --contest オプション
# ───────────────────────────────────────────────────────────
def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        if not login_omc_with_playwright(page):
            sys.exit(1)
        # トップページ
        page.goto(HOMEPAGE_URL, wait_until="networkidle")
        home_html = page.content()
        default_contest = find_current_contest(home_html)
        contest_id = DEFAULT_CONTEST if (DEFAULT_CONTEST := os.getenv("CONTEST_ID")) else default_contest
        if not contest_id:
            print("[Info] contest_id が指定も自動検出もできませんでした。")
            sys.exit(0)
        print(f"→ 対象 Contest ID = {contest_id}")
        translate_tasks_for_contest(contest_id, page)
        # JSON 出力処理などは従来通り（必要であれば）
        browser.close()

if __name__ == "__main__":
    # env で CONTEST_ID があれば優先
    main()