# scripts/fetch_and_translate.py

import os
import sys
import time
import json
import requests
import tiktoken
from bs4 import BeautifulSoup
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import openai

# ───────────────────────────────────────────────────────────
# 1) ==================== 設定項目 =========================
# ───────────────────────────────────────────────────────────

BASE_URL     = "https://onlinemathcontest.com"
HOMEPAGE_URL = BASE_URL + "/"

LANG_CONFIG_PATH = Path(__file__).parents[1] / "languages" / "config.json"
OUTPUT_ROOT      = Path(__file__).parents[1] / "languages"

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("[Error] 環境変数 OPENAI_API_KEY が設定されていません。")
    sys.exit(1)

openai.api_key = OPENAI_KEY
GPT_MODEL = "gpt-4o-mini"


# ───────────────────────────────────────────────────────────
# 2) ================= Playwright Helper ===================
# ───────────────────────────────────────────────────────────

def extract_div_innerhtml_with_playwright(url: str, div_id: str, timeout: int = 8000) -> str:
    """
    指定した URL を Playwright で開き、<div id="{div_id}"> の innerHTML を取得して返す。
    取得に失敗した場合は空文字を返す。
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


def render_html_with_playwright(file_path: Path):
    """
    1) file_path に一度最低限の <head> + KaTeX スクリプトを付け加えて保存
    2) Playwright で headless Chromium を起動し、JS レンダリング後の HTML を取得
    3) <body> 内だけを抜き出し、最終 HTML として file_path に上書き
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
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(file_url, wait_until="networkidle")
        page.wait_for_load_state("networkidle")
        full_html = page.content()
        browser.close()

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


# ───────────────────────────────────────────────────────────
# 3) ================= 共通ヘルパー関数群 ===================
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


def find_latest_ended_contest(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for header_div in soup.select("div.contest-header"):
        status_div = header_div.find("div", class_="contest-status")
        if status_div and "終了済" in status_div.get_text(strip=True):
            sib = header_div.find_next_sibling()
            while sib:
                if sib.name == "a" and "contest-name" in sib.get("class", []):
                    href = sib.get("href", "")
                    if href:
                        return href.strip().rstrip("/").split("/")[-1]
                sib = sib.find_next_sibling()
    return None


def fetch_task_ids(contest_id: str) -> list[str]:
    url = f"{BASE_URL}/contests/{contest_id}"
    try:
        html = fetch_url_html(url)
    except Exception as e:
        print(f"[Error] {contest_id} ページ取得失敗→ {e}")
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
                        "Return ONLY the translated HTML; do not wrap it in extra tags, and do not alter KaTeX markup.\n"
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


def save_jp_problem(contest_id: str, task_id: str) -> Path | None:
    jp_folder = OUTPUT_ROOT / "ja" / "contests" / contest_id / "tasks"
    jp_folder.mkdir(parents=True, exist_ok=True)

    jp_path = jp_folder / f"{task_id}.html"
    if jp_path.exists():
        print(f"[Skip JP] {contest_id}/{task_id} は既に存在します。")
        return jp_path

    url = f"{BASE_URL}/contests/{contest_id}/tasks/{task_id}"
    print(f"[Fetch JP] {url} → extracting problem_content …")
    inner_html = extract_div_innerhtml_with_playwright(url, div_id="problem_content")
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
                         jp_filepath: Path):
    out_folder = OUTPUT_ROOT / lang / "contests" / contest_id / "tasks"
    out_folder.mkdir(parents=True, exist_ok=True)

    out_path = out_folder / f"{task_id}.html"
    if out_path.exists():
        print(f"[Skip {lang}] {contest_id}/{task_id} は既に翻訳済みです。")
        return

    jp_html = jp_filepath.read_text(encoding="utf-8")
    print(f"[Translate → {lang}] contest={contest_id}, task={task_id}, term={term}")
    translated_html = translate_html_for_lang(jp_html, term, lang)
    if not translated_html.strip():
        print(f"[Error] {contest_id}/{task_id} の {lang} 翻訳結果が空でした。スキップ")
        return

    out_path.write_text(translated_html, encoding="utf-8")
    print(f"[Saved {lang}] {out_path} (bytes={len(translated_html)})")

    render_html_with_playwright(out_path)
    change_problem_display(contest_id, task_id, lang)


def change_problem_display(contest_id: str, task_id: str, lang: str = "en"):
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


def list_problem_files(contest_id: str) -> list[str]:
    folder = OUTPUT_ROOT / "ja" / "contests" / contest_id / "tasks"
    y_list: list[str] = []
    if folder.exists() and folder.is_dir():
        for fname in os.listdir(folder):
            if fname.endswith(".html"):
                y_list.append(fname[:-5])
    return y_list


def check_existence_problem(contest_id: str, task_id: str, lang: str = "en") -> bool:
    return (OUTPUT_ROOT / lang / "contests" / contest_id / "tasks" / f"{task_id}.html").exists()


def main():
    try:
        home_html = fetch_url_html(HOMEPAGE_URL)
    except Exception as e:
        print(f"[Error] トップページ取得に失敗: {e}")
        sys.exit(1)

    latest_contest = find_latest_ended_contest(home_html)
    if latest_contest is None:
        print("最新の終了済コンテストが見つかりませんでした。終了します。")
        return

    print(f"最新の終了済コンテスト ID = {latest_contest}")

    task_ids = fetch_task_ids(latest_contest)
    if not task_ids:
        print(f"[Warning] {latest_contest} にタスクが 0 件です。終了します。")
        return

    print(f"{latest_contest} のタスク一覧 = {task_ids}")

    languages = load_languages_list()
    print(f"翻訳対象言語リスト = {languages}")

    en_jp_map: dict[str, Path] = {}

    for tid in task_ids:
        jp_path = save_jp_problem(latest_contest, tid)
        if jp_path is None:
            continue

        if not check_existence_problem(latest_contest, tid, lang="en"):
            save_translated_html(latest_contest, tid, lang="en", term="task", jp_filepath=jp_path)
        else:
            print(f"[Skip EN] {latest_contest}/{tid} は既に EN 翻訳済みです。")
        en_jp_map[tid] = jp_path

    remaining_langs = [l for l in languages if l != "en"]
    if remaining_langs:
        print(f"EN 翻訳完了 → 残り言語を順番に処理します: {remaining_langs}")
        for lang in remaining_langs:
            for tid, jp_path in en_jp_map.items():
                if not check_existence_problem(latest_contest, tid, lang=lang):
                    save_translated_html(latest_contest, tid, lang=lang, term="task", jp_filepath=jp_path)
                else:
                    print(f"[Skip {lang}] {latest_contest}/{tid} は既に {lang} 翻訳済みです。")

    print("すべてのタスク翻訳処理が完了しました。")


if __name__ == "__main__":
    main()
