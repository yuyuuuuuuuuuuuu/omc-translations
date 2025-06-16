#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import openai

# ───────────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────────
BASE_URL = "https://onlinemathcontest.com"
SCRIPT_DIR = Path(__file__).parent.resolve()
LANG_ROOT = SCRIPT_DIR.parent / "languages"
JA_ROOT = LANG_ROOT / "ja" / "contests"
EN_ROOT = LANG_ROOT / "en" / "contests"

OPENAI_MODEL = "gpt-4o-mini"
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("[Error] environment variable OPENAI_API_KEY is not set", file=sys.stderr)
    sys.exit(1)
openai.api_key = OPENAI_KEY

# ───────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────
def get_all_contests():
    i = 1
    out = []
    while True:
        url = f"{BASE_URL}/contests/all?page={i}"
        resp = requests.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("div", class_="table-responsive")
        if not table:
            break
        links = table.find_all("a", href=True)
        contest_names = []
        for a in links:
            href = a["href"]
            if "/contests/" not in href:
                continue
            name = href.split("/contests/")[1].strip().rstrip("/")
            contest_names.append(name)
        if not contest_names:
            break
        out.extend(contest_names)
        i += 1
    return sorted(set(out))

def html_to_tex_placeholders(html: str) -> str:
    """
    Replace KaTeX-rendered annotation tags with $...$ placeholders
    so that the model can re-insert them correctly.
    """
    soup = BeautifulSoup(html, "html.parser")
    for katex in soup.select(".katex"):
        ann = katex.find("annotation", {"encoding": "application/x-tex"})
        if ann:
            katex.replace_with(f"${ann.text}$")
    return str(soup)

def translate_html(jp_html: str, description: str="user editorial") -> str:
    """
    Send the Japanese HTML (with KaTeX placeholders) to OpenAI to translate into English.
    """
    prompt = (
        f"You are given HTML-formatted user editorial content in Japanese, "
        f"which may include KaTeX formulas marked as $...$.\n"
        f"Translate all visible Japanese into English, preserving every HTML tag "
        f"and KaTeX formula exactly (do not alter delimiters or class names).\n"
        f"Respond with only the translated HTML.\n\n"
        + html_to_tex_placeholders(jp_html)
    )
    resp = openai.ChatCompletion.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": prompt}],
        temperature=0.0,
    )
    return resp.choices[0].message.content

# ───────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────
def main():
    contests = get_all_contests()
    for contest in contests:
        editorial_url = f"{BASE_URL}/contests/{contest}/editorial"
        try:
            resp = requests.get(editorial_url)
            resp.raise_for_status()
        except Exception as e:
            print(f"[Warning] failed to fetch editorial page for {contest}: {e}", file=sys.stderr)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        container = soup.find("div", id="editorials")
        if not container:
            continue

        # find all user-editorial links (exclude official "公式解説")
        for a in container.find_all("a", href=True):
            text = a.get_text(strip=True)
            if "公式解説" in text:
                continue

            href = a["href"]
            parts = href.rstrip("/").split("/")
            # expected: ["", "contests", "{contest}", "editorial", "{task}", "{user_id}"]
            if len(parts) < 6 or parts[-3] != "editorial":
                continue
            task_id = parts[-2]
            user_id = parts[-1]

            # paths
            ja_dir = JA_ROOT / contest / "user_editorial"
            ja_file = ja_dir / f"{user_id}.html"
            en_dir = EN_ROOT / contest / "user_editorial"
            en_file = en_dir / f"{user_id}.html"

            if ja_file.exists():
                print(f"[Skip JA] {ja_file.relative_to(SCRIPT_DIR)} exists")
            else:
                # fetch and save Japanese editorial
                try:
                    page_url = BASE_URL + href
                    r2 = requests.get(page_url)
                    r2.raise_for_status()
                    page_soup = BeautifulSoup(r2.text, "html.parser")
                    content_div = page_soup.find("div", id="editorial_content")
                    if not content_div:
                        print(f"[Warning] no #editorial_content in {page_url}", file=sys.stderr)
                        continue
                    html_inner = content_div.decode_contents()
                    ja_dir.mkdir(parents=True, exist_ok=True)
                    ja_file.write_text(html_inner, encoding="utf-8")
                    print(f"[Saved JA] {ja_file.relative_to(SCRIPT_DIR)}")
                except Exception as e:
                    print(f"[Error] fetching editorial_content for {page_url}: {e}", file=sys.stderr)
                    continue

            if en_file.exists():
                print(f"[Skip EN] {en_file.relative_to(SCRIPT_DIR)} exists")
            else:
                # translate and save English editorial
                try:
                    jp_html = ja_file.read_text(encoding="utf-8")
                    translated = translate_html(jp_html)
                    en_dir.mkdir(parents=True, exist_ok=True)
                    en_file.write_text(translated, encoding="utf-8")
                    print(f"[Saved EN] {en_file.relative_to(SCRIPT_DIR)}")
                except Exception as e:
                    print(f"[Error] translating {ja_file.relative_to(SCRIPT_DIR)}: {e}", file=sys.stderr)
                    time.sleep(5)  # rate-limit backoff
                    continue

if __name__ == "__main__":
    main()