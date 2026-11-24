"""弹窗/问卷测试的自包含夹具配置。

历史上测试直接复制仓库 data/popup_survey_config.json（线上运营配置，会随投放调整），
配置一变动测试就批量失败。这里内嵌一份固定配置：POPUP_ID 弹窗 + SURVEY_ID 问卷，
字段与断言所需内容一一对应，测试不再依赖 live 文件。
"""
import copy
import json

POPUP_ID = "pop_20260730_survey_01"
SURVEY_ID = "sv_sleep_2026q3"

FIXTURE_CONFIG = {
  "next_query_after": 300,
  "popups": [
    {
      "popup_id": POPUP_ID,
      "type": "survey",
      "placement": "home",
      "start_at": 1786000000,
      "end_at": 1893456000,  # 2030-01-01，测试期内恒有效
      "priority": 100,
      "display_rule": {
        "max_show_count": 3,
        "cooldown_seconds": 86400,
        "dismiss_stops": True,
      },
      "action_type": "survey",
      "action_payload": {"survey_id": SURVEY_ID},
      "image_url": "",
      "push_message": True,
      "i18n": {
        "zh-Hans": {
          "badge": "参与调研",
          "badge_style": "purple",
          "title": "帮助我们做得更好",
          "subtitle": "完成 3 分钟睡眠体验问卷，提交后可获得会员积分。",
          "action_text": "查看详情",
        },
        "en": {
          "badge": "Survey",
          "badge_style": "purple",
          "title": "Help Us Improve",
          "subtitle": "Take a 3-minute sleep experience survey and earn member points.",
          "action_text": "View Details",
        },
      },
    },
  ],
  "surveys": {
    SURVEY_ID: {
      "survey_id": SURVEY_ID,
      "i18n": {
        "zh-Hans": {
          "title": "睡眠体验问卷",
          "questions": [
            {
              "question_id": "q1", "index": 1, "type": "single_choice",
              "title": "最近 7 天，你对自己的整体睡眠满意度如何？",
              "hint": "请选择一个最符合你感受的答案",
              "options": [
                {"option_id": "o1", "text": "非常满意"},
                {"option_id": "o2", "text": "比较满意"},
                {"option_id": "o3", "text": "一般"},
                {"option_id": "o4", "text": "不太满意"},
                {"option_id": "o5", "text": "非常不满意"},
              ],
            },
            {
              "question_id": "q2", "index": 2, "type": "single_choice",
              "title": "最近 7 天，你对 Mindora 的整体满意度如何？",
              "hint": "请选择一个最符合你感受的答案",
              "options": [
                {"option_id": "o1", "text": "非常满意"},
                {"option_id": "o2", "text": "比较满意"},
                {"option_id": "o3", "text": "一般"},
                {"option_id": "o4", "text": "不太满意"},
                {"option_id": "o5", "text": "非常不满意"},
              ],
            },
            {
              "question_id": "q3", "index": 3, "type": "text",
              "title": "你对 Mindora 有什么改进意见吗？",
              "hint": "请填写你的建议", "placeholder": "请输入…", "required": False,
            },
          ],
          "reward": {
            "gift_type": "physical",
            "gift_name": "品牌睡眠好礼",
            "desc": "礼品会在 10 个工作日内发出，我们会以邮件的方式通知您。",
            "contact_email": "support@mindora316.com",
          },
          "reward_title": "问卷奖励",
        },
        "en": {
          "title": "Sleep Experience Survey",
          "questions": [
            {
              "question_id": "q1", "index": 1, "type": "single_choice",
              "title": "Over the past 7 days, how satisfied are you with your overall sleep?",
              "hint": "Pick the answer that best matches how you feel",
              "options": [
                {"option_id": "o1", "text": "Very satisfied"},
                {"option_id": "o2", "text": "Satisfied"},
                {"option_id": "o3", "text": "Neutral"},
                {"option_id": "o4", "text": "Dissatisfied"},
                {"option_id": "o5", "text": "Very dissatisfied"},
              ],
            },
            {
              "question_id": "q2", "index": 2, "type": "single_choice",
              "title": "Over the past 7 days, how satisfied are you with Mindora overall?",
              "hint": "Pick the answer that best matches how you feel",
              "options": [
                {"option_id": "o1", "text": "Very satisfied"},
                {"option_id": "o2", "text": "Satisfied"},
                {"option_id": "o3", "text": "Neutral"},
                {"option_id": "o4", "text": "Dissatisfied"},
                {"option_id": "o5", "text": "Very dissatisfied"},
              ],
            },
            {
              "question_id": "q3", "index": 3, "type": "text",
              "title": "Any suggestions to help us improve Mindora?",
              "hint": "Share your thoughts", "placeholder": "Type here…", "required": False,
            },
          ],
          "reward": {
            "gift_type": "physical",
            "gift_name": "Mindora Sleep Gift",
            "desc": "Your gift will be shipped within 10 business days. We will notify you by email.",
            "contact_email": "support@mindora316.com",
          },
          "reward_title": "Survey Reward",
        },
      },
    },
  },
}


def write_fixture_config(path) -> None:
  """把夹具配置写入 path（每个测试的独立副本）。"""
  path.write_text(json.dumps(FIXTURE_CONFIG, ensure_ascii=False), encoding="utf-8")


def fixture_popups() -> list:
  """夹具 popups 的深拷贝（测试可自由改写）。"""
  return copy.deepcopy(FIXTURE_CONFIG["popups"])
