import os
import sys
import time
import datetime
import subprocess

for v in ("OPENAI_API_KEY","OMC_USERNAME","OMC_PASSWORD"):
    if not os.getenv(v):
        print(f"[Error] {v} が設定されていません。")
        sys.exit(1)


def run(cmd):
    print(f"→ 実行: {cmd}")
    subprocess.run(cmd, shell=True)


def sleep_until(hour:int, minute:int=0):
    tz = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        target += datetime.timedelta(days=1)
    delta = (target - now).total_seconds()
    print(f"→ {hour:02d}:{minute:02d} JST まで {int(delta)} 秒待機")
    time.sleep(delta)


def main():
    # 1) 8:00 JST
    run("python3 scripts/participate_today.py")
    # 2) 9:00 JST
    sleep_until(9)
    run("python3 scripts/fetch_and_translate.py --contest=")
    cp = subprocess.run("python3 scripts/fetch_and_translate.py --contest-json", shell=True,
                        capture_output=True, text=True)
    try:
        dmin = int(__import__('json').loads(cp.stdout.splitlines()[-1]).get('duration_min',60))
    except:
        dmin = 60
    print(f"→ コンテスト継続時間: {dmin} 分 → 待機")
    time.sleep(dmin * 60)
    # 3) 解説翻訳
    run("python3 scripts/fetch_editorial.py --contest=")
    # 4) 過去3コンテスト更新
    run("python3 scripts/update_past3.py")
    DAY = 7
    # 5) DAYの倍数なら全ユーザー解説更新
    if datetime.datetime.now().day % DAY == 0:
        run("python3 scripts/update_user_editorials.py")

if __name__ == "__main__":
    main()