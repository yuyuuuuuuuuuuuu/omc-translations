# scripts/fetch_and_translate.py

import os
import sys
import time
import json
import requests
import subprocess
from pathlib import Path
from bs4 import BeautifulSoup

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Browser
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

OMC_USERNAME = os.getenv("OMC_USERNAME")
OMC_PASSWORD = os.getenv("OMC_PASSWORD")
if not (OMC_USERNAME and OMC_PASSWORD):
    print("[Error] 環境変数 OMC_USERNAME / OMC_PASSWORD が設定されていません。")
    sys.exit(1)

# ───────────────────────────────────────────────────────────
# 2) その他ヘルパー関数達（省略可） =======================
# ───────────────────────────────────────────────────────────

def login_omc_with_playwright(page: Page) -> bool:
    login_url = BASE_URL + "/login"
    page.goto(login_url, wait_until="networkidle")
    try:
        page.wait_for_selector("form[action='https://onlinemathcontest.com/login']", timeout=8000)
    except PlaywrightTimeoutError:
        print("[Error] ログインフォームが見つかりませんでした。")
        return False
    csrf_token = page.get_attribute("input[name='_token']", "value")
    if not csrf_token:
        print("[Error] CSRF トークンが取得できませんでした。")
        return False
    page.fill("input[name='display_name']", OMC_USERNAME)
    page.fill("input[name='password']", OMC_PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")
    if page.url.endswith("/login"):
        print("[Error] ログインに失敗しました。")
        return False
    print("→ ログイン成功")
    return True

def find_current_contest(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for header_div in soup.select("div.contest-header"):
        status = header_div.find("div", class_="contest-status")
        if status and "開催中" in status.get_text():
            sib = header_div.find_next_sibling()
            while sib:
                if sib.name == "a" and "contest-name" in sib.get("class", []):
                    return sib["href"].rstrip("/").split("/")[-1]
                sib = sib.find_next_sibling()
    return None

def fetch_url_html(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.text

def extract_div_innerhtml_with_playwright(page: Page, url: str, div_id: str, timeout: int = 8000) -> str:
    try:
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector(f"#{div_id}", timeout=timeout)
        return page.eval_on_selector(f"#{div_id}", "el => el.innerHTML") or ""
    except Exception:
        return ""

def save_jp_problem(contest_id: str, task_id: str, page: Page) -> Path | None:
    jp_folder = OUTPUT_ROOT / "ja" / "contests" / contest_id / "tasks"
    jp_folder.mkdir(parents=True, exist_ok=True)
    jp_path = jp_folder / f"{task_id}.html"
    if jp_path.exists():
        return jp_path
    url = f"{BASE_URL}/contests/{contest_id}/tasks/{task_id}"
    inner_html = extract_div_innerhtml_with_playwright(page, url, div_id="problem_content")
    if not inner_html.strip():
        return None
    jp_path.write_text(inner_html, encoding="utf-8")
    print(f"[Saved JP] {jp_path}")
    return jp_path

def HtmlKatex(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.find_all(class_="katex"):
        tex = katex.find("annotation", {"encoding": "application/x-tex"})
        if tex:
            katex.replace_with(f"${tex.text}$")
    return str(soup)

def ask_gpt(question: str, model: str, term: str) -> str:
    resp = openai.ChatCompletion.create(
        model=model,
        messages=[
            {"role":"system","content":(
                f"The text is HTML+KaTeX math {term} in Japanese. "
                "Translate all sentences to target language, preserve HTML and KaTeX markup. "
                "Return only the translated HTML."
            )},
            {"role":"user","content":question}
        ],
        temperature=0.0
    )
    return resp.choices[0].message.content.strip()

def translate_html_for_lang(jp_html: str, term: str, target_lang: str) -> str:
    latex_ready = HtmlKatex(jp_html)
    return ask_gpt(latex_ready, GPT_MODEL, term)

def load_languages_list() -> list[str]:
    data = json.loads(LANG_CONFIG_PATH.read_text(encoding="utf-8"))
    langs = data.get("languages", [])
    ordered = ["en"] + [l for l in langs if l != "en"]
    return ordered

def render_html_with_playwright(page: Page, file_path: Path):
    new_header = """<!DOCTYPE html>
<html><head>
  <meta charset="utf-8">
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
  <script defer
          src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.js"></script>
  <script defer
          src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/contrib/auto-render.min.js"
          onload="renderMathInElement(document.body, {delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}]});"></script>
</head><body>
"""
    original = file_path.read_text(encoding="utf-8")
    wrapped  = new_header + original + "\n</body></html>"
    file_path.write_text(wrapped, encoding="utf-8")

    file_url = "file://" + str(file_path.resolve())
    page.goto(file_url, wait_until="networkidle")
    page.wait_for_load_state("networkidle")
    full = page.content()
    body = BeautifulSoup(full, "html.parser").body.decode_contents()

    final = f"""<!DOCTYPE html>
<html><head>
  <meta charset="utf-8">
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
</head><body>
{body}
</body></html>"""
    file_path.write_text(final, encoding="utf-8")
    print(f"[Rendered KaTeX] {file_path}")

def change_problem_display(contest_id: str, task_id: str, lang: str = "en"):
    file_path = OUTPUT_ROOT / lang / "contests" / contest_id / "tasks" / f"{task_id}.html"
    if not file_path.exists():
        return
    soup = BeautifulSoup(file_path.read_text(encoding="utf-8"), "html.parser")
    for elt in soup.find_all("span", class_="katex-display"):
        wrapper = soup.new_tag("div", style="text-align:center;")
        elt.wrap(wrapper)
    file_path.write_text(str(soup), encoding="utf-8")

# ───────────────────────────────────────────────────────────
# 3) main 関数
# ───────────────────────────────────────────────────────────

def main(contest_override: str = None):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()

        if not login_omc_with_playwright(page):
            sys.exit(1)

        page.goto(HOMEPAGE_URL, wait_until="networkidle")
        home_html = page.content()
        current_contest = contest_override or find_current_contest(home_html)
        if not current_contest:
            print("[Info] 開催中コンテストが見つかりませんでした。")
            print(json.dumps({"contest_id":"", "duration_min":0}, ensure_ascii=False, separators=(',',':')))
            sys.exit(0)

        print(f"→ 対象 Contest ID = {current_contest}")

        # 1) タスク一覧取得
        task_ids = []
        # （省略：fetch_task_ids_playwright 実装）
        # task_ids = fetch_task_ids_playwright(page, current_contest)

        # 2) 言語リスト取得
        languages = load_languages_list()
        other_langs = [l for l in languages if l != "en"]

        # 3) 日本語版取得
        jp_paths = []
        for tid in task_ids:
            pth = save_jp_problem(current_contest, tid, page)
            if pth:
                jp_paths.append((tid, pth))

        # 4) 英語→その他言語インタリーブ翻訳 & コミット
        for tid, jp_path in jp_paths:
            # 英語
            out_en = OUTPUT_ROOT / "en" / "contests" / current_contest / "tasks" / f"{tid}.html"
            if not out_en.exists():
                # 翻訳
                translated = translate_html_for_lang(jp_path.read_text(), "task", "en")
                out_en.write_text(translated, encoding="utf-8")
                render_html_with_playwright(page, out_en)
                change_problem_display(current_contest, tid, "en")
                # Git commit & push
                try:
                    # ← ここで必ず user.name / email を設定
                    subprocess.run(["git", "config", "--local", "user.name", "github-actions[bot]"], check=True)
                    subprocess.run(["git", "config", "--local", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
                    subprocess.run(["git", "add", str(out_en)], check=True)
                    msg = f"Add languages/en/contests/{current_contest}/tasks/{tid}.html"
                    subprocess.run(["git", "commit", "-m", msg], check=True)
                    subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
                    print(f"[Git] {out_en} を commit & push")
                except subprocess.CalledProcessError as e:
                    print(f"[Error] Git 操作中に例外発生: {e}")

            # その他言語（インタリーブ）
            for lang in other_langs:
                out_path = OUTPUT_ROOT / lang / "contests" / current_contest / "tasks" / f"{tid}.html"
                if not out_path.exists():
                    translated = translate_html_for_lang(jp_path.read_text(), "task", lang)
                    out_path.write_text(translated, encoding="utf-8")
                    render_html_with_playwright(page, out_path)
                    change_problem_display(current_contest, tid, lang)
                    try:
                        subprocess.run(["git", "config", "--local", "user.name", "github-actions[bot]"], check=True)
                        subprocess.run(["git", "config", "--local", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
                        subprocess.run(["git", "add", str(out_path)], check=True)
                        msg = f"Add languages/{lang}/contests/{current_contest}/tasks/{tid}.html"
                        subprocess.run(["git", "commit", "-m", msg], check=True)
                        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
                        print(f"[Git] {out_path} を commit & push")
                    except subprocess.CalledProcessError as e:
                        print(f"[Error] Git 操作中に例外発生: {e}")

        # 5) duration_min 取得 & JSON 出力
        html_contest = fetch_url_html(f"{BASE_URL}/contests/{current_contest}")
        soup2 = BeautifulSoup(html_contest, "html.parser")
        duration_min = 60
        for p in soup2.select("p.list-group-item-heading"):
            text = p.get_text(strip=True)
            if text.endswith("分"):
                num = "".join(filter(str.isdigit, text))
                if num.isdigit():
                    duration_min = int(num)
                    break

        result = {"contest_id": current_contest, "duration_min": duration_min}
        print(json.dumps(result, ensure_ascii=False, separators=(',',':')))
        browser.close()

if __name__ == "__main__":
    # 引数パース（--contest オプションなど）省略
    main()
