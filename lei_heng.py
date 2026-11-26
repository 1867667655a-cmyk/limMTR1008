from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from engine import (
    Action,
    BGMTrack,
    Character,
    Coin,
    DamageContext,
    EventContext,
    EventType,
    Passive,
    Skill,
    StatusEffect,
    floor_mul,
)

if TYPE_CHECKING:
    from engine import CombatEngine


# ==============================================================================
# 专属状态定义（全部归类为中性 category="neutral"：既不属于增益，也不属于减益）
# ==============================================================================

class TigerAmmoStatus(StatusEffect):
    """虎标弹：使用技能时消耗，已消耗过虎标弹的技能本回合不会再次消耗"""

    def __init__(self, stacks: int = 0):
        super().__init__("虎标弹", stacks=stacks, category="neutral")


class FierceTigerAmmoStatus(StatusEffect):
    """猛虎标弹：伤害*1.3、烧伤层数*2、每次消耗使技能最终威力+1、适用消耗虎标弹词条"""

    def __init__(self, stacks: int = 0):
        super().__init__("猛虎标弹", stacks=stacks, category="neutral")


class DiliStatus(StatusEffect):
    """
    底力（最低0层，不会消失/转移，计数器）：
    造成伤害+5，受到伤害+3；>=25 时 1:1 转化为极力；每层+1%伤害，上限30。
    """

    MAX_STACKS = 30
    CONVERT_THRESHOLD = 25

    def __init__(self, stacks: int = 0):
        super().__init__("底力", stacks=max(0, stacks), category="neutral")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.CALCULATE_DAMAGE, self._on_calc_damage, priority=15)
        self.bind(engine, EventType.AFTER_TAKE_DAMAGE, self._on_after_damage, priority=25)

    def on_reapply(self, engine: "CombatEngine", add_stacks: int, extra: Dict[str, Any]) -> None:
        self.stacks = min(self.MAX_STACKS, max(0, self.stacks + add_stacks))

    def _on_calc_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.attacker is not self.owner or self.stacks <= 0 or dmg_ctx.damage <= 0:
            return
        dmg_ctx.mul_percent(100 + self.stacks)

    def _on_after_damage(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive:
            return
        # 若已转化为极力，则不再积累底力
        if owner.custom_state.get("has_jili"):
            return

        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.damage <= 0:
            return

        gain = 0
        if dmg_ctx.attacker is owner:
            gain += 5
        if dmg_ctx.target is owner:
            gain += 3
        if gain <= 0:
            return

        self.stacks = min(self.MAX_STACKS, self.stacks + gain)
        ctx.engine.log(f"  [底力] {owner.name} 底力 +{gain}（当前: {self.stacks}）")

        if self.stacks >= self.CONVERT_THRESHOLD:
            converted = self.stacks
            self.stacks = 0  # 最低0层，保留计数器不移除
            owner.custom_state["has_jili"] = True
            ctx.engine.log(f"  [底力转化] {owner.name} 底力达到 {converted} 层，1:1 转化为极力！")
            grant_jili(ctx.engine, owner, converted)


class JiliStatus(StatusEffect):
    """
    极力：
    造成伤害+10，受到伤害+5；每层+2%伤害；每10层最终威力+1；上限50；
    >=50 时将在下回合技能结算结束时额外自动释放一次特殊技能追击。
    """

    MAX_STACKS = 50

    def __init__(self, stacks: int = 0):
        super().__init__("极力", stacks=min(self.MAX_STACKS, max(0, stacks)), category="neutral")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.BEFORE_SKILL_ROLL, self._on_before_roll, priority=0)
        self.bind(engine, EventType.CALCULATE_DAMAGE, self._on_calc_damage, priority=15)
        self.bind(engine, EventType.AFTER_TAKE_DAMAGE, self._on_after_damage, priority=25)

    def on_reapply(self, engine: "CombatEngine", add_stacks: int, extra: Dict[str, Any]) -> None:
        self.stacks = min(self.MAX_STACKS, max(0, self.stacks + add_stacks))

    def _on_before_roll(self, ctx: EventContext) -> None:
        if ctx.data["actor"] is not self.owner or self.stacks <= 0:
            return
        bonus = self.stacks // 10
        if bonus > 0:
            ctx.data["roll_state"]["base_power"] += bonus

    def _on_calc_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.attacker is not self.owner or self.stacks <= 0 or dmg_ctx.damage <= 0:
            return
        dmg_ctx.mul_percent(100 + 2 * self.stacks)

    def _on_after_damage(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive:
            return
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.damage <= 0:
            return

        gain = 0
        if dmg_ctx.attacker is owner:
            gain += 10
        if dmg_ctx.target is owner:
            gain += 5
        if gain <= 0:
            return

        grant_jili(ctx.engine, owner, gain)


class SlashStanceStatus(StatusEffect):
    """
    斩切：
    拥有此效果时所有技能威力+2、转化为不可摧毁硬币、伤害增加100%，回合结束减1层。
    """

    def __init__(self, stacks: int = 1):
        super().__init__("斩切", stacks=stacks, category="neutral")

    def on_attach(self, engine: "CombatEngine", owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.BEFORE_SKILL_ROLL, self._on_before_roll, priority=0)
        self.bind(engine, EventType.CLASH_START, self._on_clash_start, priority=0)
        self.bind(engine, EventType.CALCULATE_DAMAGE, self._on_calc_damage, priority=20)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=90)

    def _on_before_roll(self, ctx: EventContext) -> None:
        if ctx.data["actor"] is not self.owner or self.stacks <= 0:
            return
        ctx.data["roll_state"]["base_power"] += 2
        for coin in ctx.data["roll_state"]["coins"]:
            coin.unbreakable = True

    def _on_clash_start(self, ctx: EventContext) -> None:
        if self.stacks <= 0 or not self.owner:
            return
        for key in ("action1", "action2"):
            act: Action = ctx.data[key]
            if act.actor is self.owner:
                act.extra["force_unbreakable"] = True

    def _on_calc_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.attacker is not self.owner or self.stacks <= 0 or dmg_ctx.damage <= 0:
            return
        dmg_ctx.mul_percent(200)

    def _on_turn_end(self, ctx: EventContext) -> None:
        if self.owner:
            ctx.engine.set_status_stacks(self.owner, self.name, self.stacks - 1)


