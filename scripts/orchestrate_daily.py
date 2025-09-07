#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import time
import datetime
import json             
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
    rc = subprocess.run(cmd, shell=True).returncode
    if rc != 0:
        print(f"[Warning] コマンド失敗: {cmd} (exit={rc})")
    return rc

# ───────────────────────────────────────────────────────────
# 変更があれば commit & push（まとめ同期）
# ───────────────────────────────────────────────────────────
def git_sync(message: str):
    try:
        # GH Actions の bot 署名を毎回念のため設定
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)

        # まず全てステージ（個別スクリプトが add 済みでも冪等）
        subprocess.run(["git", "add", "-A"], check=True)

        # 差分がなければ何もしない（quiet: 差分なし=0, あり=非0）
        diff_rc = subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode
        if diff_rc == 0:
            print("→ [git] 追加すべき差分なし（スキップ）")
            return

        # 競合を避けつつコミット＆プッシュ
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "pull", "--rebase"], check=True)
        subprocess.run(["git", "push"], check=True)
        print(f"→ [git] {message} を push 済み")
    except subprocess.CalledProcessError as e:
        # ここで失敗しても全体は止めない（次フェーズを継続）
        print(f"[Warning] git 同期に失敗: {e}")

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
    # 1) 毎日 20:00 JST → 参加登録
    sleep_until(20)
    run("python3 scripts/participate_today.py")
    # 参加登録での生成物（ログイン状態の追記等）があれば拾う
    git_sync("Sync after participate_today")

    # 2) 21:00 JST → 問題文翻訳（ここで JP を保存、EN は生成 or Skip）
    sleep_until(21)
    run("python3 scripts/fetch_and_translate.py")
    # ★ EN が Skip でも JP は保存されているため、ここで必ず同期
    git_sync("Sync JA originals after tasks")

    # 3) コンテスト終了時刻まで待機
    print("→ コンテスト継続時間を取得…")
    cp = subprocess.run(
        "python3 scripts/fetch_and_translate.py --contest-json",
        shell=True, capture_output=True, text=True
    )
    try:
        jline = cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else "{}"
        j = json.loads(jline)
        dmin = int(j.get("duration_min", 60))
    except Exception:
        dmin = 60
    print(f"→ Contest Duration = {dmin} 分")

    JST = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(JST)

    # 当日21:00 JST を開始時刻と仮定
    contest_start = now.replace(hour=21, minute=0, second=0, microsecond=0)
    end_time = contest_start + datetime.timedelta(minutes=dmin)
    sleep_sec = (end_time - now).total_seconds()
    if sleep_sec > 0:
        m, s = divmod(int(sleep_sec), 60)
        print(f"→ 本当のコンテスト終了予定 ({end_time.time()}) まで {m} 分{s} 秒待機")
        time.sleep(sleep_sec)
    else:
        print(f"→ コンテスト終了予定を過ぎています (現在 {now.time()})、すぐに解説取得へ")

    # 4) 公式解説の取得・翻訳
    run("python3 scripts/fetch_editorial.py")
    # ★ ここでも EN が Skip になりうるため、JP を確実に同期
    git_sync("Sync JA originals after editorials")

    # 5) 過去３コンテストの更新
    run("python3 scripts/update_past3.py")
    # ★ 直近3回分の JP 生成もここで拾う
    git_sync("Sync JA originals after past3")

    # 6) 日付が７の倍数ならユーザー解説全更新
    today = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=9)))
    if today.day % 7 == 0:
        run("python3 scripts/update_user_editorials.py")
        # ★ ユーザー解説でも JP 側の追加・修正があれば同期
        git_sync("Sync JA originals after user editorials")

if __name__ == "__main__":
    main()