import os
import sys
import requests
from bs4 import BeautifulSoup

# Playwright 周り
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

BASE_URL = "https://onlinemathcontest.com"


def fetch_static_html(url: str) -> str:
    """
    requests で静的に URL を GET して HTML を返す。
    """
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/114.0.0.0 Safari/537.36"
    })
    r = sess.get(url)
    r.raise_for_status()
    return r.text


def debug_problem_page(contest_id: str, task_id: str):
    """
    contest_id, task_id を受け取り、主に以下を確認する：
      1) 静的取得した HTML に <div id="problem_content"> が存在し、中身が何か
      2) 同 HTML 内に 'const content = "..."' を含む <script> があるか
      3) もし Playwright が使えるなら、Playwright でページをレンダリングして
         get 'div#problem_content' の innerHTML を取得できるか
    """
    url = f"{BASE_URL}/contests/{contest_id}/tasks/{task_id}"
    print(f"\n=== デバッグ対象 URL: {url} ===\n")

    # 1) 静的 HTML を取得
    try:
        html = fetch_static_html(url)
        print("[STEP 1] requests で静的取得した HTML の一部を表示（先頭 500 文字）")
        print(html[:500].replace("\n", "\\n\n"))
        print("…\n")
    except Exception as e:
        print(f"[Error] requests で HTML を取得できませんでした: {e}")
        return

    # 2) BeautifulSoup で <div id="problem_content"> を探す
    soup = BeautifulSoup(html, "html.parser")
    prob_div = soup.find("div", id="problem_content")
    if prob_div is None:
        print("[STEP 2] <div id=\"problem_content\"> は HTML に存在しませんでした。\n")
    else:
        inner = prob_div.decode_contents().strip()
        if not inner:
            print("[STEP 2] <div id=\"problem_content\"> は HTML に存在しますが、中身は空です（innerHTML は空文字）。\n")
        else:
            print("[STEP 2] <div id=\"problem_content\"> 内の静的な innerHTML（先頭 200 文字）：")
            print(inner[:200].replace("\n", "\\n\n"))
            print("…\n")

    # 3) BeautifulSoup から 'const content' を含む <script> タグを探す
    scripts = soup.find_all("script")
    found_content_js = False
    for sc in scripts:
        txt = sc.string
        if txt and "const content" in txt:
            found_content_js = True
            # 先頭 200 文字だけ出力して位置を確認
            snippet = txt.strip()[:200]
            print("[STEP 3] 'const content' を含む <script> タグの先頭 200 文字：")
            print(snippet.replace("\n", "\\n\n"))
            print("…\n")
            break

    if not found_content_js:
        print("[STEP 3] HTML 内に 'const content' を含む <script> タグが見つかりませんでした。\n")
    else:
        print("[STEP 3] JavaScript 内に問題文を埋め込む元の変数 `content` が存在しています。\n")

    # 4) Playwright が使えるなら、headless でレンダリングしてから div#problem_content を取得する
    if PLAYWRIGHT_AVAILABLE:
        print("[STEP 4] Playwright が利用可能です。実際にブラウザでレンダリングを試みます…")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                # URL に行って JS の実行を待機
                page.goto(url, wait_until="networkidle")

                # 2 秒だけ待ってみる（JSレンダリングが終わるまで余裕を持たせる）
                try:
                    page.wait_for_selector("#problem_content", timeout=5000)
                except PlaywrightTimeoutError:
                    print("[STEP 4] タイムアウト: #problem_content が現れませんでした (5秒経過)。")
                    browser.close()
                    return

                # innerHTML を取得
                inner_html = page.eval_on_selector("#problem_content", "el => el.innerHTML")
                browser.close()

                if not inner_html or not inner_html.strip():
                    print("[STEP 4] Playwright でレンダリング後も <div id=\"problem_content\"> の innerHTML は空でした。")
                else:
                    print("[STEP 4] Playwright でレンダリング後の <div id=\"problem_content\"> innerHTML（先頭 200 文字）：")
                    print(inner_html.strip()[:200].replace("\n", "\\n\n"))
                    print("…")
        except Exception as e:
            print(f"[STEP 4] Playwright 実行中に例外が発生しました: {e}\n")
    else:
        print("[STEP 4] Playwright がインストールされていないためスキップします。\n")