# ==============================================================================
# 通用辅助逻辑（台词、弹药、烧伤触发、极力/底力/斩切发放）
# ==============================================================================

def speak(engine: "CombatEngine", char: Character, line: str) -> None:
    engine.log(f'  【{char.name}】"{line}"')


def trigger_burn(
    engine: "CombatEngine",
    target: Character,
    source: Optional[Character],
    times: int = 1,
) -> None:
    """触发目标烧伤 X 次：造成等量伤害，且层数不会减半"""
    for _ in range(times):
        if not target.alive:
            break
        burn = target.get_status_stacks("烧伤")
        if burn <= 0:
            break
        engine.log(f"  [烧伤触发] {target.name} 触发 {burn} 层烧伤")
        engine.apply_damage(
            attacker=source,
            target=target,
            raw_damage=burn,
            source_type="dot_burn_trigger",
            trigger_on_hit_statuses=False,
            allow_breath=False,
        )


def unlock_dili(engine: "CombatEngine", char: Character) -> None:
    """解锁 0 层底力计数器（不消失、不转移）"""
    if "底力" not in char.statuses:
        dili = DiliStatus(stacks=0)
        char.statuses["底力"] = dili
        dili.on_attach(engine, char)
        engine.log(f"  [插翅虎] {char.name} 解锁了【底力】（初始 0 层）")


def grant_slash_stance(engine: "CombatEngine", char: Character, stacks: int) -> None:
    if not char.alive or stacks <= 0:
        return
    if not char.custom_state.get("spoken_slash"):
        char.custom_state["spoken_slash"] = True
        speak(engine, char, "动手就得有始有终，对不？")
    engine.apply_status(char, SlashStanceStatus(stacks=stacks), source=char)


def _trigger_first_jili(engine: "CombatEngine", char: Character) -> None:
    if char.custom_state.get("spoken_jili"):
        return
    char.custom_state["spoken_jili"] = True
    char.custom_state["has_jili"] = True
    speak(engine, char, "嗬。你们要动真格的，那我可就来劲了。")
    # 猛虎标弹支援：第一次获得极力时，立刻丢弃所有子弹并获得18发猛虎标弹
    char.custom_state["fierce_support_active"] = True
    discard_all_ammo(engine, char)
    grant_fierce_ammo_up_to(engine, char, 18, refill_mode=False)
    engine.bgm.evaluate(reason="首次获得极力")


