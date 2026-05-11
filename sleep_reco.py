import logging
from typing import List, Optional
from user_profile import UserProfile, SleepScenario, SleepStage

class RecommendationEngine:
    """根据用户画像生成 Sleep Scenarios 的引擎"""

    @staticmethod
    def should_rerun_recommendation(old_profile: Optional[UserProfile], new_profile: UserProfile) -> bool:
        """层级判断逻辑"""
        # 第一层：如果没有旧方案，必须生成
        if not old_profile or not old_profile.sleep_scenarios:
            return True
        
        # 第二层：判断画像关键字段（例如压力指数、睡眠时长偏好）是否发生显著变化
        # 假设我们关注 long_term_profile 中的特定指标
        def get_metric(profile, key):
            for k, v in profile.long_term_profile:
                if k == key: return v
            return None

        # 示例：压力值变化超过 0.3 则重算
        old_stress = get_metric(old_profile, "stress_index")
        new_stress = get_metric(new_profile, "stress_index")
        if old_stress is not None and new_stress is not None:
            if abs(old_stress - new_stress) > 0.3:
                return True
                
        return False

    @staticmethod
    def generate(profile: UserProfile) -> List[SleepScenario]:
        """生成逻辑：实际开发中这里可以调用 LLM 或 规则库"""
        # 模拟生成两个候选方案
        scenarios = [
            SleepScenario(
                scenario_id="ocean_zen_001",
                scenario_name="海浪禅意方案",
                stages=[
                    SleepStage(stage_name="Relax", background_name="blue_ocean", audio_file="waves.mp3", 
                               guide_file="relax_guide.mp3", light_scene="breathing_blue", aroma_mode="ocean_breeze"),
                    # ... 此处应补全四个阶段，此处仅为示例
                ]
            ),
            SleepScenario(
                scenario_id="forest_night_002",
                scenario_name="森林静谧方案",
                stages=[
                    SleepStage(stage_name="Relax", background_name="deep_forest", audio_file="forest_rain.mp3", 
                               guide_file="forest_guide.mp3", light_scene="soft_green", aroma_mode="woodland"),
                    # ... 补全阶段
                ]
            )
        ]
        return scenarios# 新增：存储推荐的助眠候选方案