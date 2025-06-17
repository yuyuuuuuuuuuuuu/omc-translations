import os
import sys
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

# 環境変数からログイン情報
USERNAME = os.getenv("OMC_USERNAME")
PASSWORD = os.getenv("OMC_PASSWORD")
if not (USERNAME and PASSWORD):
    print("[Error] OMC_USERNAME / OMC_PASSWORD を設定してください。")
    sys.exit(1)

# JST タイムゾーン定義
ojst = timezone(timedelta(hours=9))


def get_today_contests() -> list[str]:
    """
    ホームページから「今日開催予定」のコンテストID一覧を取得する。
    """
    resp = requests.get("https://onlinemathcontest.com/")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    today = datetime.now(ojst).date()
    out: list[str] = []
    # 各コンテストヘッダーを走査
    for header in soup.select("div.contest-header"):
        # 日付を示す <span class="contest-time text-muted"> を取得
        time_tag = header.select_one("span.contest-time.text-muted")
        if not time_tag:
            continue
        # 例: "2025-06-17 (Tue) 21:00"
        text = time_tag.get_text(strip=True)
        date_part = text.split()[0]
        try:
            contest_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            continue
        # 今日の日付のみ残す
        if contest_date != today:
            continue
        # 続く <a class="contest-name" href="..."> から contest_id を取得
        sib = header.find_next_sibling()
        while sib:
            if sib.name == "a" and "contest-name" in sib.get("class", []):
                cid = sib["href"].rstrip("/").split("/")[-1]
                out.append(cid)
                break
            sib = sib.find_next_sibling()
    return out


def participate(username: str, password: str, contest: str) -> bool:
    """
    指定コンテストへの参加登録を行う。成功時 True。
    """
    session = requests.Session()
    # STEP 1: CSRF トークンを取得
    login_url = "https://onlinemathcontest.com/login"
    r = session.get(login_url); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", attrs={"name": "_token"})
    if not token_input or not token_input.has_attr("value"):
        raise RuntimeError("CSRF トークンが取得できませんでした。")
    csrf_token = token_input["value"]

    # STEP 2: ログイン POST
    payload = {"_token": csrf_token, "display_name": username, "password": password}
    r = session.post(login_url, data=payload, allow_redirects=True); r.raise_for_status()
    if r.url.endswith("/login"):
        return False

    # STEP 3: 参加フォームを探して POST
    url = f"https://onlinemathcontest.com/contests/{contest.lower()}"
    r = session.get(url); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", id="join_form")
    if not form:
        return False
    data = { inp.get("name"): inp.get("value", "")
             for inp in form.find_all("input", {"type": "hidden"})
             if inp.get("name") }
    r = session.post(form["action"], data=data, allow_redirects=True); r.raise_for_status()
    return r.url.rstrip("/").endswith(f"/contests/{contest.lower()}")


def main():
    # 今日開催予定のコンテストを取得
    contests = get_today_contests()
    if not contests:
        print("[Info] 今日開催予定のコンテストはありません。")
        return
    # 各コンテストに参加登録（エラーは無視）
    for c in contests:
        try:
            ok = participate(USERNAME, PASSWORD, c)
            print(f"→ {c} 参加登録: {'成功' if ok else '失敗／既参加'}")
        except Exception as e:
            print(f"[Warning] {c} 参加登録中に例外: {e}")

if __name__ == "__main__":
    main()