def grant_jili(engine: "CombatEngine", char: Character, stacks: int) -> None:
    if not char.alive or stacks <= 0:
        return
    char.custom_state["has_jili"] = True

    existing = char.get_status("极力")
    if existing:
        existing.stacks = min(JiliStatus.MAX_STACKS, existing.stacks + stacks)
        curr = existing.stacks
    else:
        obj = engine.apply_status(
            char,
            JiliStatus(stacks=min(JiliStatus.MAX_STACKS, stacks)),
            source=char,
            extra={"from_grant_jili": True},
        )
        curr = obj.stacks if obj else 0

    engine.log(f"  [极力] {char.name} 极力当前层数: {curr}")
    _trigger_first_jili(engine, char)

    # 极力 >= 50 时将在下回合技能结算结束时额外自动释放一次特殊技能追击
    if curr >= 50 and char.custom_state.get("pending_special_turn") is None:
        next_turn = max(1, engine.global_turn) + 1
        char.custom_state["pending_special_turn"] = next_turn
        engine.log(f"  [极力蓄满] {char.name} 极力已达 {curr} 层，下回合结束时将追击释放特殊技能")


def discard_all_ammo(engine: "CombatEngine", char: Character) -> None:
    if char.get_status("虎标弹"):
        engine.remove_status(char, "虎标弹")
    if char.get_status("猛虎标弹"):
        engine.remove_status(char, "猛虎标弹")
    engine.log(f"  [更换弹药] {char.name} 丢弃了所有现有子弹")


def grant_tiger_ammo_up_to(
    engine: "CombatEngine", char: Character, target_amount: int = 12
) -> None:
    """补充或获得虎标弹至 target_amount 发；新增 0 发也算作一次获得"""
    if not char.alive:
        return
    curr = char.get_status_stacks("虎标弹")
    add = max(0, target_amount - curr)
    if add > 0:
        engine.apply_status(char, TigerAmmoStatus(stacks=add), source=char)
    engine.log(f"  [装填] {char.name} 补充虎标弹（+{add}，当前: {char.get_status_stacks('虎标弹')} 发）")

    count = char.custom_state.get("tiger_ammo_gain_count", 0) + 1
    char.custom_state["tiger_ammo_gain_count"] = count

    if count == 1 and not char.custom_state.get("spoken_tiger_ammo"):
        char.custom_state["spoken_tiger_ammo"] = True
        speak(engine, char, "豁……还真有两下子？看来不是糟蹋我子弹的家伙啊？")

    if count == 2:
        unlock_dili(engine, char)


def grant_fierce_ammo_up_to(
    engine: "CombatEngine", char: Character, amount: int, refill_mode: bool = True
) -> None:
    """refill_mode=True 时补充至 amount 发；False 时直接增加 amount 发"""
    if not char.alive:
        return
    curr = char.get_status_stacks("猛虎标弹")
    add = max(0, amount - curr) if refill_mode else max(0, amount)
    if add > 0:
        engine.apply_status(char, FierceTigerAmmoStatus(stacks=add), source=char)
    engine.log(
        f"  [猛虎装填] {char.name} 获得猛虎标弹（+{add}，当前: {char.get_status_stacks('猛虎标弹')} 发）"
    )


def get_active_ammo_type(char: Character) -> Optional[str]:
    if char.get_status_stacks("猛虎标弹") > 0:
        return "猛虎标弹"
    if char.get_status_stacks("虎标弹") > 0:
        return "虎标弹"
    return None

def _on_external_jili(self, ctx: EventContext) -> None:
        owner = self.owner
        if ctx.data.get("target") is not owner or not owner:
            return
        if ctx.data.get("status_name") != "极力":
            return
        if ctx.data.get("extra", {}).get("from_grant_jili"):
            return
        _trigger_first_jili(ctx.engine, owner)
    
def get_total_ammo(char: Character) -> int:
    return char.get_status_stacks("猛虎标弹") + char.get_status_stacks("虎标弹")


def _consume_bullets(engine: "CombatEngine", char: Character, amount: int) -> Dict[str, Any]:
    """优先消耗猛虎标弹，不足或没有则消耗虎标弹"""
    fierce = char.get_status_stacks("猛虎标弹")
    tiger = char.get_status_stacks("虎标弹")

    take_fierce = min(fierce, amount)
    rem = amount - take_fierce
    take_tiger = min(tiger, rem)

    if take_fierce > 0:
        engine.consume_status(char, "猛虎标弹", take_fierce)
    if take_tiger > 0:
        engine.consume_status(char, "虎标弹", take_tiger)

    return {
        "total": take_fierce + take_tiger,
        "fierce": take_fierce,
        "tiger": take_tiger,
    }


