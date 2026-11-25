from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional, Type

from .events import EventContext, EventType
from .math_utils import floor_div, floor_mul, floor_val
from .models import Character, DamageContext, StatusEffect

if TYPE_CHECKING:
    from .combat import CombatEngine


def _halve(stacks: int) -> int:
    """减半向下取整，只剩1层时变为0"""
    return floor_div(stacks, 2)


# ==============================================================================
# 减益状态 (Debuffs)
# ==============================================================================

class BurnStatus(StatusEffect):
    """烧伤 [减益]：每回合结束时造成等量伤害，回合结束层数减半"""

    def __init__(self, stacks: int = 1):
        super().__init__("烧伤", stacks=stacks, category="debuff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=10)

    def _on_turn_end(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive or self.stacks <= 0:
            return
        dmg = self.stacks
        engine: "CombatEngine" = ctx.engine
        engine.log(f"  [烧伤] {owner.name} 受到 {dmg} 点烧伤伤害")
        engine.apply_damage(
            attacker=None,
            target=owner,
            raw_damage=dmg,
            source_type="dot_burn",
            trigger_on_hit_statuses=False,
            allow_breath=False,
        )
        if owner.alive:
            engine.set_status_stacks(owner, self.name, _halve(self.stacks))


class TremorStatus(StatusEffect):
    """震颤 [减益]：引爆时造成等量伤害；回合结束层数-1；引爆后层数减半"""

    def __init__(self, stacks: int = 1):
        super().__init__("震颤", stacks=stacks, category="debuff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=10)

    def _on_turn_end(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive or self.stacks <= 0:
            return
        ctx.engine.set_status_stacks(owner, self.name, max(0, self.stacks - 1))


def burst_tremor(
    engine: "CombatEngine", target: Character, source: Optional[Character] = None
) -> int:
    """震颤引爆接口：可供任何技能/被动直接调用"""
    st = target.get_status("震颤")
    if not st or st.stacks <= 0 or not target.alive:
        return 0
    dmg = st.stacks
    ctx = engine.events.emit(
        EventType.TREMOR_BURST, engine, target=target, source=source, damage=dmg, stacks=st.stacks
    )
    if ctx.cancelled:
        return 0
    dmg = floor_val(ctx.data["damage"])
    engine.log(f"  [震颤引爆] {target.name} 引爆 {st.stacks} 层震颤，受到 {dmg} 点伤害")
    actual = engine.apply_damage(
        attacker=source,
        target=target,
        raw_damage=dmg,
        source_type="dot_tremor_burst",
        trigger_on_hit_statuses=False,
        allow_breath=False,
    )
    if target.alive and target.get_status("震颤"):
        new_stacks = _halve(target.get_status_stacks("震颤"))
        engine.set_status_stacks(target, "震颤", new_stacks)
    return actual


class BleedStatus(StatusEffect):
    """流血 [减益]：拼点时造成等量伤害，回合结束层数减半"""

    def __init__(self, stacks: int = 1):
        super().__init__("流血", stacks=stacks, category="debuff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.CLASH_START, self._on_clash_start, priority=10)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=10)

    def _on_clash_start(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive or self.stacks <= 0:
            return
        act1 = ctx.data["action1"]
        act2 = ctx.data["action2"]
        if owner not in (act1.actor, act2.actor):
            return
        dmg = self.stacks
        engine: "CombatEngine" = ctx.engine
        engine.log(f"  [流血] {owner.name} 拼点触发流血，受到 {dmg} 点伤害")
        engine.apply_damage(
            attacker=None,
            target=owner,
            raw_damage=dmg,
            source_type="dot_bleed",
            trigger_on_hit_statuses=False,
            allow_breath=False,
        )

    def _on_turn_end(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive or self.stacks <= 0:
            return
        ctx.engine.set_status_stacks(owner, self.name, _halve(self.stacks))


class CorrosionStatus(StatusEffect):
    """腐蚀 [减益]：每回合结束时或受到伤害时造成等量伤害，回合结束层数减半"""

    def __init__(self, stacks: int = 1):
        super().__init__("腐蚀", stacks=stacks, category="debuff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.AFTER_TAKE_DAMAGE, self._on_take_damage, priority=10)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=10)

    def _on_take_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        owner = self.owner
        if dmg_ctx.target is not owner or owner is None or not owner.alive or self.stacks <= 0:
            return
        if not dmg_ctx.trigger_on_hit_statuses:
            return
        dmg = self.stacks
        engine: "CombatEngine" = ctx.engine
        engine.log(f"  [腐蚀] {owner.name} 受击触发腐蚀，额外受到 {dmg} 点伤害")
        engine.apply_damage(
            attacker=None,
            target=owner,
            raw_damage=dmg,
            source_type="dot_corrosion_hit",
            trigger_on_hit_statuses=False,
            allow_breath=False,
        )

    def _on_turn_end(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive or self.stacks <= 0:
            return
        dmg = self.stacks
        engine: "CombatEngine" = ctx.engine
        engine.log(f"  [腐蚀] {owner.name} 回合结束受到 {dmg} 点腐蚀伤害")
        engine.apply_damage(
            attacker=None,
            target=owner,
            raw_damage=dmg,
            source_type="dot_corrosion_end",
            trigger_on_hit_statuses=False,
            allow_breath=False,
        )
        if owner.alive:
            engine.set_status_stacks(owner, self.name, _halve(self.stacks))


class RuptureStatus(StatusEffect):
    """破裂 [减益]：受到伤害时层数减半并造成等量伤害"""

    def __init__(self, stacks: int = 1):
        super().__init__("破裂", stacks=stacks, category="debuff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.AFTER_TAKE_DAMAGE, self._on_take_damage, priority=10)

    def _on_take_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        owner = self.owner
        if dmg_ctx.target is not owner or owner is None or not owner.alive or self.stacks <= 0:
            return
        if not dmg_ctx.trigger_on_hit_statuses:
            return

        dmg = self.stacks
        new_stacks = _halve(self.stacks)
        engine: "CombatEngine" = ctx.engine
        engine.set_status_stacks(owner, self.name, new_stacks)
        engine.log(
            f"  [破裂] {owner.name} 触发破裂造成 {dmg} 点伤害（层数减半为 {new_stacks}）"
        )
        engine.apply_damage(
            attacker=None,
            target=owner,
            raw_damage=dmg,
            source_type="dot_rupture",
            trigger_on_hit_statuses=False,
            allow_breath=False,
        )


class DamageDownStatus(StatusEffect):
    """伤害下降 [减益]：造成的伤害每层减少 10%（向下取整），每回合清零"""

    def __init__(self, stacks: int = 1):
        super().__init__("伤害下降", stacks=stacks, category="debuff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.CALCULATE_DAMAGE, self._on_calc_damage, priority=10)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=90)

    def _on_calc_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.attacker is not self.owner or self.stacks <= 0 or dmg_ctx.damage <= 0:
            return
        before = dmg_ctx.damage
        rate = max(0, 100 - 10 * self.stacks)
        dmg_ctx.mul_percent(rate)
        if dmg_ctx.damage != before:
            ctx.engine.log(
                f"  [伤害下降] {self.owner.name} ({self.stacks}层) 降伤 {100 - rate}%: {before} -> {dmg_ctx.damage}"
            )

    def _on_turn_end(self, ctx: EventContext) -> None:
        if self.owner:
            ctx.engine.set_status_stacks(self.owner, self.name, 0)


class FragileStatus(StatusEffect):
    """易损 [减益]：受到的伤害每层增加 10%（向下取整，DoT 与追加伤害不生效），每回合清零"""

    def __init__(self, stacks: int = 1):
        super().__init__("易损", stacks=stacks, category="debuff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.BEFORE_TAKE_DAMAGE, self._on_before_take_damage, priority=10)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=90)

    def _on_before_take_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.target is not self.owner or self.stacks <= 0 or dmg_ctx.damage <= 0:
            return
        if dmg_ctx.ignore_protection_fragile:
            return
        before = dmg_ctx.damage
        pct = 100 + 10 * self.stacks
        dmg_ctx.mul_percent(pct)
        if dmg_ctx.damage != before:
            ctx.engine.log(
                f"  [易损] {self.owner.name} ({self.stacks}层) 受击增伤 +{10 * self.stacks}%: {before} -> {dmg_ctx.damage}"
            )

    def _on_turn_end(self, ctx: EventContext) -> None:
        if self.owner:
            ctx.engine.set_status_stacks(self.owner, self.name, 0)


class WeakStatus(StatusEffect):
    """虚弱 [减益]：技能威力每层减 1，每回合清零"""

    def __init__(self, stacks: int = 1):
        super().__init__("虚弱", stacks=stacks, category="debuff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.BEFORE_SKILL_ROLL, self._on_before_roll, priority=0)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=90)

    def _on_before_roll(self, ctx: EventContext) -> None:
        if ctx.data["actor"] is not self.owner or self.stacks <= 0:
            return
        ctx.data["roll_state"]["base_power"] -= self.stacks

    def _on_turn_end(self, ctx: EventContext) -> None:
        if self.owner:
            ctx.engine.set_status_stacks(self.owner, self.name, 0)


class CoinPowerDownStatus(StatusEffect):
    """硬币威力弱化 [减益]：变动值威力每层减 1，每回合清零"""

    def __init__(self, stacks: int = 1):
        super().__init__("硬币威力弱化", stacks=stacks, category="debuff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        # priority=50 确保先吃完“硬币威力强化 / 愤怒”等加值后再减算，正向硬币下限保底为 0
        self.bind(engine, EventType.BEFORE_SKILL_ROLL, self._on_before_roll, priority=50)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=90)

    def _on_before_roll(self, ctx: EventContext) -> None:
        if ctx.data["actor"] is not self.owner or self.stacks <= 0:
            return
        for coin in ctx.data["roll_state"]["coins"]:
            if coin.value >= 0:
                coin.value = max(0, coin.value - self.stacks)
            else:
                coin.value -= self.stacks

    def _on_turn_end(self, ctx: EventContext) -> None:
        if self.owner:
            ctx.engine.set_status_stacks(self.owner, self.name, 0)


# ==============================================================================
# 增益状态 (Buffs)
# ==============================================================================

class ChargeStatus(StatusEffect):
    """充能 [增益]：使用特定技能消耗"""

    def __init__(self, stacks: int = 1):
        super().__init__("充能", stacks=stacks, category="buff")


class BreathStatus(StatusEffect):
    """呼吸 [增益]：造成伤害时消耗一半（1层时消耗1层），造成 (1 + 消耗层数%) 伤害"""

    def __init__(self, stacks: int = 1):
        super().__init__("呼吸", stacks=stacks, category="buff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.CALCULATE_DAMAGE, self._on_calc_damage, priority=50)

    def _on_calc_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        owner = self.owner
        if dmg_ctx.attacker is not owner or owner is None or not dmg_ctx.allow_breath:
            return
        if self.stacks <= 0 or dmg_ctx.damage <= 0:
            return

        consumed = 1 if self.stacks == 1 else _halve(self.stacks)
        remaining = self.stacks - consumed
        before_dmg = dmg_ctx.damage
        dmg_ctx.damage = floor_mul(dmg_ctx.damage, 100 + consumed, 100)

        engine: "CombatEngine" = ctx.engine
        engine.log(
            f"  [呼吸] {owner.name} 消耗 {consumed} 层呼吸（剩余 {remaining}），"
            f"伤害 {before_dmg} -> {dmg_ctx.damage} (+{consumed}%)"
        )
        engine.set_status_stacks(owner, self.name, remaining)


class ProtectionStatus(StatusEffect):
    """守护 [增益]：受到的伤害每层减 10%（向下取整，DoT 与追加伤害不生效），每回合清零"""

    def __init__(self, stacks: int = 1):
        super().__init__("守护", stacks=stacks, category="buff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.BEFORE_TAKE_DAMAGE, self._on_before_take_damage, priority=10)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=90)

    def _on_before_take_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.target is not self.owner or self.stacks <= 0 or dmg_ctx.damage <= 0:
            return
        if dmg_ctx.ignore_protection_fragile:
            return
        rate = max(0, 100 - 10 * self.stacks)
        before = dmg_ctx.damage
        dmg_ctx.mul_percent(rate)
        if dmg_ctx.damage != before:
            ctx.engine.log(
                f"  [守护] {self.owner.name} ({self.stacks}层) 减伤 {100 - rate}%: {before} -> {dmg_ctx.damage}"
            )

    def _on_turn_end(self, ctx: EventContext) -> None:
        if self.owner:
            ctx.engine.set_status_stacks(self.owner, self.name, 0)


class StrengthStatus(StatusEffect):
    """强壮 [增益]：技能威力每层加 1，每回合清零"""

    def __init__(self, stacks: int = 1):
        super().__init__("强壮", stacks=stacks, category="buff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.BEFORE_SKILL_ROLL, self._on_before_roll, priority=0)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=90)

    def _on_before_roll(self, ctx: EventContext) -> None:
        if ctx.data["actor"] is not self.owner or self.stacks <= 0:
            return
        ctx.data["roll_state"]["base_power"] += self.stacks

    def _on_turn_end(self, ctx: EventContext) -> None:
        if self.owner:
            ctx.engine.set_status_stacks(self.owner, self.name, 0)


class DamageUpStatus(StatusEffect):
    """伤害提升 [增益]：造成的伤害每层增加 10%（向下取整），每回合清零"""

    def __init__(self, stacks: int = 1):
        super().__init__("伤害提升", stacks=stacks, category="buff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.CALCULATE_DAMAGE, self._on_calc_damage, priority=10)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=90)

    def _on_calc_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.attacker is not self.owner or self.stacks <= 0 or dmg_ctx.damage <= 0:
            return
        before = dmg_ctx.damage
        pct = 100 + 10 * self.stacks
        dmg_ctx.mul_percent(pct)
        if dmg_ctx.damage != before:
            ctx.engine.log(
                f"  [伤害提升] {self.owner.name} ({self.stacks}层) 增伤 +{10 * self.stacks}%: {before} -> {dmg_ctx.damage}"
            )

    def _on_turn_end(self, ctx: EventContext) -> None:
        if self.owner:
            ctx.engine.set_status_stacks(self.owner, self.name, 0)


class CoinPowerUpStatus(StatusEffect):
    """硬币威力强化 [增益]：变动值威力每层加 1，每回合清零"""

    def __init__(self, stacks: int = 1):
        super().__init__("硬币威力强化", stacks=stacks, category="buff")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.BEFORE_SKILL_ROLL, self._on_before_roll, priority=0)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=90)

    def _on_before_roll(self, ctx: EventContext) -> None:
        if ctx.data["actor"] is not self.owner or self.stacks <= 0:
            return
        for coin in ctx.data["roll_state"]["coins"]:
            coin.value += self.stacks

    def _on_turn_end(self, ctx: EventContext) -> None:
        if self.owner:
            ctx.engine.set_status_stacks(self.owner, self.name, 0)


# ==============================================================================
# 注册表与统一分类表
# ==============================================================================

STATUS_REGISTRY: Dict[str, Type[StatusEffect]] = {}
STATUS_CATEGORIES: Dict[str, str] = {}


def register_status(name: str, cls: Type[StatusEffect], category: str = "neutral") -> None:
    STATUS_REGISTRY[name] = cls
    STATUS_CATEGORIES[name] = category


def register_default_statuses() -> None:
    # 减益 (debuff)
    register_status("烧伤", BurnStatus, "debuff")
    register_status("震颤", TremorStatus, "debuff")
    register_status("流血", BleedStatus, "debuff")
    register_status("腐蚀", CorrosionStatus, "debuff")
    register_status("破裂", RuptureStatus, "debuff")
    register_status("伤害下降", DamageDownStatus, "debuff")
    register_status("易损", FragileStatus, "debuff")
    register_status("虚弱", WeakStatus, "debuff")
    register_status("硬币威力弱化", CoinPowerDownStatus, "debuff")

    # 增益 (buff)
    register_status("充能", ChargeStatus, "buff")
    register_status("呼吸", BreathStatus, "buff")
    register_status("守护", ProtectionStatus, "buff")
    register_status("强壮", StrengthStatus, "buff")
    register_status("伤害提升", DamageUpStatus, "buff")
    register_status("硬币威力强化", CoinPowerUpStatus, "buff")
