# OMC Translations

**日本語は下にあります**

---

## Overview
This repository automatically fetches, translates and publishes problem statements, official editorials, and selected user editorials from [Online Math Contest](https://onlinemathcontest.com) (OMC).  
All content is translated from Japanese into English (and optionally other languages) while preserving full KaTeX math markup.

The whole pipeline runs every day on GitHub Actions:

| JST Time | Phase | Job / Script |
|----------|-------|--------------|
| **20:00** | Log-in & contest registration | `scripts/participate_today.py` |
| **21:00** | Fetch & translate **tasks**<br> • EN first (task-wise commits)<br> • other languages interleaved | `scripts/fetch_and_translate.py` |
| 21:00 + _duration_ | Wait until contest ends | internal helper |
| end-time | Fetch & translate **official editorials** | `scripts/fetch_editorial.py` |
| end-time + few min | Fetch & translate **past 3 contests** (tasks & editorials) | `scripts/update_past3.py` |
| midnight (if day % 7 == 0) | Update **all user editorials** across all contests | `scripts/update_user_editorials.py` |

Everything is committed & pushed task-by-task (or editorial-by-editorial) so the repository stays in sync and readers can preview translations early.

---

## Repository layout
```
languages/
├─ config.json          # \["en", "fr", ...]
├─ en/                  # translated output
│    contests/OMC001/...
└─ ja/                  # original Japanese HTML
scripts/
├─ participate\_today.py
├─ fetch\_and\_translate.py
├─ fetch\_editorial.py
├─ update\_past3.py
├─ update\_user\_editorials.py
├─ orchestrate\_daily.py   # local end-to-end runner (used by GHA)
└─ translate\_specific\_remote.py
.github/workflows/
└─ daily.yml             # 20:00 JST schedule
```

````
---

## Requirements (local)

| Tool | Purpose |
|------|---------|
| **Python 3.10+** | main scripts |
| **Playwright (Chromium)** | headless browser to execute site JS & render KaTeX |
| **OpenAI Python SDK** | translation |

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
````

---

## Environment variables

| Variable                        | Description                          |
| ------------------------------- | ------------------------------------ |
| `OPENAI_API_KEY`                | GPT-4o-mini key                      |

GitHub Actions secrets of the same names are already configured.

---

## How to run manually

```bash
# 1. register & translate tonight's contest (all phases)
python scripts/orchestrate_daily.py

# 2. translate a single task / editorial / user editorial
python scripts/translate_specific_remote.py task omcb047 11404
python scripts/translate_specific_remote.py editorial omcb047 10982
python scripts/translate_specific_remote.py user_editorial omcb047 10982 --user_id 135

# 3. fix existing HTMLs that still contain raw "$" KaTeX
python scripts/bulk_fix_tasks_editorials.py          # tasks + official editorials
python scripts/bulk_fix_user_editorials.py           # user editorials
```

`translate_specific_remote.py` fetches live **JS variable `content`** directly from the site, converts Markdown-like `*italic*`, `**bold**`, line `***` (→ `<hr>`) to proper HTML, runs GPT translation, re-renders KaTeX in a headless browser, and overwrites the corresponding file.

---

## Adding a new language

1. Create `languages/<lang>/` with a `contests` sub-folder (can be empty).
2. Edit `languages/config.json` and append the ISO code, e.g.

```json
{ "languages": ["en", "fr", "de"] }
```

That’s all — the daily workflow will start producing `<lang>` translations in the same interleaved order.

---

## License

All original problem statements and editorials are © OMC authors.
The translation code is MIT-licensed (see `LICENSE`).
Translated HTML is distributed for educational / archival purposes only.

---

# 日本語版

## 概要

このリポジトリは [Online Math Contest](https://onlinemathcontest.com)（OMC）の
**問題文・公式解説・ユーザー解説** を自動取得し、多言語（主に英語）へ翻訳して公開します。
数式は KaTeX を保持したまま GPT‐4o-mini で翻訳し、ブラウザで再レンダリングします。

毎日 GitHub Actions で以下のフローを実行します：

| 日本時間                 | 処理                            | スクリプト                       |
| -------------------- | ----------------------------- | --------------------------- |
| **20:00**            | プログラム開始              | `participate_today.py`      |
| **21:00**            | 問題文取得＋翻訳 (タスク単位で commit/push) | `fetch_and_translate.py`    |
| 終了時刻まで               | wait                          | 内部                          |
| 終了時刻                 | 公式解説取得＋翻訳                     | `fetch_editorial.py`        |
| 直後                   | 直近3コンテストの更新                   | `update_past3.py`           |
| その日の深夜 (日付 % 7 == 0) | 全コンテストのユーザー解説更新               | `update_user_editorials.py` |

---

## ディレクトリ構成

```
languages/       # 言語別 HTML 置き場
  en/            # 翻訳済
  ja/            # 日本語原文
scripts/         # 自動化スクリプト
.github/workflows/daily.yml  # 毎日20:00(JST)実行
```

---

## 必要ツール

* Python 3.10 以上
* Playwright (Chromium)
* OpenAI Python SDK

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## 環境変数

| 名称                              | 目的             |
| ------------------------------- | -------------- |
| `OPENAI_API_KEY`                | GPT‐4o-mini キー |

---

## 手動実行例

```bash
# 当日分を一気に回す
python scripts/orchestrate_daily.py

# 単一ファイルだけ再翻訳
python scripts/translate_specific_remote.py task omcb047 11404
```

---

## 新しい言語を追加する

1. `languages/<lang>/contests/` を作成
2. `languages/config.json` に `<lang>` を追加
   これだけでワークフローが新言語を自動生成します。

---

## ライセンス

コードは MIT ライセンス、翻訳 HTML は OMC コンテンツのファンアーカイブとして公開しています。