def _refund_ammo_record(engine: "CombatEngine", char: Character, rec: Dict[str, Any]) -> None:
    if not rec or rec.get("refunded") or rec.get("total_consumed", 0) <= 0:
        return
    rec["refunded"] = True
    f_amt = rec.get("fierce_consumed", 0)
    t_amt = rec.get("tiger_consumed", 0)
    if f_amt > 0:
        engine.apply_status(char, FierceTigerAmmoStatus(stacks=f_amt), source=char)
    if t_amt > 0:
        engine.apply_status(char, TigerAmmoStatus(stacks=t_amt), source=char)
    engine.log(
        f"  [弹药返还] {char.name} 拼点失败技能被摧毁，返还弹药 "
        f"(猛虎:{f_amt}, 虎标:{t_amt})"
    )


# ==============================================================================
# 技能基类：统一处理弹药消耗、不重扣、拼输返还、猛虎标弹加伤/翻倍烧伤
# ==============================================================================

class CommanderSkillBase(Skill):
    def _ensure_ammo_record(
        self, engine: "CombatEngine", actor: Character, action: Action
    ) -> Dict[str, Any]:
        rec = self.extra_data.get("ammo_record")
        if rec and rec.get("processed"):
            return rec

        rec = {
            "processed": True,
            "refunded": False,
            "initial_ammo": get_total_ammo(actor),
            "total_consumed": 0,
            "fierce_consumed": 0,
            "tiger_consumed": 0,
            "fierce_events": 0,      # 猛虎标弹“消耗次数”
            "used_fierce": False,
            "skill_power_bonus": 0,
            "fierce_power_bonus": 0,
            "coin_power_bonus": 0,
            "damage_bonus_pct": 0,
            "bonus_burn": 0,
            "burn_triggers": 0,
            "extra_power_clauses_done": [],
        }
        self._process_first_use(engine, actor, action, rec)
        self.extra_data["ammo_record"] = rec
        action.extra["ammo_record"] = rec
        return rec

    def _consume_one_bullet(
        self, engine: "CombatEngine", actor: Character, rec: Dict[str, Any]
    ) -> bool:
        res = _consume_bullets(engine, actor, 1)
        if res["total"] <= 0:
            return False
        rec["total_consumed"] += res["total"]
        rec["fierce_consumed"] += res["fierce"]
        rec["tiger_consumed"] += res["tiger"]
        if res["fierce"] > 0:
            rec["used_fierce"] = True
            rec["fierce_events"] += 1
            rec["fierce_power_bonus"] += 1
        return True

    def _consume_all_remaining(
        self, engine: "CombatEngine", actor: Character, rec: Dict[str, Any]
    ) -> int:
        rem = get_total_ammo(actor)
        if rem <= 0:
            return 0
        res = _consume_bullets(engine, actor, rem)
        rec["total_consumed"] += res["total"]
        rec["fierce_consumed"] += res["fierce"]
        rec["tiger_consumed"] += res["tiger"]
        if res["fierce"] > 0:
            rec["used_fierce"] = True
            # “消耗所有剩余虎标弹”算消耗一次
            rec["fierce_events"] += 1
            rec["fierce_power_bonus"] += 1
        return res["total"]

    def _process_first_use(
        self, engine: "CombatEngine", actor: Character, action: Action, rec: Dict[str, Any]
    ) -> None:
        pass

    def on_before_roll(
        self, engine: "CombatEngine", actor: Character, roll_state: Dict[str, Any]
    ) -> None:
        action: Action = roll_state["action"]
        rec = self._ensure_ammo_record(engine, actor, action)
        total_power_add = rec["skill_power_bonus"] + rec["fierce_power_bonus"]
        if total_power_add != 0:
            roll_state["base_power"] += total_power_add
        coin_add = rec.get("coin_power_bonus", 0)
        if coin_add != 0:
            for c in roll_state["coins"]:
                c.value += coin_add

    def modify_damage(
        self, engine: "CombatEngine", action: Action, dmg_ctx: DamageContext
    ) -> None:
        rec = self.extra_data.get("ammo_record") or action.extra.get("ammo_record") or {}
        pct = rec.get("damage_bonus_pct", 0)
        if pct > 0:
            dmg_ctx.mul_percent(100 + pct)

    def _apply_burn_and_triggers(
        self,
        engine: "CombatEngine",
        actor: Character,
        target: Character,
        base_burn: int,
        rec: Dict[str, Any],
    ) -> None:
        total_burn = base_burn + rec.get("bonus_burn", 0)
        if rec.get("used_fierce"):
            total_burn *= 2
        if total_burn > 0 and target.alive:
            engine.apply_status(target, "烧伤", total_burn, source=actor)
        triggers = rec.get("burn_triggers", 0)
        if triggers > 0 and target.alive:
            trigger_burn(engine, target, source=actor, times=triggers)


