#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import time
import datetime
import subprocess

# ───────────────────────────────────────────────────────────
# 環境変数チェック
# ───────────────────────────────────────────────────────────
for v in ("OPENAI_API_KEY", "OMC_USERNAME", "OMC_PASSWORD"):
    if not os.getenv(v):
        print(f"[Error] {v} が設定されていません。")
        sys.exit(1)

# ───────────────────────────────────────────────────────────
# コマンド実行ヘルパー
# ───────────────────────────────────────────────────────────
def run(cmd: str):
    print(f"→ 実行: {cmd}")
    if subprocess.run(cmd, shell=True).returncode != 0:
        print(f"[Warning] コマンド失敗: {cmd}")

# ───────────────────────────────────────────────────────────
# JST の指定時刻までスリープする（すでに過ぎていればスキップ）
# ───────────────────────────────────────────────────────────
def sleep_until(hour: int, minute: int = 0):
    tz = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if now < target:
        delta = (target - now).total_seconds()
        print(f"→ {hour:02d}:{minute:02d} JST まで {int(delta)} 秒待機")
        time.sleep(delta)
    else:
        print(f"→ {hour:02d}:{minute:02d} JST は既に過ぎているためスキップ (現在 {now.time().replace(microsecond=0)})")

# ───────────────────────────────────────────────────────────
# メイン処理
# ───────────────────────────────────────────────────────────
def main():
    # 1) 毎朝 8:00 JST → 参加登録
    sleep_until(20)
    run("python3 scripts/participate_today.py")

    # 2) 9:00 JST → 問題文翻訳
    sleep_until(21)
    run("python3 scripts/fetch_and_translate.py")

    # 3) コンテスト終了時刻（開始 9:00 + duration_min）まで待機
    print("→ コンテスト継続時間を取得…")
    cp = subprocess.run(
        "python3 scripts/fetch_and_translate.py --contest-json",
        shell=True, capture_output=True, text=True
    )
    try:
        j = json.loads(cp.stdout.strip().splitlines()[-1])
        dmin = int(j.get("duration_min", 60))
    except Exception:
        dmin = 60
    print(f"→ Contest Duration = {dmin} 分")

    # JST タイムゾーン定義
    from datetime import datetime, time as dtime, timedelta, timezone
    JST = timezone(timedelta(hours=9))
    now = datetime.now(JST)
    # 当日9:00 JST をコンテスト開始時刻とみなす
    contest_start = now.replace(hour=21, minute=0, second=0, microsecond=0)
    # 終了予定時刻
    end_time = contest_start + timedelta(minutes=dmin)
    sleep_sec = (end_time - now).total_seconds()
    if sleep_sec > 0:
        m, s = divmod(int(sleep_sec), 60)
        print(f"→ 本当のコンテスト終了予定 ({end_time.time()}) まで {m} 分{s} 秒待機")
        time.sleep(sleep_sec)
    else:
        print(f"→ コンテスト終了予定を過ぎています (現在 {now.time()})、すぐに解説取得へ")


    # 4) 解説翻訳
    run("python3 scripts/fetch_editorial.py")

    # 5) 過去３コンテスト更新
    run("python3 scripts/update_past3.py")

    # 6) 日付が７の倍数ならユーザー解説全更新
    today = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=9)))
    if today.day % 7 == 0:
        run("python3 scripts/update_user_editorials.py")

if __name__ == "__main__":
    main()
