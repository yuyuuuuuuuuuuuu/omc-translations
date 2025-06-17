import subprocess
import requests
from bs4 import BeautifulSoup


def get_all_contests() -> list[str]:
    out, page = [], 1
    while True:
        r = requests.get(f"https://onlinemathcontest.com/contests/all?page={page}")
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.select("div.table-responsive a[href*='/contests/']")
        names = [a['href'].split('/contests/')[1].strip() for a in links]
        if not names:
            break
        out += names
        page += 1
    return out


def run(cmd: str):
    print(f"→ 実行: {cmd}")
    subprocess.run(cmd, shell=True)


def main():
    contests = get_all_contests()[:3]
    for c in contests:
        run(f"python3 scripts/fetch_and_translate.py --contest {c}")
        run(f"python3 scripts/fetch_editorial.py --contest {c}")

if __name__ == "__main__":
    main()