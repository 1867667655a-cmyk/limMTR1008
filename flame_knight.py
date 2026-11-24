from engine import (
    Character,
    Coin,
    DamageContext,
    EventContext,
    EventType,
    Passive,
    Skill,
    burst_tremor,
)


class BlazingSlash(Skill):
    """三技能：带有独立倍率（1.5倍向下取整）+ 震颤引爆"""

    def __init__(self):
        super().__init__(
            skill_id="fk_s3",
            name="劫火震地斩",
            tier=3,
            base_power=6,
            coins=[Coin(4), Coin(4)],
        )

    def modify_damage(self, engine, action, dmg_ctx: DamageContext) -> None:
        # 角色卡独立修改伤害计算：自身倍率 150% 并额外 +3 固定伤害
        dmg_ctx.mul_percent(150)
        dmg_ctx.add_flat(3)

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(target, "烧伤", 4, source=action.actor)
        burst_tremor(engine, target, source=action.actor)


class TremorStrike(Skill):
    """二技能：施加震颤和烧伤"""

    def __init__(self):
        super().__init__(
            skill_id="fk_s2",
            name="余烬重锤",
            tier=2,
            base_power=5,
            coins=[Coin(3), Coin(3)],
        )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(target, "震颤", 5, source=action.actor)
        engine.apply_status(target, "烧伤", 2, source=action.actor)


class SparkJab(Skill):
    """一技能：拼点失败也能给对面挂1层烧伤的特色低费技"""

    def __init__(self):
        super().__init__(
            skill_id="fk_s1",
            name="火星直刺",
            tier=1,
            base_power=4,
            coins=[Coin(3)],
        )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(target, "烧伤", 2, source=action.actor)

    def on_clash_lose(self, engine, self_action, opp_action, roll_value: int) -> None:
        engine.log(f"  [被动/技能特效] {self_action.actor.name} 拼输仍溅射 1 层烧伤！")
        engine.apply_status(opp_action.actor, "烧伤", 1, source=self_action.actor)


class ImmortalEmberPassive(Passive):
    """被动：每场战斗1次，受到致命伤害时锁血至 1 HP 并对攻击者挂 5 层烧伤"""

    def __init__(self):
        super().__init__("不灭余烬")
        self.triggered = False

    def on_register(self, engine, owner: Character) -> None:
        super().on_register(engine, owner)
        self.bind(engine, EventType.CHARACTER_DEATH, self._on_death, priority=-50)

    def _on_death(self, ctx: EventContext) -> None:
        char = ctx.data["character"]
        killer = ctx.data.get("killer")
        if not self.triggered and char is self.owner and char.hp <= 0:
            self.triggered = True
            char.hp = 1
            ctx.cancelled = True
            ctx.engine.log(f"  [被动触发] {char.name} 触发【不灭余烬】，锁血至 1 HP！")
            if killer and killer.alive:
                ctx.engine.apply_status(killer, "烧伤", 5, source=char)


def create_character(team: str = "P1", **kwargs) -> Character:
    char = Character(
        char_id=f"flame_knight_{team}",
        name="烈焰骑士",
        team=team,
        max_hp=85,
        passives=[ImmortalEmberPassive()],
    )
    char.set_standard_skills(SparkJab(), TremorStrike(), BlazingSlash())
    return char
