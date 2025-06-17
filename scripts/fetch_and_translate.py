#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import requests
import subprocess
import argparse
from pathlib import Path
from bs4 import BeautifulSoup

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Browser
import openai

# ───────────────────────────────────────────────────────────
# 1) 設定項目
# ───────────────────────────────────────────────────────────

BASE_URL        = "https://onlinemathcontest.com"
HOMEPAGE_URL    = BASE_URL + "/"
LANG_CONFIG_PATH = Path(__file__).parents[1] / "languages" / "config.json"
OUTPUT_ROOT     = Path(__file__).parents[1] / "languages"

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
# 2) 共通ヘルパー関数
# ───────────────────────────────────────────────────────────

def fetch_url_html(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent":"Mozilla/5.0"})
    resp.raise_for_status()
    return resp.text

def find_current_contest(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for header in soup.select("div.contest-header"):
        status = header.find("div", class_="contest-status")
        if status and "開催中" in status.get_text():
            sib = header.find_next_sibling()
            while sib:
                if sib.name=="a" and "contest-name" in sib.get("class", []):
                    return sib["href"].rstrip("/").split("/")[-1]
                sib = sib.find_next_sibling()
    return None

def login_omc_with_playwright(page: Page) -> bool:
    page.goto(BASE_URL+"/login", wait_until="networkidle")
    try:
        page.wait_for_selector("form[action='https://onlinemathcontest.com/login']", timeout=8000)
    except PlaywrightTimeoutError:
        print("[Error] ログインフォームが見つかりませんでした。")
        return False
    token = page.get_attribute("input[name='_token']", "value")
    if not token:
        print("[Error] CSRF トークン取得失敗。")
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

def fetch_task_ids_playwright(page: Page, contest_id: str) -> list[str]:
    url = f"{BASE_URL}/contests/{contest_id}"
    page.goto(url, wait_until="networkidle")
    html = page.content()
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

def extract_div_innerhtml_with_playwright(page: Page, url: str, div_id: str, timeout: int=8000) -> str:
    try:
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector(f"#{div_id}", timeout=timeout)
        return page.eval_on_selector(f"#{div_id}", "el => el.innerHTML") or ""
    except Exception:
        return ""

def save_jp_problem(contest_id: str, task_id: str, page: Page) -> Path | None:
    out_dir = OUTPUT_ROOT/"ja"/"contests"/contest_id/"tasks"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir/f"{task_id}.html"
    if out_path.exists():
        return out_path
    url = f"{BASE_URL}/contests/{contest_id}/tasks/{task_id}"
    html = extract_div_innerhtml_with_playwright(page,url,"problem_content")
    if not html.strip():
        print(f"[Warning] {contest_id}/{task_id} の problem_content 取得失敗")
        return None
    out_path.write_text(html, encoding="utf-8")
    print(f"[Saved JP] {out_path}")
    return out_path

def HtmlKatex(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.select(".katex"):
        ann = katex.find("annotation",{"encoding":"application/x-tex"})
        if ann:
            katex.replace_with(f"${ann.text}$")
    return str(soup)

def ask_gpt(question: str, model: str, term: str) -> str:
    resp = openai.ChatCompletion.create(
        model=model,
        messages=[
            {"role":"system","content":(
                f"The HTML you will receive contains KaTeX math {term} in Japanese.\n"
                "Translate all sentences into the target language, preserving HTML and KaTeX markup.\n"
                "Return only the translated HTML."
            )},
            {"role":"user","content":question}
        ],
        temperature=0.0
    )
    return resp.choices[0].message.content.strip()

def translate_html_for_lang(jp_html: str, term: str, lang: str) -> str:
    latex = HtmlKatex(jp_html)
    return ask_gpt(latex, GPT_MODEL, term)

def render_html_with_playwright(page: Page, path: Path):
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
    orig = path.read_text(encoding="utf-8")
    wrapped = header + orig + "\n</body></html>"
    path.write_text(wrapped, encoding="utf-8")
    page.goto("file://"+str(path.resolve()), wait_until="networkidle")
    page.wait_for_load_state("networkidle")
    body = BeautifulSoup(page.content(),"html.parser").body.decode_contents()
    final = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.0/dist/katex.min.css">
</head><body>
{body}
</body></html>"""
    path.write_text(final, encoding="utf-8")

def change_problem_display(contest_id: str, task_id: str, lang: str="en"):
    path = OUTPUT_ROOT/lang/"contests"/contest_id/"tasks"/f"{task_id}.html"
    if not path.exists(): return
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    for elt in soup.select("span.katex-display"):
        wrapper = soup.new_tag("div", style="text-align:center;")
        elt.wrap(wrapper)
    path.write_text(str(soup), encoding="utf-8")


# ───────────────────────────────────────────────────────────
# 3) 本体
# ───────────────────────────────────────────────────────────

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import json
import subprocess
import argparse
from pathlib import Path
from bs4 import BeautifulSoup
import requests
import openai
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Browser

BASE_URL        = "https://onlinemathcontest.com"
HOMEPAGE_URL    = BASE_URL + "/"
LANG_CONFIG_PATH = Path(__file__).parents[1] / "languages" / "config.json"
OUTPUT_ROOT     = Path(__file__).parents[1] / "languages"

# （省略：各種 helper 関数（login_omc_with_playwright, find_current_contest, fetch_task_ids_playwright,
#  extract_div_innerhtml_with_playwright, save_jp_problem, HtmlKatex, ask_gpt, translate_html_for_lang,
#  render_html_with_playwright, change_problem_display, json_only）を先の版からそのままコピペしてください）

def full_translate(contest_override: str|None):
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()

        # ログイン & コンテストID検出
        if not login_omc_with_playwright(page):
            sys.exit(1)
        page.goto(HOMEPAGE_URL, wait_until="networkidle")
        current = contest_override or find_current_contest(page.content())
        if not current:
            print("[Info] 開催中コンテストなし")
            print(json.dumps({"contest_id":"", "duration_min":0}, ensure_ascii=False, separators=(',',':')))
            browser.close()
            sys.exit(0)
        print(f"→ 対象 Contest ID = {current}")

        # タスク一覧取得
        task_ids = fetch_task_ids_playwright(page, current)
        print(f"→ {current} のタスク一覧 = {task_ids}")

        # 日本語版を保存
        for tid in task_ids:
            save_jp_problem(current, tid, page)

        # 言語順の読み込み（config.json の ["en", 他...]）
        langs = json.loads(LANG_CONFIG_PATH.read_text(encoding="utf-8")).get("languages", [])
        ordered = ["en"] + [l for l in langs if l!="en"]

        # ── ① 英語まとめて翻訳 & コミット
        for tid in task_ids:
            jp_path = OUTPUT_ROOT/"ja"/"contests"/current/"tasks"/f"{tid}.html"
            out_en = OUTPUT_ROOT/"en"/"contests"/current/"tasks"/f"{tid}.html"
            if not out_en.exists():
                # ディレクトリ作成
                out_en.parent.mkdir(parents=True, exist_ok=True)
                # 翻訳結果を書き込み
                translated = translate_html_for_lang(jp_path.read_text(encoding="utf-8"), "task", "en")
                out_en.write_text(translated, encoding="utf-8")
                # KaTeX レンダリング & 中央寄せ
                render_html_with_playwright(page, out_en)
                change_problem_display(current, tid, "en")
                # Git commit & push
                try:
                    subprocess.run(["git","config","--local","user.name","github-actions[bot]"], check=True)
                    subprocess.run(["git","config","--local","user.email","github-actions[bot]@users.noreply.github.com"], check=True)
                    subprocess.run(["git","add", str(out_en)], check=True)
                    subprocess.run(["git","commit","-m", f"Add {out_en}"], check=True)
                    subprocess.run(["git","push","origin","HEAD:main"], check=True)
                    print(f"[Git] {out_en} を commit & push")
                except subprocess.CalledProcessError as e:
                    print(f"[Error] Git 操作中に例外発生: {e}")

        # ── ② その他言語 (インタリーブ) 翻訳 & コミット
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
                        subprocess.run(["git","config","--local","user.name","github-actions[bot]"], check=True)
                        subprocess.run(["git","config","--local","user.email","github-actions[bot]@users.noreply.github.com"], check=True)
                        subprocess.run(["git","add", str(out_p)], check=True)
                        subprocess.run(["git","commit","-m", f"Add {out_p}"], check=True)
                        subprocess.run(["git","push","origin","HEAD:main"], check=True)
                        print(f"[Git] {out_p} を commit & push")
                    except subprocess.CalledProcessError as e:
                        print(f"[Error] Git 操作中に例外発生: {e}")

        # ── JSON 出力用に duration_min を取得
        html_ct = fetch_url_html(f"{BASE_URL}/contests/{current}")
        soup2 = BeautifulSoup(html_ct, "html.parser")
        duration = 60
        for p in soup2.select("p.list-group-item-heading"):
            t = p.get_text(strip=True)
            if t.endswith("分"):
                num = "".join(filter(str.isdigit, t))
                if num.isdigit():
                    duration = int(num)
                    break

        print(json.dumps({"contest_id":current, "duration_min":duration}, ensure_ascii=False, separators=(',',':')))
        browser.close()



def json_only(contest_override: str|None):
    # contest_id の検出
    if contest_override:
        current = contest_override
    else:
        html = fetch_url_html(HOMEPAGE_URL)
        current = find_current_contest(html)
    if not current:
        print(json.dumps({"contest_id":"", "duration_min":0}, ensure_ascii=False, separators=(',',':')))
        return

    # duration_min の検出
    html_ct = fetch_url_html(f"{BASE_URL}/contests/{current}")
    soup2 = BeautifulSoup(html_ct, "html.parser")
    duration = 60
    for p in soup2.select("p.list-group-item-heading"):
        t = p.get_text(strip=True)
        if t.endswith("分"):
            num = "".join(filter(str.isdigit, t))
            if num.isdigit():
                duration = int(num)
                break

    print(json.dumps({"contest_id":current, "duration_min":duration}, ensure_ascii=False, separators=(',',':')))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--contest", help="対象の Contest ID 指定（省略時は開催中を自動検出）")
    parser.add_argument("--contest-json", action="store_true",
                        help="contest_id と duration_min のみ JSON 出力")
    args = parser.parse_args()

    if args.contest_json:
        json_only(args.contest)
    else:
        full_translate(args.contest)
