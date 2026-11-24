from engine import (
    Action,
    Character,
    Coin,
    DamageContext,
    EventContext,
    EventType,
    Passive,
    Skill,
    StatusEffect,
)


class SwordIntentBuff(StatusEffect):
    """自创 Buff：【剑意】每层使拼点掷骰基础点数 +1，满3层时在技能后自动追击一次"""

    def __init__(self, stacks: int = 1):
        super().__init__("剑意", stacks=stacks, category="buff")

    def on_attach(self, engine, owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.BEFORE_SKILL_ROLL, self._on_before_roll, priority=0)

    def _on_before_roll(self, ctx: EventContext) -> None:
        if ctx.data["actor"] is not self.owner:
            return
        ctx.data["roll_state"]["base_power"] += self.stacks


class PursuitSlash(Skill):
    """用于追击的衍生技能（不进入常规手牌，由代码直接 enqueue）"""

    def __init__(self):
        super().__init__(
            skill_id="pb_pursuit",
            name="幻影追击",
            tier="追击",
            base_power=3,
            coins=[Coin(3)],
            clashable=False,
        )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(target, "流血", 2, source=action.actor)


class SeveringVoid(Skill):
    """三技能：消耗4层充能时，强制把第一个硬币概率提升至100%，并施加破裂与腐蚀"""

    def __init__(self):
        super().__init__(
            skill_id="pb_s3",
            name="断空瞬斩",
            tier=3,
            base_power=5,
            coins=[Coin(4), Coin(4), Coin(4)],
        )

    def on_before_roll(self, engine, actor, roll_state) -> None:
        if engine.consume_status(actor, "充能", 4):
            engine.log(f"  [充能消耗] {actor.name} 消耗 4 层充能，【断空瞬斩】首枚硬币必定触发！")
            roll_state["coins"][0].probability = 1.0

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(target, "破裂", 4, source=action.actor)
        engine.apply_status(target, "腐蚀", 2, source=action.actor)


class GaleCombo(Skill):
    """二技能：获得充能、呼吸，并叠加流血"""

    def __init__(self):
        super().__init__(
            skill_id="pb_s2",
            name="疾风连刃",
            tier=2,
            base_power=4,
            coins=[Coin(3), Coin(3)],
        )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(action.actor, "充能", 3)
        engine.apply_status(action.actor, "呼吸", 6)
        engine.apply_status(target, "流血", 3, source=action.actor)


class BreathDraw(Skill):
    """一技能：攒充能、攒呼吸、叠自创Buff剑意"""

    def __init__(self):
        super().__init__(
            skill_id="pb_s1",
            name="蓄势拔刀",
            tier=1,
            base_power=3,
            coins=[Coin(3)],
        )

    def on_clash_win(self, engine, self_action, opp_action, roll_value) -> None:
        engine.apply_status(self_action.actor, SwordIntentBuff(stacks=1))

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        engine.apply_status(action.actor, "充能", 2)
        engine.apply_status(action.actor, "呼吸", 4)


class PhantomPursuitPassive(Passive):
    """被动：每当幻影剑客拼点胜利并攻击后，若自身【剑意】>= 2 层，立刻触发【追击】"""

    def __init__(self):
        super().__init__("如影随形")

    def on_register(self, engine, owner: Character) -> None:
        super().on_register(engine, owner)
        self.bind(engine, EventType.CLASH_WIN, self._on_clash_win, priority=20)

    def _on_clash_win(self, ctx: EventContext) -> None:
        win_act: Action = ctx.data["winner_action"]
        lose_act: Action = ctx.data["loser_action"]
        if win_act.actor is not self.owner:
            return
        intent = win_act.actor.get_status_stacks("剑意")
        if intent >= 2 and lose_act.actor.alive:
            ctx.engine.consume_status(win_act.actor, "剑意", 2)
            ctx.engine.log(f"  [追击触发] {win_act.actor.name} 消耗 2 层剑意，发动追击！")
            ctx.engine.enqueue_action(
                Action(
                    actor=win_act.actor,
                    skill=PursuitSlash(),
                    targets=[lose_act.actor],
                    consume_from_hand=False,
                    is_pursuit=True,
                    clashable=False,
                ),
                front=True,
            )


def create_character(team: str = "P2", **kwargs) -> Character:
    char = Character(
        char_id=f"phantom_blade_{team}",
        name="幻影剑客",
        team=team,
        max_hp=80,
        passives=[PhantomPursuitPassive()],
    )
    # 展示兼容“更多的可选择技能”：除了标准3-2-1的6个技能，多给1个额外的二技能（共7个，第7个会留到下轮导致下轮不补牌）
    char.skill_template = [
        (SeveringVoid(), 1),
        (GaleCombo(), 2),  # 故意设为3个二技能，测试跨轮未用尽技能机制
        (BreathDraw(), 3),
    ]
    return char
