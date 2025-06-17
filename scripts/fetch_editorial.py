# scripts/fetch_editorial.py

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

BASE_URL     = "https://onlinemathcontest.com"
LANG_CONFIG_PATH = Path(__file__).parents[1] / "languages" / "config.json"
OUTPUT_ROOT      = Path(__file__).parents[1] / "languages"

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("[Error] OPENAI_API_KEY が設定されていません。")
    sys.exit(1)
openai.api_key = OPENAI_KEY
GPT_MODEL = "gpt-4o-mini"

def git_add_and_push(file_path: Path):
    try:
        # ローカルリポジトリの user.name / email を設定
        subprocess.run(["git", "config", "--local", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "--local", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        # add → commit → push
        subprocess.run(["git", "add", str(file_path)], check=True)
        subprocess.run(["git", "commit", "-m", f"Add {file_path}"], check=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
        print(f"[Git] {file_path} を commit & push")
    except subprocess.CalledProcessError as e:
        print(f"[Error] Git 操作中に例外発生: {e}")

def fetch_url_html(url: str) -> str:
    """
    requests.get を使って指定 URL の HTML を取得し、文字列として返す。
    ステータスコード 200 以外は例外を投げる。
    """
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
    """
    トップページ HTML を解析して、最初に出てくる
    <div class="contest-header"> ... <div class="contest-status">終了済</div> ...
    のような部分を探し、対応する <a class="contest-name" href="/contests/{ID}"> を辿って
    コンテスト ID を返す。見つからなければ None。
    """
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
    """
    /contests/{contest_id} ページから、すべての <a href="/contests/{contest_id}/tasks/{task_id}"> を探索し、
    task_id 部分の数値文字列をリストで返す。ソート済み。
    """
    url = f"{BASE_URL}/contests/{contest_id}"
    try:
        html = fetch_url_html(url)
    except Exception as e:
        print(f"[Error] {contest_id} ページ取得失敗 → {e}")
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


def fetch_editorial_html_with_playwright(page: Page, contest_id: str, task_id: str) -> str:
    """
    Playwright の page オブジェクトで、/contests/{contest_id}/editorial/{task_id} を開いてから
    <div id="editorial_content"> の innerHTML を取得し、文字列で返す。
    取得できなければ空文字を返す。
    """
    url = f"{BASE_URL}/contests/{contest_id}/editorial/{task_id}"
    try:
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector("#editorial_content", timeout=8000)
        content = page.eval_on_selector("#editorial_content", "el => el.innerHTML")
        return content or ""
    except PlaywrightTimeoutError:
        print(f"[Timeout] {url} に #editorial_content が現れませんでした。")
        return ""
    except Exception as e:
        print(f"[Error] Playwright 例外: {url} -> {e}")
        return ""


def HtmlKatex(html: str) -> str:
    """
    すでに KaTeX が埋め込まれた HTML の中から
    「<span class="katex"><annotation encoding="application/x-tex">…</annotation>…</span>」を探し、
    TeX ソース部分を "$…$" の形式に置き換える。
    翻訳前に行うことで、翻訳結果で KaTeX マークアップが維持されるようにする。
    """
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.find_all(class_="katex"):
        tex = katex.find("annotation", {"encoding": "application/x-tex"})
        if tex:
            katex.replace_with(f"${tex.text}$")
    return str(soup)


def ask_gpt(question: str, model: str, term: str) -> str:
    """
    OpenAI ChatCompletion API を呼び出し、
    HTML 形式の KaTeX 数式と日本語文章を「モデル=model」で target language に翻訳させる。
    システムプロンプトで HTML の書式を壊さないように指示し、
    結果として翻訳済みの HTML をそのまま返させる。
    """
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
    """
    日本語の HTML (KaTeX 埋め込み) を GPT に投げて target_lang に翻訳した結果を返す。
    """
    latex_ready = HtmlKatex(jp_html)
    translated = ask_gpt(latex_ready, GPT_MODEL, term)
    return translated


def load_languages_list() -> list[str]:
    """
    languages/config.json を読み込んで "languages" フィールドを得る。
    先頭に必ず "en" が来るように並べ替えて返す。
    """
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


def render_html_with_playwright(page: Page, file_path: Path):
    """
    (翻訳済み HTML に含まれる KaTeX 数式をレンダリングするため)
    file_path に一時的に KaTeX の <head> と <script> を挿入し、
    headless Chromium (Playwright) で開いて JS による数式描画を行い、
    レンダリング後の <body> 部分を抜き出して最終 HTML として上書き保存する。
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
                      {left: '$',  right: '$',  display: false}
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


def change_editorial_display(contest_id: str, task_id: str, lang: str = "en"):
    """
    editorial HTML の中に含まれる <span class="katex-display"> を <div style="text-align:center;"> でラップし、
    数式を中央寄せにする。
    """
    file_path = OUTPUT_ROOT / lang / "contests" / contest_id / "editorial" / f"{task_id}.html"
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


def save_jp_editorial(contest_id: str, task_id: str, page: Page) -> Path | None:
    """
    Playwright の page オブジェクトを使って editorial ページを開き、
    innerHTML を fetch して languages/ja/... に保存する。
    既にファイルが存在すればスキップして Path を返す。取得失敗時は None を返す。
    """
    jp_folder = OUTPUT_ROOT / "ja" / "contests" / contest_id / "editorial"
    jp_folder.mkdir(parents=True, exist_ok=True)

    jp_path = jp_folder / f"{task_id}.html"
    if jp_path.exists():
        print(f"[Skip JP] {contest_id}/{task_id} の解説はすでに存在します。")
        return jp_path

    inner_html = fetch_editorial_html_with_playwright(page, contest_id, task_id)
    if not inner_html.strip():
        print(f"[Warning] {contest_id}/{task_id} の解説コンテンツが空または取得失敗。")
        return None

    jp_path.write_text(inner_html, encoding="utf-8")
    print(f"[Saved JP] {jp_path} (bytes={len(inner_html)})")
    return jp_path


def save_translated_editorial(contest_id: str,
                              task_id: str,
                              lang: str,
                              term: str,
                              jp_filepath: Path,
                              page: Page):
    """
    日本語版解説 (jp_filepath) を target lang に翻訳し、
    languages/{lang}/contests/{contest_id}/editorial/{task_id}.html に保存する。
    翻訳後、KaTeX レンダリング & 中央寄せを行う。
    """
    out_folder = OUTPUT_ROOT / lang / "contests" / contest_id / "editorial"
    out_folder.mkdir(parents=True, exist_ok=True)

    out_path = out_folder / f"{task_id}.html"
    if out_path.exists():
        print(f"[Skip {lang}] {contest_id}/{task_id} の解説はすでに翻訳済みです。")
        return

    jp_html = jp_filepath.read_text(encoding="utf-8")
    print(f"[Translate → {lang}] contest={contest_id}, editorial={task_id}, term={term}")
    translated_html = translate_html_for_lang(jp_html, term, lang)
    if not translated_html.strip():
        print(f"[Error] {contest_id}/{task_id} の {lang} 翻訳結果が空でした。スキップ")
        return

    out_path.write_text(translated_html, encoding="utf-8")
    print(f"[Saved {lang}] {out_path} (bytes={len(translated_html)})")

    render_html_with_playwright(page, out_path)
    change_editorial_display(contest_id, task_id, lang)


def check_existence_editorial(contest_id: str, task_id: str, lang: str = "en") -> bool:
    """
    languages/{lang}/contests/{contest_id}/editorial/{task_id}.html が既にあるかを返す。
    """
    return (OUTPUT_ROOT / lang / "contests" / contest_id / "editorial" / f"{task_id}.html").exists()


def translate_editorials_for_contest(contest_id:str, page:Page):
    task_ids = fetch_task_ids(contest_id)
    langs = load_languages_list()
    # JP 取得
    for tid in task_ids:
        save_jp_editorial(contest_id, tid, page)
    # EN 翻訳
    for tid in task_ids:
        if not check_existence_editorial(contest_id, tid, lang="en"):
            save_translated_editorial(contest_id, tid, lang="en", term="editorial",
                                     jp_filepath=OUTPUT_ROOT/"ja"/"contests"/contest_id/"editorial"/f"{tid}.html", page=page)
            git_add_and_push(...)
    # 他言語
    other = [l for l in langs if l!="en"]
    for tid in task_ids:
        for lang in other:
            if not check_existence_editorial(contest_id, tid, lang=lang):
                save_translated_editorial(contest_id, tid, lang=lang, term="editorial",
                                         jp_filepath=OUTPUT_ROOT/"ja"/"contests"/contest_id/"editorial"/f"{tid}.html", page=page)
                git_add_and_push(...)

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--contest", help="対象ContestIDを指定")
    args=parser.parse_args()
    contest = args.contest or (find_latest_ended_contest(fetch_url_html(BASE_URL + "/")))
    if not contest:
        sys.exit(0)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_context().new_page()
        translate_editorials_for_contest(contest, page)
        browser.close()