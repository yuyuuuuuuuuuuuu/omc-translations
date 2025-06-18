import subprocess
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://onlinemathcontest.com"

def get_past3_contests() -> list[str]:
    """トップページから過去３コンテストのIDを取得して返す"""
    out = []
    page = 1
    while True:
        url = f"{BASE_URL}/contests/all?page={page}"
        resp = requests.get(url); resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        tbl = soup.find("div", class_="table-responsive")
        if not tbl:
            break
        names = [a["href"].split("/contests/")[1].strip()
                 for a in tbl.select("a[href*='/contests/']")]
        if not names:
            break
        out.extend(names)
        if len(out) >= 3:
            break
        page += 1
    return out[:3]

def run(cmd: str):
    """
    コマンドを表示して shell 実行。
    失敗したら警告を表示する。
    """
    print(f"→ 実行: {cmd}")
    res = subprocess.run(cmd, shell=True)
    if res.returncode != 0:
        print(f"[Warning] コマンド失敗: {cmd}")

def main():
    # 過去 3 コンテスト分だけ処理
    contests = get_past3_contests()
    for c in contests:
        # (1) 問題文翻訳（過去コンテストはログイン不要モード）
        run(f"python3 scripts/fetch_and_translate.py --contest {c} --no-login")
        # (2) 公式解説翻訳
        run(f"python3 scripts/fetch_editorial.py --contest {c}")
        # (3) ユーザー解説翻訳
        run(f"python3 scripts/update_user_editorials.py --contest {c}")

if __name__ == "__main__":
    main()