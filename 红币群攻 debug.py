from engine import (
    Character,
    Coin,
    DamageContext,
    Passive,
    Skill,
    burst_tremor,
)


class CrimsonPierce(Skill):
    """1. 红币单体技能（1白币 + 1红币）：拼输/抗群攻失败后，等对方放完再以 4+1[固] 只打原先预选的人"""

    def __init__(self):
        super().__init__(
            skill_id="ca_s1",
            name="血棘突刺",
            tier=1,
            base_power=4,
            coins=[
                Coin(3),                    # 第1枚：普通白币（拼输会碎）
                Coin(4, unbreakable=True),  # 第2枚：红币（拼输后保留为 +1）
            ],
        )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        # 哪怕是碎币状态命中，依然会正常触发 on_hit 的流血
        engine.apply_status(target, "流血", 2, source=action.actor)


class ShatterTremorWave(Skill):
    """2. 红币非拼点群攻（全红币）：若在群攻竞争中落败或被更高群攻压制，等对方放完后依然以碎币打全体敌人"""

    def __init__(self):
        super().__init__(
            skill_id="ca_s2",
            name="余震裂地",
            tier=2,
            base_power=5,
            coins=[
                Coin(3, unbreakable=True),
                Coin(3, unbreakable=True),
            ],
            target_type="all_enemies",  # 非拼点敌方群攻
        )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(target, "震颤", 3, source=action.actor)


class BloodMeteorClash(Skill):
    """3. 红币拼点群攻（1白币 + 1红币）：拼赢直接炸全体敌人；拼输等对方放完后，依然以碎币炸全体敌人"""

    def __init__(self):
        super().__init__(
            skill_id="ca_s3",
            name="赤陨裁决",
            tier=3,
            base_power=6,
            coins=[
                Coin(4),
                Coin(5, unbreakable=True),
            ],
            clash_aoe=True,  # 拼点群攻
        )

    def on_unbreakable_attack_start(self, engine, action) -> None:
        # 碎币反击专属钩子：碎币发动时先给自己挂 2 层伤害提升（+20%伤害）
        engine.log(f"  [碎币狂怒] {action.actor.name} 的【{self.name}】虽已碎币，仍燃起 2 层伤害提升！")
        engine.apply_status(action.actor, "伤害提升", 2)

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(target, "烧伤", 3, source=action.actor)
        burst_tremor(engine, target, source=action.actor)


class CrimsonDoomsday(Skill):
    """4. 无差别红币拼点群攻（整张技能不可摧毁）：拼赢或碎币都会无差别轰炸敌人与队友"""

    def __init__(self):
        super().__init__(
            skill_id="ca_s4",
            name="猩红终末",
            tier="EX",
            base_power=7,
            coins=[Coin(4), Coin(4)],
            unbreakable=True,     # 整张技能全部硬币均为不可摧毁红币
            clash_aoe=True,       # 拼点群攻
            indiscriminate=True,  # 无差别攻击（敌人+队友）
        )

    def modify_damage(self, engine, action, dmg_ctx: DamageContext) -> None:
        # 如果是以碎币状态发动，伤害按 80% 结算；正常拼赢则是 120%
        if action.is_unbreakable_attack:
            dmg_ctx.mul_percent(80)
        else:
            dmg_ctx.mul_percent(120)

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(target, "破裂", 3, source=action.actor)
        engine.apply_status(target, "腐蚀", 2, source=action.actor)


class CrimsonTenacityPassive(Passive):
    """被动示例"""

    def __init__(self):
        super().__init__("不屈血印")


def create_character(team: str = "P1", **kwargs) -> Character:
    char = Character(
        char_id=f"crimson_arbiter_{team}",
        name="猩红裁决者",
        team=team,
        max_hp=95,
        passives=[CrimsonTenacityPassive()],
    )

    # 放入 4 种红币/群攻技能方便一次性测试
    char.skill_template = [
        (CrimsonDoomsday(), 1),     # 1张 无差别红币拼点群攻
        (BloodMeteorClash(), 1),    # 1张 混合红币拼点群攻
        (ShatterTremorWave(), 2),   # 2张 全红币非拼点群攻
        (CrimsonPierce(), 2),       # 2张 混合红币单体技能
    ]
    return char