# ==============================================================================
# 技能定义：一 / 二 / 三 / 特殊 / 强力特殊
# ==============================================================================

class CommanderSkill1(CommanderSkillBase):
    """一技能：8+2+2"""

    def __init__(self):
        super().__init__(
            skill_id="cmd_s1",
            name="二连斩",
            tier=1,
            base_power=8,
            coins=[Coin(2), Coin(2)],
        )

    def _process_first_use(self, engine, actor, action, rec) -> None:
        if self._consume_one_bullet(engine, actor, rec):
            rec["bonus_burn"] += 3
            engine.log(f"  [弹药消耗] {actor.name} 使用【{self.name}】消耗 1 发子弹（命中额外+3烧伤）")

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        rec = self.extra_data.get("ammo_record") or action.extra.get("ammo_record") or {}
        self._apply_burn_and_triggers(engine, action.actor, target, base_burn=3, rec=rec)


class CommanderSkill2(CommanderSkillBase):
    """二技能：5+3+3+3"""

    def __init__(self):
        super().__init__(
            skill_id="cmd_s2",
            name="三连击",
            tier=2,
            base_power=5,
            coins=[Coin(3), Coin(3), Coin(3)],
        )

    def _process_first_use(self, engine, actor, action, rec) -> None:
        # 条目1：消耗1发 -> 最终威力+1
        if self._consume_one_bullet(engine, actor, rec):
            rec["skill_power_bonus"] += 1
        # 条目2：消耗1发 -> 额外+3烧伤、最终威力+1
        if self._consume_one_bullet(engine, actor, rec):
            rec["bonus_burn"] += 3
            rec["skill_power_bonus"] += 1
        # 条目3：消耗1发 -> 烧伤触发1次
        if self._consume_one_bullet(engine, actor, rec):
            rec["burn_triggers"] += 1

        if rec["total_consumed"] > 0:
            engine.log(
                f"  [弹药消耗] {actor.name} 使用【{self.name}】消耗 {rec['total_consumed']} 发子弹"
                f"（威力+{rec['skill_power_bonus'] + rec['fierce_power_bonus']}）"
            )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        rec = self.extra_data.get("ammo_record") or action.extra.get("ammo_record") or {}
        # 命中时施加3层烧伤 + 命中时施加2层烧伤 = 基础5层
        self._apply_burn_and_triggers(engine, action.actor, target, base_burn=5, rec=rec)


class CommanderSkill3(CommanderSkillBase):
    """三技能：8+4+4+4（三枚不可摧毁硬币）"""

    def __init__(self):
        super().__init__(
            skill_id="cmd_s3",
            name="爆碎斩",
            tier=3,
            base_power=8,
            coins=[
                Coin(4, unbreakable=True),
                Coin(4, unbreakable=True),
                Coin(4, unbreakable=True),
            ],
        )

    def _process_first_use(self, engine, actor, action, rec) -> None:
        # 若使用此技能时剩余虎标弹大于三枚则本技能伤害加50%
        if rec["initial_ammo"] > 3:
            rec["damage_bonus_pct"] += 50

        # 条目1、2：各消耗1发 -> 额外+3烧伤、最终威力+1
        for _ in range(2):
            if self._consume_one_bullet(engine, actor, rec):
                rec["bonus_burn"] += 3
                rec["skill_power_bonus"] += 1

        # 条目3：消耗所有剩余虎标弹
        rem = self._consume_all_remaining(engine, actor, rec)
        if rem > 0:
            rec["bonus_burn"] += rec["total_consumed"] * 2
            rec["skill_power_bonus"] += rec["total_consumed"]
            rec["burn_triggers"] += 2

        if rec["total_consumed"] > 0:
            engine.log(
                f"  [弹药消耗] {actor.name} 使用【{self.name}】共消耗 {rec['total_consumed']} 发子弹"
                f"（威力+{rec['skill_power_bonus'] + rec['fierce_power_bonus']}）"
            )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        rec = self.extra_data.get("ammo_record") or action.extra.get("ammo_record") or {}
        # 命中时施加3层烧伤 + 3层烧伤 = 基础6层
        self._apply_burn_and_triggers(engine, action.actor, target, base_burn=6, rec=rec)


