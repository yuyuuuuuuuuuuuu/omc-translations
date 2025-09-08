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

# OpenAI API キー（必須）
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_KEY:
    print("[Error] 環境変数 OPENAI_API_KEY が設定されていません。")
    sys.exit(1)
openai.api_key = OPENAI_KEY

GPT_MODEL = "gpt-4o-mini"

# OMC ログイン情報（--no-login モードでは使用しません）
OMC_USERNAME = os.getenv("OMC_USERNAME", "")
OMC_PASSWORD = os.getenv("OMC_PASSWORD", "")

# ───────────────────────────────────────────────────────────
# 2) 共通ヘルパー関数
# ───────────────────────────────────────────────────────────

def fetch_url_html(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.text

def find_current_contest(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for header in soup.select("div.contest-header"):
        status = header.find("div", class_="contest-status")
        if status and "開催中" in status.get_text(strip=True):
            sib = header.find_next_sibling()
            while sib:
                if sib.name == "a" and "contest-name" in sib.get("class", []):
                    return sib["href"].rstrip("/").split("/")[-1]
                sib = sib.find_next_sibling()
    return None

def login_omc_with_playwright(page: Page) -> bool:
    if not (OMC_USERNAME and OMC_PASSWORD):
        print("[Error] OMC_USERNAME / OMC_PASSWORD が設定されていません。")
        return False
    login_url = BASE_URL + "/login"
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
    page.fill("input[name='password']",    OMC_PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")
    if page.url.endswith("/login"):
        print("[Error] ログインに失敗しました。")
        return False
    print("→ ログイン成功")
    return True

def fetch_task_ids_playwright(page: Page, contest_id: str) -> list[str]:
    url = f"{BASE_URL}/contests/{contest_id}"
    page.goto(url, wait_until="networkidle")
    soup = BeautifulSoup(page.content(), "html.parser")
    ids = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        marker = f"/contests/{contest_id}/tasks/"
        if marker in href:
            parts = href.strip().rstrip("/").split("/")
            if len(parts) >= 2 and parts[-2] == "tasks" and parts[-1].isdigit():
                ids.add(parts[-1])
    return sorted(ids, key=lambda x: int(x))

def extract_div_innerhtml_with_playwright(page: Page, url: str, div_id: str, timeout: int = 8000) -> str:
    try:
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector(f"#{div_id}", timeout=timeout)
        return page.eval_on_selector(f"#{div_id}", "el => el.innerHTML") or ""
    except Exception as e:
        print(f"[Error] Playwright 例外: {e}")
        return ""

def save_jp_problem(contest_id: str, task_id: str, page: Page) -> Path | None:
    jp_folder = OUTPUT_ROOT / "ja" / "contests" / contest_id / "tasks"
    jp_folder.mkdir(parents=True, exist_ok=True)
    jp_path = jp_folder / f"{task_id}.html"
    if jp_path.exists():
        return jp_path
    url = f"{BASE_URL}/contests/{contest_id}/tasks/{task_id}"
    print(f"[Fetch JP] {url}")
    html = extract_div_innerhtml_with_playwright(page, url, "problem_content")
    if not html.strip():
        print(f"[Warning] {contest_id}/{task_id} の problem_content 取得失敗")
        return None
    jp_path.write_text(html, encoding="utf-8")
    print(f"[Saved JP] {jp_path} (bytes={len(html)})")
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
            {
                "role": "system",
                "content": (
                    f"The text you are about to receive is HTML-formatted KaTeX math {term} written in Japanese.\n"
                    "Please translate all sentences into the target language (e.g. English), preserving all KaTeX formatting\n"
                    "(font size, line breaks, class=\"katex-display\" for display formulas, etc.).\n"
                    "Return ONLY the translated HTML; do not wrap it in extra tags, do not alter KaTeX markup.\n"
                    "If input is empty or none, return empty."
                )
            },
            {"role": "user", "content": question}
        ],
        temperature=0.0
    )
    return resp.choices[0].message.content.strip()

def translate_html_for_lang(jp_html: str, term: str, target_lang: str) -> str:
    latex_ready = HtmlKatex(jp_html)
    return ask_gpt(latex_ready, GPT_MODEL, term)

def render_html_with_playwright(page: Page, file_path: Path):
    new_header = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.js"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/contrib/auto-render.min.js"
          onload="renderMathInElement(document.body,{delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}]});"></script>
  <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
</head>
<body>
"""
    original = file_path.read_text(encoding="utf-8")
    wrapped  = new_header + original + "\n</body></html>"
    file_path.write_text(wrapped, encoding="utf-8")
    file_url = "file://" + str(file_path.resolve())
    page.goto(file_url, wait_until="networkidle")
    page.wait_for_load_state("networkidle")
    full    = page.content()
    soup    = BeautifulSoup(full, "html.parser")
    body    = soup.body.decode_contents()
    final   = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
</head>
<body>
{body}
</body>
</html>"""
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
    print(f"[Wrapped display] {file_path}")

# ───────────────────────────────────────────────────────────
# 3) メインロジック
# ───────────────────────────────────────────────────────────

def full_translate(contest_override: str|None, no_login: bool):
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()

        # 必要ならログイン
        if not no_login:
            if not login_omc_with_playwright(page):
                sys.exit(1)

        # コンテスト検出
        page.goto(HOMEPAGE_URL, wait_until="networkidle")
        html = page.content()
        current = contest_override or find_current_contest(html)
        if not current:
            print(json.dumps({"contest_id":"","duration_min":0}, ensure_ascii=False, separators=(',',':')))
            browser.close()
            sys.exit(0)
        print(f"→ 対象 Contest ID = {current}")

        # タスク一覧取得
        task_ids = fetch_task_ids_playwright(page, current)
        print(f"→ {current} のタスク一覧 = {task_ids}")

        # 日本語版取得
        for tid in task_ids:
            save_jp_problem(current, tid, page)

        # 言語順取得
        langs = json.loads(LANG_CONFIG_PATH.read_text(encoding="utf-8")).get("languages", [])
        ordered = ["en"] + [l for l in langs if l!="en"]

        # 英語まとめて
        for tid in task_ids:
            jp_path = OUTPUT_ROOT/"ja"/"contests"/current/"tasks"/f"{tid}.html"
            out_en = OUTPUT_ROOT/"en"/"contests"/current/"tasks"/f"{tid}.html"
            if not out_en.exists():
                out_en.parent.mkdir(parents=True, exist_ok=True)
                translated = translate_html_for_lang(jp_path.read_text(encoding="utf-8"), "task", "en")
                out_en.write_text(translated, encoding="utf-8")
                render_html_with_playwright(page, out_en)
                change_problem_display(current, tid, "en")
                try:
                    subprocess.run(["git","config","--local","user.name","github-actions[bot]"],check=True)
                    subprocess.run(["git","config","--local","user.email","github-actions[bot]@users.noreply.github.com"],check=True)
                    subprocess.run(["git","add",str(out_en)],check=True)
                    subprocess.run(["git","commit","-m",f"Add {out_en}"],check=True)
                    subprocess.run(["git","push","origin","HEAD:main"],check=True)
                except subprocess.CalledProcessError as e:
                    print(f"[Error] Git exception: {e}")

        # その他言語インタリーブ
        for tid in task_ids:
            jp_path = OUTPUT_ROOT/"ja"/"contests"/current/"tasks"/f"{tid}.html"
            for lang in ordered[1:]:
                out_p = OUTPUT_ROOT/lang/"contests"/current/"tasks"/f"{tid}.html"
                if not out_p.exists():
                    out_p.parent.mkdir(parents=True, exist_ok=True)
                    translated = translate_html_for_lang(jp_path.read_text(encoding="utf-8"), "task", lang)
                    out_p.write_text(translated, encoding="utf-8")
                    render_html_with_playwright(page, out_p)
                    change_problem_display(current, tid, lang)
                    try:
                        subprocess.run(["git","config","--local","user.name","github-actions[bot]"],check=True)
                        subprocess.run(["git","config","--local","user.email","github-actions[bot]@users.noreply.github.com"],check=True)
                        subprocess.run(["git","add",str(out_p)],check=True)
                        subprocess.run(["git","commit","-m",f"Add {out_p}"],check=True)
                        subprocess.run(["git","push","origin","HEAD:main"],check=True)
                    except subprocess.CalledProcessError as e:
                        print(f"[Error] Git exception: {e}")

        # duration_min だけ JSON 出力
        contest_html = fetch_url_html(f"{BASE_URL}/contests/{current}")
        soup2 = BeautifulSoup(contest_html, "html.parser")
        duration = 60
        for p in soup2.select("p.list-group-item-heading"):
            t = p.get_text(strip=True)
            if t.endswith("分"):
                num = "".join(filter(str.isdigit, t))
                if num.isdigit():
                    duration = int(num)
                    break
        print(json.dumps({"contest_id":current,"duration_min":duration}, ensure_ascii=False, separators=(',',':')))
        browser.close()

def json_only(contest_override: str|None):
    if contest_override:
        current = contest_override
    else:
        html = fetch_url_html(HOMEPAGE_URL)
        current = find_current_contest(html)
    if not current:
        print(json.dumps({"contest_id":"","duration_min":0}, ensure_ascii=False, separators=(',',':')))
        return
    contest_html = fetch_url_html(f"{BASE_URL}/contests/{current}")
    soup2 = BeautifulSoup(contest_html, "html.parser")
    duration = 60
    for p in soup2.select("p.list-group-item-heading"):
        t = p.get_text(strip=True)
        if t.endswith("分"):
            num = "".join(filter(str.isdigit, t))
            if num.isdigit():
                duration = int(num)
                break
    print(json.dumps({"contest_id":current,"duration_min":duration}, ensure_ascii=False, separators=(',',':')))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--contest",      help="対象 Contest ID を指定（省略時は開催中自動検出）")
    parser.add_argument("--contest-json", action="store_true",
                        help="contest_id と duration_min のみ JSON 出力")
    parser.add_argument("--no-login",     action="store_true",
                        help="ログイン処理をスキップ（過去コンテスト更新用）")
    args = parser.parse_args()

    if args.contest_json:
        json_only(args.contest)
    else:
        full_translate(args.contest, args.no_login)
