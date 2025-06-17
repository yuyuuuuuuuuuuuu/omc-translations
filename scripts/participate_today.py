import os
import sys
import requests
from bs4 import BeautifulSoup

USERNAME = os.getenv("OMC_USERNAME")
PASSWORD = os.getenv("OMC_PASSWORD")
if not (USERNAME and PASSWORD):
    print("[Error] OMC_USERNAME / OMC_PASSWORD を設定してください。")
    sys.exit(1)


def get_current_contests() -> list[str]:
    resp = requests.get("https://onlinemathcontest.com/")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for header in soup.select("div.contest-header"):
        status = header.select_one("div.contest-status")
        if status and "開催中" in status.get_text():
            sib = header.find_next_sibling()
            while sib:
                if sib.name == "a" and "contest-name" in sib.get("class", []):
                    out.append(sib["href"].rstrip("/").split("/")[-1])
                    break
                sib = sib.find_next_sibling()
    return out


def participate(username: str, password: str, contest: str) -> bool:
    session = requests.Session()
    # login
    login_url = "https://onlinemathcontest.com/login"
    r = session.get(login_url); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    token = soup.find("input", {"name":"_token"})["value"]
    payload = {"_token":token, "display_name":username, "password":password}
    r = session.post(login_url, data=payload, allow_redirects=True); r.raise_for_status()
    if r.url.endswith("/login"):
        return False
    # join
    url = f"https://onlinemathcontest.com/contests/{contest.lower()}"
    r = session.get(url); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", id="join_form")
    if not form:
        return False
    data = { inp["name"]: inp.get("value","") for inp in form.find_all("input",{"type":"hidden"}) if inp.get("name") }
    r = session.post(form["action"], data=data, allow_redirects=True); r.raise_for_status()
    return r.url.rstrip("/").endswith(f"/contests/{contest.lower()}")


def main():
    contests = get_current_contests()
    if not contests:
        print("参加可能なコンテストはありません。")
    for c in contests:
        try:
            ok = participate(USERNAME, PASSWORD, c)
            print(f"→ {c} 参加登録: {'成功' if ok else '失敗／既参加'}")
        except Exception as e:
            print(f"[Warning] {c} 参加中に例外: {e}")

if __name__ == "__main__":
    main()