def debug_editorial_page(contest_id: str, task_id: str):
    """
    contest_id, task_id を受け取り、主に以下を確認する：
      1) 静的 HTML に <div id="editorial_content"> が存在し、中身が何か
      2) HTML 内に 'const content' を含む <script> があるか
      3) もし Playwright が使えるなら、ブラウザレンダリング後の <div id="editorial_content"> を取得できるか
    """
    url = f"{BASE_URL}/contests/{contest_id}/editorial/{task_id}"
    print(f"\n=== デバッグ対象 URL: {url} ===\n")

    # 1) 静的 HTML を取得
    try:
        html = fetch_static_html(url)
        print("[STEP 1] requests で静的取得した HTML の一部を表示（先頭 500 文字）")
        print(html[:500].replace("\n", "\\n\n"))
        print("…\n")
    except Exception as e:
        print(f"[Error] requests で HTML を取得できませんでした: {e}")
        return

    # 2) BeautifulSoup で <div id="editorial_content"> を探す
    soup = BeautifulSoup(html, "html.parser")
    edi_div = soup.find("div", id="editorial_content")
    if edi_div is None:
        print("[STEP 2] <div id=\"editorial_content\"> は HTML に存在しませんでした。\n")
    else:
        inner = edi_div.decode_contents().strip()
        if not inner:
            print("[STEP 2] <div id=\"editorial_content\"> は HTML に存在しますが、中身は空です（innerHTML は空文字）。\n")
        else:
            print("[STEP 2] <div id=\"editorial_content\"> 内の静的な innerHTML（先頭 200 文字）：")
            print(inner[:200].replace("\n", "\\n\n"))
            print("…\n")

    # 3) BeautifulSoup から 'const content' を含む <script> タグを探す
    scripts = soup.find_all("script")
    found_content_js = False
    for sc in scripts:
        txt = sc.string
        if txt and "const content" in txt:
            found_content_js = True
            snippet = txt.strip()[:200]
            print("[STEP 3] 'const content' を含む <script> タグの先頭 200 文字：")
            print(snippet.replace("\n", "\\n\n"))
            print("…\n")
            break

    if not found_content_js:
        print("[STEP 3] HTML 内に 'const content' を含む <script> タグが見つかりませんでした。\n")
    else:
        print("[STEP 3] JavaScript 内に解説文を埋め込む元の `content` 変数が存在しています。\n")

    # 4) Playwright で要素をレンダリングして innerHTML を取れるか試す
    if PLAYWRIGHT_AVAILABLE:
        print("[STEP 4] Playwright が利用可能です。レンダリングを試みます…")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                page.goto(url, wait_until="networkidle")

                try:
                    page.wait_for_selector("#editorial_content", timeout=5000)
                except PlaywrightTimeoutError:
                    print("[STEP 4] タイムアウト: #editorial_content が現れませんでした (5秒経過)。")
                    browser.close()
                    return

                inner_html = page.eval_on_selector("#editorial_content", "el => el.innerHTML")
                browser.close()

                if not inner_html or not inner_html.strip():
                    print("[STEP 4] Playwright でレンダリング後も <div id=\"editorial_content\"> の innerHTML は空でした。")
                else:
                    print("[STEP 4] Playwright でレンダリング後の <div id=\"editorial_content\"> innerHTML（先頭 200 文字）：")
                    print(inner_html.strip()[:200].replace("\n", "\\n\n"))
                    print("…")
        except Exception as e:
            print(f"[STEP 4] Playwright 実行中に例外が発生しました: {e}\n")
    else:
        print("[STEP 4] Playwright がインストールされていないためスキップします。\n")


def main():
    if len(sys.argv) < 3:
        print("使い方: python debug_problem_page.py {contest_id} {task_id}")
        print("例: python debug_problem_page.py omc251 12373")
        sys.exit(1)

    contest_id = sys.argv[1]
    task_id    = sys.argv[2]

    print(f"■ 問題ページ ({contest_id}/{task_id}) をデバッグします ■")
    debug_problem_page(contest_id, task_id)

    print(f"\n■ 解説ページ ({contest_id}/{task_id}) をデバッグします ■")
    debug_editorial_page(contest_id, task_id)


if __name__ == "__main__":
    main()
