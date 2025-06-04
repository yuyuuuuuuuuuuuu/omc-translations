# your-repo/scripts/fetch_latest_ended.py

import os
import sys
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ───────────────────────────────────────────────────────────
# 定数定義
# ───────────────────────────────────────────────────────────
BASE_URL       = "https://onlinemathcontest.com"
HOMEPAGE_URL   = BASE_URL + "/"
# 保存先ルート: languages/ja/contests/{contest_id}/{tasks or editorial}/{task_id}.html
OUTPUT_ROOT    = os.path.normpath(os.path.join(
    os.path.dirname(__file__),
    os.pardir,            # scripts/ の一つ上へ
    "languages", "ja", "contests"
))


def fetch_url_html(url: str) -> str:
    """
    requests を使って指定 URL を GET し、HTML を文字列で返す。
    エラーがあれば例外を投げる。
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/114.0.0.0 Safari/537.36"
    })
    resp = session.get(url)
    resp.raise_for_status()
    return resp.text


def find_latest_ended_contest(html: str) -> str:
    """
    トップページ HTML を BeautifulSoup で解析し、
    最も上部にある「終了済」コンテストの contest_id を返す。
    見つからなければ None を返す。
    """
    soup = BeautifulSoup(html, "html.parser")

    # すべての <div class="contest-header"> を順に調べる
    for header_div in soup.select("div.contest-header"):
        status_div = header_div.find("div", class_="contest-status")
        if not status_div:
            continue
        if "終了済" not in status_div.get_text(strip=True):
            continue

        # 「終了済」を含む header_div を見つけたら、その兄弟要素から
        # <a class="contest-name" href=".../contests/{contest_id}"> を探す
        sib = header_div.find_next_sibling()
        a_tag = None
        while sib:
            if sib.name == "a" and "contest-name" in sib.get("class", []):
                a_tag = sib
                break
            sib = sib.find_next_sibling()

        if a_tag:
            href = a_tag.get("href", "").strip()
            if href:
                contest_id = href.rstrip("/").split("/")[-1]
                return contest_id

    return None


def fetch_task_ids(contest_id: str) -> list[str]:
    """
    /contests/{contest_id} ページを requests で取得し、
    href に "/contests/{contest_id}/tasks/" を含む <a> タグをすべて拾って
    task_id のリストを返す。見つからなければ空リストを返す。
    """
    url = f"{BASE_URL}/contests/{contest_id}"
    try:
        html = fetch_url_html(url)
    except Exception as e:
        print(f"[Error] {contest_id} ページ取得に失敗: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    task_ids: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if f"/contests/{contest_id}/tasks/" in href:
            parts = href.rstrip("/").split("/")
            if len(parts) >= 2 and parts[-2] == "tasks" and parts[-1].isdigit():
                task_ids.append(parts[-1])

    return sorted(set(task_ids), key=lambda x: int(x))


def extract_div_innerhtml_with_playwright(url: str, div_id: str, timeout: int = 8000) -> str:
    """
    Playwright の headless Chromium を使って指定 URL にアクセスし、
    <div id="{div_id}"> が出現するまで待機。見つかればその innerHTML を返す。
    失敗すれば空文字を返す。
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            # NetworkIdle まで待つことで JS の marked() 実行を期待
            page.goto(url, wait_until="networkidle")
            try:
                page.wait_for_selector(f"#{div_id}", timeout=timeout)
            except PlaywrightTimeoutError:
                print(f"[Timeout] {url} に #{div_id} が現れませんでした。")
                browser.close()
                return ""
            # innerHTML を直接取得
            inner_html = page.eval_on_selector(f"#{div_id}", "el => el.innerHTML")
            browser.close()
            return inner_html or ""
    except Exception as e:
        print(f"[Error] Playwright で {url} を処理中に例外: {e}")
        return ""


