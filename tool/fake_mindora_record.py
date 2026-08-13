import os, time
from user_server_client import UserServerClient
  
now = int(time.time())
DAY = 86400

def play(ts, scene, duration=1800):
    return [ts, {"cmd": f"sleep.scene.{scene}", "event": "sop_start", "duration": duration}]

client = UserServerClient(
    base_url="https://api.mindora316.com/user_server",
    jwt_token=os.environ["JWT_TOKEN"],
)
resp = client.update_profile(user_profile={
    "behaviors": {
        "plays": [
            # 近 7 天内：cocos 5 次（→ weekly_best / week onset_efficiency / explore scene_preference）
            play(now - 1*DAY, "cocos_island_moonlight"),
            play(now - 2*DAY, "cocos_island_moonlight"),
            play(now - 3*DAY, "cocos_island_moonlight"),
            play(now - 4*DAY, "cocos_island_moonlight"),
            play(now - 5*DAY, "cocos_island_moonlight"),
            # amalfi 3 次、kyoto 2 次（→ month scenario_list top3 的 2、3 名）
            play(now - 2*DAY, "amalfi_breeze"),
            play(now - 6*DAY, "amalfi_breeze"),
            play(now - 8*DAY, "amalfi_breeze"),
            play(now - 3*DAY, "kyoto_forest"),
            play(now - 10*DAY, "kyoto_forest"),
        ],
    },
})
print(resp["response"].get("code"), resp["response"].get("msg"))

