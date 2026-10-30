import datetime
import random
import sys
from zoneinfo import ZoneInfo

from user_server_client import UserServerClient

LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def evening_ts(days_ago=1):
    """Return a slightly randomized local timestamp between 21:05 and 22:55."""
    target_date = datetime.datetime.now(LOCAL_TZ).date() - datetime.timedelta(days=days_ago)
    minute = random.randint(5, 115)
    second = random.randint(0, 59)
    start = datetime.datetime.combine(target_date, datetime.time(21, 0), tzinfo=LOCAL_TZ)
    return int(start.timestamp()) + minute * 60 + second

def play(ts, scene, duration=1800):
    return [ts, {"cmd": f"sleep.scene.{scene}", "event": "sop_start", "duration": duration}]

def main():
    client = UserServerClient(
        base_url="https://api.mindora316.com/user_server",
        # base_url="http://192.168.0.116:9001",
        jwt_token=sys.argv[1],
    )
    resp = client.update_profile(user_profile={
        "behaviors": {
            "plays": [
                # 近 7 天内：cocos 5 次（→ weekly_best / week onset_efficiency / explore scene_preference）
                play(evening_ts(1), "cocos_island_moonlight"),
                play(evening_ts(2), "cocos_island_moonlight"),
                play(evening_ts(3), "cocos_island_moonlight"),
                play(evening_ts(4), "cocos_island_moonlight"),
                play(evening_ts(5), "cocos_island_moonlight"),
                # amalfi 3 次、kyoto 2 次（→ month scenario_list top3 的 2、3 名）
                play(evening_ts(2), "amalfi_breeze"),
                play(evening_ts(6), "amalfi_breeze"),
                play(evening_ts(8), "amalfi_breeze"),
                play(evening_ts(3), "kyoto_forest"),
                play(evening_ts(10), "kyoto_forest"),
            ],
        },
        # "behaviors": {
        #     "plays": [
        #         play(evening_ts(1), "kyoto_forest"),
        #     ],
        # },
    })
    print(resp["response"].get("code"), resp["response"].get("msg"))


if __name__ == "__main__":
    main()