def save_div_innerhtml(contest_id: str, task_id: str, subdir: str, div_id: str):
    """
    contest_id, task_id, subdir ("tasks" or "editorial"), div_id を指定すると、
      URL = https://onlinemathcontest.com/contests/{contest_id}/{subdir}/{task_id}
    に Playwright でアクセスし、<div id="{div_id}"> の innerHTML を
      languages/ja/contests/{contest_id}/{subdir}/{task_id}.html
    に保存する。すでにファイルがあればスキップ。
    """

    # 保存先フォルダを作成
    folder = os.path.join(OUTPUT_ROOT, contest_id, subdir)
    os.makedirs(folder, exist_ok=True)

    file_path = os.path.join(folder, f"{task_id}.html")
    if os.path.exists(file_path):
        print(f"[Skip] {contest_id}/{subdir}/{task_id}.html は既に存在します。")
        return

    # Playwright で innerHTML 抽出
    url = f"{BASE_URL}/contests/{contest_id}/{subdir}/{task_id}"
    print(f"[Fetch] {url} → 抽出中 ({div_id}) …")
    inner_html = extract_div_innerhtml_with_playwright(url, div_id)

    if not inner_html.strip():
        print(f"[Warning] {contest_id}/{subdir}/{task_id} の <div id=\"{div_id}\"> が空、もしくは抽出に失敗しました。")
        # 空でも一応ファイルを作っておくか、スキップするかは好みで選べます。ここでは「スキップ」しておく
        return

    # ファイルに保存
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(inner_html)

    print(f"[Saved] {contest_id}/{subdir}/{task_id}.html (length={len(inner_html)} bytes)")


def main():
    # 1) トップページ取得 → 最新の終了済コンテストを探す
    try:
        homepage_html = fetch_url_html(HOMEPAGE_URL)
    except Exception as e:
        print("[Error] トップページの取得に失敗しました:", e)
        sys.exit(1)

    latest_ended = find_latest_ended_contest(homepage_html)
    if latest_ended is None:
        print("最新の終了済コンテストが見つかりませんでした。")
        return

    print("最新の終了済コンテスト ID =", latest_ended)

    # 2) すでに取得済みかチェック (tasks フォルダにファイルがあれば OK)
    fetched_tasks_dir = os.path.join(OUTPUT_ROOT, latest_ended, "tasks")
    if os.path.isdir(fetched_tasks_dir) and os.listdir(fetched_tasks_dir):
        print(f"{latest_ended} のタスクは既に取得済み → {fetched_tasks_dir}")
    else:
        # 3) タスク一覧を取得 → 各タスクの problem_content を Playwright で抜き出して保存
        task_ids = fetch_task_ids(latest_ended)
        if not task_ids:
            print(f"{latest_ended} にタスクが 1 つも見つかりませんでした。")
            return

        print(f"{latest_ended} のタスクID 一覧 = {task_ids}")
        for tid in task_ids:
            save_div_innerhtml(latest_ended, tid, subdir="tasks", div_id="problem_content")

    # 4) 同じく editorial を取得 (editorial フォルダに何も入っていなければ実行)
    fetched_editorials_dir = os.path.join(OUTPUT_ROOT, latest_ended, "editorial")
    if os.path.isdir(fetched_editorials_dir) and os.listdir(fetched_editorials_dir):
        print(f"{latest_ended} のエディトリアルは既に取得済み → {fetched_editorials_dir}")
    else:
        # タスク一覧を再取得 or 先ほどの task_ids を流用（冗長性を省くため再取得しても良い）
        task_ids = fetch_task_ids(latest_ended)
        if not task_ids:
            print(f"{latest_ended} にタスクが 1 つも見つかりません → エディトリアルもスキップ")
            return

        print(f"{latest_ended} のエディトリアルを取得開始 (task IDs = {task_ids})")
        for tid in task_ids:
            save_div_innerhtml(latest_ended, tid, subdir="editorial", div_id="editorial_content")

    print("最新の終了済コンテストの「タスク」「エディトリアル」取得が完了しました。")


if __name__ == "__main__":
    main()
