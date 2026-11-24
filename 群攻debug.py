from engine import (
    Character,
    Coin,
    DamageContext,
    Passive,
    Skill,
    burst_tremor,
)


class EmberWave(Skill):
    """1. 普通敌方群攻（非拼点）：所有点数小于自身的敌人都会吃到烧伤"""

    def __init__(self):
        super().__init__(
            skill_id="cm_s1",
            name="余烬扩散",
            tier=1,
            base_power=4,
            coins=[Coin(3), Coin(2)],
            target_type="all_enemies",  # 自动识别为【普通敌方群攻】
        )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        # 所有没骰过群攻的敌人都会挂上 2 层烧伤
        engine.apply_status(target, "烧伤", 2, source=action.actor)

    def on_aoe_end(self, engine, action, hit_targets) -> None:
        # 群攻追加机制示例：每命中 1 名目标，自身获得 1 层强壮（下回合/本回合有效按引擎规则）
        if hit_targets:
            engine.log(f"  [群攻追加] {action.actor.name} 命中 {len(hit_targets)} 人，获得 {len(hit_targets)} 层伤害提升！")
            engine.apply_status(action.actor, "伤害提升", len(hit_targets))


class IndiscriminateQuake(Skill):
    """2. 无差别群攻（非拼点）：敌人和队友一起掷点数对抗，输的人本回合技能作废并吃震颤"""

    def __init__(self):
        super().__init__(
            skill_id="cm_s2",
            name="无差别震荡",
            tier=2,
            base_power=5,
            coins=[Coin(4), Coin(3)],
            target_type="all_others",  # 自动识别为【无差别群攻】（敌人 + 队友）
        )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(target, "震颤", 4, source=action.actor)
        engine.apply_status(target, "破裂", 2, source=action.actor)


class ClashMeteor(Skill):
    """3. 拼点群攻（仅敌方）：只找 1 个敌人拼点，拼赢直接炸全体敌人"""

    def __init__(self):
        super().__init__(
            skill_id="cm_s3",
            name="陨星冲击",
            tier=3,
            base_power=6,
            coins=[Coin(4), Coin(4)],
            clash_aoe=True,  # 开启【拼点群攻】（默认仅攻击敌人）
        )

    def modify_damage(self, engine, action, dmg_ctx: DamageContext) -> None:
        # 群攻独立倍率：120% 伤害
        dmg_ctx.mul_percent(120)

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(target, "烧伤", 3, source=action.actor)
        # 群攻触发震颤引爆
        burst_tremor(engine, target, source=action.actor)


class AbyssCollapse(Skill):
    """4. 无差别拼点群攻：找 1 个敌人拼点，拼赢无差别轰炸场上所有敌人与队友"""

    def __init__(self):
        super().__init__(
            skill_id="cm_s4",
            name="深渊崩毁",
            tier="EX",
            base_power=7,
            coins=[Coin(5), Coin(4)],
            clash_aoe=True,       # 拼点群攻
            indiscriminate=True,  # 叠加无差别（赢了之后敌人、队友全吃伤害与Debuff）
        )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(target, "腐蚀", 3, source=action.actor)
        engine.apply_status(target, "流血", 2, source=action.actor)


class CalamityFocusPassive(Passive):
    """被动示例：使用群攻命中任意目标后自身获得 1 层充能"""

    def __init__(self):
        super().__init__("灾厄共鸣")


def create_character(team: str = "P1", **kwargs) -> Character:
    char = Character(
        char_id=f"cataclysm_mage_{team}",
        name="灾厄术士",
        team=team,
        max_hp=90,
        passives=[CalamityFocusPassive()],
    )

    # 如果想用标准 3-2-1 模板（一共 6 张牌），可以这样写：
    # char.set_standard_skills(EmberWave(), IndiscriminateQuake(), ClashMeteor())

    # 这里演示直接用 skill_template 把 4 种群攻都放进牌库，方便测试：
    char.skill_template = [
        (AbyssCollapse(), 1),       # 1 张【无差别拼点群攻】
        (ClashMeteor(), 1),         # 1 张【拼点群攻】
        (IndiscriminateQuake(), 2), # 2 张【无差别群攻】
        (EmberWave(), 2),           # 2 张【普通敌方群攻】
    ]
    return char