class CommanderSpecialSkill(CommanderSkillBase):
    """特殊技能：8+12 群攻"""

    def __init__(self):
        super().__init__(
            skill_id="cmd_special",
            name="快刀乱麻",
            tier="特殊",
            base_power=8,
            coins=[Coin(12)],
            target_type="all_enemies",
            clashable=False,
            is_aoe=True,
        )

    def _process_first_use(self, engine, actor, action, rec) -> None:
        # 至多消耗30层极力，每消耗10层最终威力+2
        jili = actor.get_status_stacks("极力")
        consume_jili = min(30, jili)
        if consume_jili > 0:
            engine.consume_status(actor, "极力", consume_jili)
            rec["skill_power_bonus"] += (consume_jili // 10) * 2

        # 全场所有存活角色烧伤层数之和每有4层，最终威力+2（最多+10）
        total_field_burn = sum(
            c.get_status_stacks("烧伤") for c in engine.get_living_characters()
        )
        burn_power_bonus = min(10, (total_field_burn // 4) * 2)
        rec["skill_power_bonus"] += burn_power_bonus

        # 消耗所有剩余虎标弹
        rem = self._consume_all_remaining(engine, actor, rec)
        if rem > 0:
            rec["skill_power_bonus"] += rec["total_consumed"]
            rec["damage_bonus_pct"] += rec["total_consumed"] * 10

        rec["burn_triggers"] += 1
        engine.log(
            f"  [特殊技能] {actor.name} 消耗 {consume_jili} 层极力、场上总烧伤 {total_field_burn} 层、"
            f"消耗 {rec['total_consumed']} 发子弹（威力+{rec['skill_power_bonus'] + rec['fierce_power_bonus']}）"
        )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        rec = self.extra_data.get("ammo_record") or action.extra.get("ammo_record") or {}
        self._apply_burn_and_triggers(engine, action.actor, target, base_burn=10, rec=rec)

    def on_attack_end(self, engine, action: Action) -> None:
        grant_slash_stance(engine, action.actor, 3)
        grant_fierce_ammo_up_to(engine, action.actor, 9, refill_mode=False)


class CommanderStrongSpecialSkill(CommanderSkillBase):
    """强力特殊技能：8+3+3+3+3+3+3 群攻（血量低于15%时替换释放）"""

    def __init__(self):
        super().__init__(
            skill_id="cmd_strong_special",
            name="超绝猛虎杀击乱斩",
            tier="强力特殊",
            base_power=8,
            coins=[Coin(3) for _ in range(6)],
            target_type="all_enemies",
            clashable=False,
            is_aoe=True,
        )

    def on_unopposed_start(self, engine, action: Action) -> None:
        speak(engine, action.actor, "既然冲着擒虎来的，就得做好被反咬的觉悟！")

    def _process_first_use(self, engine, actor, action, rec) -> None:
        # 消耗所有极力
        jili = actor.get_status_stacks("极力")
        if jili > 0:
            engine.consume_status(actor, "极力", jili)
            rec["skill_power_bonus"] += (jili // 10) * 5
            rec["coin_power_bonus"] += (jili // 10) * 2
            rec["damage_bonus_pct"] += jili * 3

        # 剩余虎标弹大于3枚 -> 伤害+50%
        if rec["initial_ammo"] > 3:
            rec["damage_bonus_pct"] += 50

        # 5条单发消耗词条
        single_clauses = [
            (4, 1, 0),
            (6, 1, 0),
            (6, 1, 0),
            (6, 1, 0),
            (10, 1, 25),
        ]
        for burn_add, pwr_add, dmg_add in single_clauses:
            if self._consume_one_bullet(engine, actor, rec):
                rec["bonus_burn"] += burn_add
                rec["skill_power_bonus"] += pwr_add
                rec["damage_bonus_pct"] += dmg_add

        # 最后消耗所有剩余虎标弹
        rem = self._consume_all_remaining(engine, actor, rec)
        if rem > 0:
            rec["skill_power_bonus"] += rec["total_consumed"]
            rec["bonus_burn"] += rec["total_consumed"] * 2

        # 本技能伤害增加本技能消耗虎标弹*5%
        if rec["total_consumed"] > 0:
            rec["damage_bonus_pct"] += rec["total_consumed"] * 5

        rec["burn_triggers"] += 4
        engine.log(
            f"  [强力特殊技能] {actor.name} 倾泻 {jili} 层极力与 {rec['total_consumed']} 发子弹"
            f"（威力+{rec['skill_power_bonus'] + rec['fierce_power_bonus']}，硬币+{rec['coin_power_bonus']}）"
        )

    def on_hit(self, engine, action, target, dmg_ctx: DamageContext) -> None:
        rec = self.extra_data.get("ammo_record") or action.extra.get("ammo_record") or {}
        # 6 + 4 + 6 + 6 = 22 基础烧伤
        self._apply_burn_and_triggers(engine, action.actor, target, base_burn=22, rec=rec)

    def on_attack_end(self, engine, action: Action) -> None:
        grant_slash_stance(engine, action.actor, 3)


# ==============================================================================
# 指挥官被动群
# ==============================================================================

class SteelQiPassive(Passive):
    """钢气：低于75%/50%/25%血量提供递进威力与增伤"""

    def __init__(self):
        super().__init__("钢气", "血量低于75%/50%/25%时获得威力与伤害加成")

    def _get_tier(self, owner: Character) -> int:
        if not owner.alive:
            return 0
        if owner.hp < floor_mul(owner.max_hp, 25, 100):
            return 3
        if owner.hp < floor_mul(owner.max_hp, 50, 100):
            return 2
        if owner.hp < floor_mul(owner.max_hp, 75, 100):
            return 1
        return 0

    def is_active(self, engine, owner: Character) -> bool:
        return self._get_tier(owner) > 0

    def on_register(self, engine, owner: Character) -> None:
        super().on_register(engine, owner)
        self.bind(engine, EventType.BEFORE_SKILL_ROLL, self._on_before_roll, priority=0)
        self.bind(engine, EventType.CALCULATE_DAMAGE, self._on_calc_damage, priority=15)

    def _on_before_roll(self, ctx: EventContext) -> None:
        if ctx.data["actor"] is not self.owner:
            return
        tier = self._get_tier(self.owner)
        if tier == 0:
            return
        rs = ctx.data["roll_state"]
        if tier == 1:
            rs["base_power"] += 1
        elif tier == 2:
            rs["base_power"] += 2
        elif tier == 3:
            rs["base_power"] += 2
            for c in rs["coins"]:
                c.value += 1

    def _on_calc_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.attacker is not self.owner or dmg_ctx.damage <= 0:
            return
        tier = self._get_tier(self.owner)
        if tier == 1:
            dmg_ctx.mul_percent(110)
        elif tier == 2:
            dmg_ctx.mul_percent(120)
        elif tier == 3:
            dmg_ctx.mul_percent(130)


class CommanderCorePassive(Passive):
    """
    核心中枢被动（整合：天退星刀 装填 / 插翅虎 / 猛虎标弹支援 / 拼输退弹 / 猛虎标弹*1.3 / 极力>=50下回合追击）
    """

    def __init__(self):
        super().__init__("天退星刀·装填与统御")

    def on_register(self, engine, owner: Character) -> None:
        super().on_register(engine, owner)
        self.bind(engine, EventType.ROUND_START, self._on_round_start, priority=0)
        self.bind(engine, EventType.CLASH_LOSE, self._on_clash_lose, priority=10)
        self.bind(engine, EventType.CALCULATE_DAMAGE, self._on_fierce_ammo_damage, priority=80)
        self.bind(engine, EventType.TURN_END, self._on_turn_end_pursuit, priority=99)

        # 兼容群攻压制返还弹药
        aoe_suppress_ev = getattr(EventType, "AOE_SUPPRESS", None)
        if aoe_suppress_ev is not None:
            self.bind(engine, aoe_suppress_ev, self._on_aoe_suppress, priority=10)

        # 兼容外部强行赋予极力（例如对手/队友卡直接 apply_status("极力")）
        self.bind(engine, EventType.STATUS_APPLY, self._on_external_jili, priority=10)

    def _on_round_start(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive:
            return
        if owner.custom_state.get("fierce_support_active"):
            grant_fierce_ammo_up_to(ctx.engine, owner, 18, refill_mode=True)
        elif owner.custom_state.get("reload_unlocked"):
            grant_tiger_ammo_up_to(ctx.engine, owner, 12)

    def _on_clash_lose(self, ctx: EventContext) -> None:
        owner = self.owner
        loser_act: Action = ctx.data["loser_action"]
        if loser_act.actor is not owner or not owner:
            return

        rec = None
        if loser_act.skill:
            rec = loser_act.skill.extra_data.get("ammo_record") or loser_act.extra.get("ammo_record")

        if ctx.data.get("destroyed", True):
            if rec:
                _refund_ammo_record(ctx.engine, owner, rec)
        else:
            # 不可摧毁硬币存活：把本次弹药消费记录同步给后续不可摧毁攻击，防止重复消耗并保留威力加成
            u_act: Optional[Action] = ctx.data.get("unbreakable_action")
            if u_act and u_act.skill and rec:
                u_act.skill.extra_data["ammo_record"] = rec
                u_act.extra["ammo_record"] = rec

        # 天退星刀 装填：自身第一次拼点失败时获得12发虎标弹
        if not owner.custom_state.get("reload_unlocked"):
            owner.custom_state["reload_unlocked"] = True
            self.notify_triggered(ctx.engine, detail="首次拼点失败，装填12发虎标弹")
            grant_tiger_ammo_up_to(ctx.engine, owner, 12)

    def _on_aoe_suppress(self, ctx: EventContext) -> None:
        owner = self.owner
        act: Optional[Action] = ctx.data.get("target_action")
        if not act or act.actor is not owner or not owner:
            return
        rec = (act.skill.extra_data.get("ammo_record") if act.skill else None) or act.extra.get("ammo_record")
        u_act: Optional[Action] = ctx.data.get("unbreakable_action")
        if ctx.data.get("suppressed") and u_act is None:
            if rec:
                _refund_ammo_record(ctx.engine, owner, rec)
        elif u_act and u_act.skill and rec:
            u_act.skill.extra_data["ammo_record"] = rec
            u_act.extra["ammo_record"] = rec

    def _on_fierce_ammo_damage(self, ctx: EventContext) -> None:
        """猛虎标弹：最终伤害直接 * 1.3（向下取整）"""
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.attacker is not self.owner or dmg_ctx.damage <= 0:
            return
        if not dmg_ctx.skill:
            return
        rec = dmg_ctx.skill.extra_data.get("ammo_record")
        if rec and rec.get("used_fierce"):
            before = dmg_ctx.damage
            dmg_ctx.mul_percent(130)
            ctx.engine.log(f"  [猛虎标弹] 伤害×1.3: {before} -> {dmg_ctx.damage}")

    def _on_external_jili(self, ctx: EventContext) -> None:
        owner = self.owner
        if ctx.data.get("target") is not owner or not owner:
            return
        if ctx.data.get("status_name") != "极力":
            return
        if not owner.custom_state.get("spoken_jili"):
            # 延后保证首次获得极力的台词/丢弹/BGM触发
            owner.custom_state["spoken_jili"] = True
            owner.custom_state["has_jili"] = True
            speak(ctx.engine, owner, "嗬。你们要动真格的，那我可就来劲了。")
            owner.custom_state["fierce_support_active"] = True
            discard_all_ammo(ctx.engine, owner)
            grant_fierce_ammo_up_to(ctx.engine, owner, 18, refill_mode=False)
            ctx.engine.bgm.evaluate(reason="首次获得极力")

    def _on_turn_end_pursuit(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive:
            return
        target_turn = owner.custom_state.get("pending_special_turn")
        if target_turn is not None and ctx.engine.global_turn >= target_turn:
            owner.custom_state["pending_special_turn"] = None
            enemies = ctx.engine.get_enemies(owner)
            if not enemies:
                return
            if owner.hp < floor_mul(owner.max_hp, 15, 100):
                sp_skill: Skill = CommanderStrongSpecialSkill()
            else:
                sp_skill = CommanderSpecialSkill()

            ctx.engine.log(f"  [极力追击] {owner.name} 触发特殊技能追击：【{sp_skill.name}】！")
            ctx.engine.enqueue_action(
                Action(
                    actor=owner,
                    skill=sp_skill,
                    targets=list(enemies),
                    consume_from_hand=False,
                    is_pursuit=True,
                    clashable=False,
                ),
                front=False,
            )


# ==============================================================================
# 角色卡入口工厂函数
# ==============================================================================

def create_character(team: str = "P1", **kwargs: Any) -> Character:
    char_ref: List[Character] = []

    bgm = BGMTrack(
        track_id="cmd_lv4_bgm",
        title="天退星",
        condition=lambda eng, tr: bool(tr.owner and tr.owner.custom_state.get("has_jili")),
        stop_on_owner_death=True,
    )

    char = Character(
        char_id=f"commander_lv4_{team}",
        name="猛虎下山",
        team=team,
        max_hp=1000,
        passives=[
            SteelQiPassive(),
            CommanderCorePassive(),
        ],
        bgm_tracks=[bgm],
        custom_state={
            "reload_unlocked": False,
            "fierce_support_active": False,
            "has_jili": False,
            "tiger_ammo_gain_count": 0,
            "pending_special_turn": None,
            "spoken_tiger_ammo": False,
            "spoken_jili": False,
            "spoken_slash": False,
        },
    )
    char.set_standard_skills(
        CommanderSkill1(),
        CommanderSkill2(),
        CommanderSkill3(),
    )
    char_ref.append(char)
    return char
