from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from engine import (
    STATUS_CATEGORIES,
    Action,
    Character,
    Coin,
    DamageContext,
    EventContext,
    EventType,
    Passive,
    Skill,
    StatusEffect,
    floor_div,
    floor_mul,
    floor_val,
)

SUKUNA_MAX_HP = 1000
MAX_EXCITEMENT = 10  # [待确认] 兴致上限，如需其他数值可直接修改此常量


def _get_sukuna_passive(char: Character) -> Optional["SukunaCorePassive"]:
    for p in char.passives:
        if isinstance(p, SukunaCorePassive):
            return p
    return None


# ==============================================================================
# 专属衍生状态
# ==============================================================================

class BleedVulnerabilityStatus(StatusEffect):
    """三技能附加：3回合内受到的流血施加层数 ×2"""

    def __init__(self, stacks: int = 3):
        super().__init__("流血易伤(宿傩)", stacks=stacks, category="debuff")

    def on_attach(self, engine, owner: Character) -> None:
        super().on_attach(engine, owner)
        self.bind(engine, EventType.STATUS_APPLY, self._on_status_apply, priority=5)
        self.bind(engine, EventType.TURN_END, self._on_turn_end, priority=90)

    def on_reapply(self, engine, add_stacks: int, extra: Dict[str, Any]) -> None:
        self.stacks = max(self.stacks, floor_val(add_stacks))

    def _on_status_apply(self, ctx: EventContext) -> None:
        if ctx.data.get("target") is not self.owner:
            return
        if ctx.data.get("status_name") == "流血" and self.stacks > 0:
            ctx.data["stacks"] = floor_mul(ctx.data["stacks"], 2, 1)

    def _on_turn_end(self, ctx: EventContext) -> None:
        if self.owner and self.stacks > 0:
            ctx.engine.set_status_stacks(self.owner, self.name, self.stacks - 1)


# ==============================================================================
# 宿傩技能定义
# ==============================================================================

class SukunaSkill1(Skill):
    """1技能：7+2+2+2"""

    def __init__(self):
        super().__init__(
            skill_id="sukuna_s1",
            name="解·一式",
            tier=1,
            base_power=7,
            coins=[Coin(2), Coin(2), Coin(2)],
            tags={"sukuna_basic"},
        )


class SukunaSkill2(Skill):
    """2技能：9+4（最后1枚正面重投，每次正面+1易损，连续重投3次停止并引爆流血）"""

    def __init__(self):
        super().__init__(
            skill_id="sukuna_s2",
            name="捌·连斩",
            tier=2,
            base_power=9,
            coins=[Coin(4)],
            tags={"sukuna_basic"},
        )

    def on_after_roll(self, engine, actor: Character, roll_state: Dict[str, Any]) -> None:
        action: Action = roll_state["action"]
        coin_results = roll_state.get("coin_results", [])
        if not coin_results:
            return

        last_coin, last_triggered, _ = coin_results[-1]
        fragile_hits = 1 if last_triggered else 0
        reroll_count = 0
        burst_bleed = False

        while last_triggered and reroll_count < 3:
            reroll_count += 1
            triggered = engine.rng.random() < last_coin.probability
            val = floor_val(last_coin.value) if triggered else 0
            roll_state["total"] = floor_val(roll_state["total"] + val)
            if triggered:
                fragile_hits += 1
                engine.log(
                    f"    [{actor.name}:{self.name}] 第 {reroll_count} 次重投 -> 正面({val:+d}) (当前:{roll_state['total']})"
                )
            else:
                engine.log(
                    f"    [{actor.name}:{self.name}] 第 {reroll_count} 次重投 -> 反面(+0) (当前:{roll_state['total']})"
                )
            last_triggered = triggered

        if reroll_count >= 3:
            burst_bleed = True
            engine.log(f"  [重投上限] {actor.name} 的【{self.name}】连续重投3次，停止重投并触发流血引爆！")

        action.extra["s2_fragile_hits"] = fragile_hits
        action.extra["s2_burst_bleed"] = burst_bleed

    def on_hit(self, engine, action: Action, target: Character, dmg_ctx: DamageContext) -> None:
        fragile_hits = action.extra.get("s2_fragile_hits", 0)
        if fragile_hits > 0:
            engine.apply_status(target, "易损", fragile_hits, source=action.actor)

        if action.extra.get("s2_burst_bleed") and target.alive:
            bleed = target.get_status_stacks("流血")
            if bleed > 0:
                engine.log(f"  [流血引爆] {target.name} 引爆 {bleed} 层流血，扣除 {bleed} 点生命！")
                engine.apply_damage(
                    attacker=action.actor,
                    target=target,
                    raw_damage=bleed,
                    source_type="dot_bleed_burst",
                    trigger_on_hit_statuses=False,
                    allow_breath=False,
                )
                if target.alive:
                    new_bleed = floor_div(bleed, 2)
                    engine.set_status_stacks(target, "流血", new_bleed)
                    engine.log(f"  [流血引爆] {target.name} 剩余流血层数减半为 {new_bleed} 层")


class SukunaSkill3(Skill):
    """3技能：11+3+3+3（命中使敌方3回合受到流血施加×2；敌方流血>=10时转化为「灶」）"""

    def __init__(self):
        super().__init__(
            skill_id="sukuna_s3",
            name="捌·深斩",
            tier=3,
            base_power=11,
            coins=[Coin(3), Coin(3), Coin(3)],
            tags={"sukuna_basic"},
        )

    def on_hit(self, engine, action: Action, target: Character, dmg_ctx: DamageContext) -> None:
        engine.apply_status(
            target,
            BleedVulnerabilityStatus(stacks=3),
            stacks=3,
            source=action.actor,
        )


class FugaSkill(Skill):
    """「灶」：20+5+5+5（流血×1.5并转化为烧伤；领域内威力+场上流血÷2且改为群攻）"""

    def __init__(self):
        super().__init__(
            skill_id="sukuna_fuga",
            name="「灶」",
            tier="EX",
            base_power=20,
            coins=[Coin(5), Coin(5), Coin(5)],
            tags={"sukuna_triggered"},
        )

    def on_before_roll(self, engine, actor: Character, roll_state: Dict[str, Any]) -> None:
        passive = _get_sukuna_passive(actor)
        if passive and passive.domain_active():
            total_bleed = sum(
                c.get_status_stacks("流血")
                for c in engine.get_living_characters()
            )
            bonus = floor_div(total_bleed, 2)
            if bonus > 0:
                roll_state["base_power"] = floor_val(roll_state["base_power"] + bonus)
                engine.log(f"  [领域·灶] 吸收场上 {total_bleed} 层流血，威力 +{bonus}！")

    def on_hit(self, engine, action: Action, target: Character, dmg_ctx: DamageContext) -> None:
        bleed = target.get_status_stacks("流血")
        if bleed > 0:
            burn_gain = floor_mul(bleed, 15, 10)
            engine.set_status_stacks(target, "流血", 0)
            engine.apply_status(target, "烧伤", burn_gain, source=action.actor)
            engine.log(f"  [「灶」转化] {target.name} 的 {bleed} 层流血转化为 {burn_gain} 层烧伤！")


class WorldCuttingSlashSkill(Skill):
    """空间斩：50+10（无视减伤、护盾、锁血，命中伤害×2）"""

    def __init__(self):
        super().__init__(
            skill_id="sukuna_wcs",
            name="空间斩",
            tier="EX",
            base_power=50,
            coins=[Coin(10)],
            clashable=False,
            tags={"sukuna_triggered", "world_cutting_slash"},
        )

    def modify_damage(self, engine, action: Action, dmg_ctx: DamageContext) -> None:
        dmg_ctx.ignore_protection_fragile = True
        dmg_ctx.mul_percent(200)
        dmg_ctx.extra["ignore_damage_reduction"] = True
        dmg_ctx.extra["ignore_death_immunity"] = True
        dmg_ctx.extra["wcs_min_damage"] = dmg_ctx.damage

    def on_hit(self, engine, action: Action, target: Character, dmg_ctx: DamageContext) -> None:
        if target.hp <= 0 and target.alive:
            target.alive = False
            engine.log(f"  [空间斩] 无视锁血，直接斩杀 {target.name}！")


class ReverseCursedTechniqueSkill(Skill):
    """反转术式（技能）：回5%血后重新选技；若术式熔断则失去回血、抵消熔断并扣5%血"""

    def __init__(self):
        super().__init__(
            skill_id="sukuna_rct",
            name="反转术式",
            tier="特殊",
            base_power=0,
            coins=[],
            target_type="self",
            clashable=False,
            tags={"sukuna_rct"},
        )

    def on_select(self, engine, action: Action) -> None:
        actor = action.actor
        passive = _get_sukuna_passive(actor)
        if passive:
            passive.execute_rct(engine, actor)

        if self in actor.available_skills:
            actor.available_skills.remove(self)

        engine.log(f"  [反转术式] {actor.name} 使用完毕，请继续选择本回合要使用的技能！")
        slot_idx = action.extra.get("slot_idx", 0)
        if actor.ai_selector:
            next_act = actor.ai_selector(engine, actor, slot_idx, list(engine.current_turn_selections))
        elif engine.manual_selector:
            next_act = engine.manual_selector(engine, actor, slot_idx, list(engine.current_turn_selections))
        else:
            next_act = engine._default_ai_select(actor, slot_idx, list(engine.current_turn_selections))

        if next_act.skill is not None:
            if next_act.consume_from_hand and next_act.skill in actor.available_skills:
                actor.available_skills.remove(next_act.skill)
            next_act.skill.on_select(engine, next_act)

        action.skill = next_act.skill
        action.targets = next_act.targets
        action.consume_from_hand = next_act.consume_from_hand
        action.is_pursuit = next_act.is_pursuit
        action.clashable = next_act.clashable
        action.extra.update(next_act.extra)


class ShenWuJieSkill(Skill):
    """神武解：6+4+4+4（每正面1个硬币施加2层麻痹）"""

    def __init__(self):
        super().__init__(
            skill_id="sukuna_shenwujie",
            name="神武解",
            tier="EX",
            base_power=6,
            coins=[Coin(4), Coin(4), Coin(4)],
            clashable=False,
            tags={"sukuna_pursuit"},
        )

    def on_after_roll(self, engine, actor: Character, roll_state: Dict[str, Any]) -> None:
        heads = sum(1 for _, triggered, _ in roll_state.get("coin_results", []) if triggered)
        roll_state["action"].extra["swj_heads"] = heads

    def on_hit(self, engine, action: Action, target: Character, dmg_ctx: DamageContext) -> None:
        heads = action.extra.get("swj_heads", 0)
        if heads > 0:
            engine.apply_status(target, "麻痹", heads * 2, source=action.actor)


# ==============================================================================
# 「双嘴」与「四手」实体部位（单纯无法行动的部位）
# ==============================================================================

class SukunaBodyPart(Character):
    """原身启动召唤的「双嘴」或「四手」部位"""

    def __init__(self, part_id: str, name: str, team: str, sukuna: Character):
        super().__init__(
            char_id=part_id,
            name=name,
            team=team,
            max_hp=100,
            hp=100,
            action_slots=0,
        )
        self.sukuna = sukuna


# ==============================================================================
# 宿傩核心被动集合控制器
# ==============================================================================

class SukunaCorePassive(Passive):
    """整合宿傩全套被动、领域、术式熔断、魔虚罗适应与原身启动全机制"""

    def __init__(self):
        super().__init__(
            "诅咒之王",
            "包含兴致、咒力、黑闪、伏魔御厨子、术式熔断、十种影法术、抱歉切成三段与原身启动全机制",
        )
        # 兴致相关（整场首次拼败/打平+2，之后+1）
        self.excitement: int = 0
        self.first_tie_or_lose_done: bool = False

        # 头抬太高了
        self.head_too_high_ready_round: int = 1
        self.blocked_pursuit_targets_round: Dict[int, int] = {}

        # 反转术式计数（上限1）
        self.rct_count: int = 0

        # 咒力与领域（累计消耗20开启领域，开完重新计）
        self.cursed_energy: int = 15
        self.consumed_cursed_energy: int = 0
        self.domain_turns_left: int = 0
        self.burnout_active: bool = False

        # 黑闪倍率
        self.black_flash_mult_pct: int = 100

        # 魔虚罗（90%血以下开启）
        self.mahoraga_enabled: bool = False
        self.mahoraga_permanently_disabled: bool = False
        self.chimera_heal_disabled: bool = False
        self.heavy_hit_count_150: int = 0

        self.adapt1_skills: Dict[str, int] = {}
        self.adapt2_debuffs: Dict[str, int] = {}
        self.adapt3_buffs: Dict[str, int] = {}

        # 二阶段：也就是说…是？赢了 / 原身启动
        self.win_revive_triggered: bool = False
        self.true_form_triggered: bool = False
        self.mouth_part: Optional[SukunaBodyPart] = None
        self.arms_part: Optional[SukunaBodyPart] = None

        # 双嘴四手同时在场：拼胜5次 -> 3回合内全拼胜则再放一次空间斩
        self.both_parts_clash_wins: int = 0
        self.wcs_challenge_active: bool = False
        self.wcs_challenge_turns_left: int = 0
        self.wcs_challenge_started_turn: int = -1
        self.wcs_challenge_wins: int = 0
        self.wcs_challenge_failed: bool = False

        # 内部记录缓存
        self._bleed_snapshot: Dict[int, int] = {}
        self._maintaining_bleed: bool = False
        self._temp_rct_skill: Optional[ReverseCursedTechniqueSkill] = None
        self._blocked_unbreakable_actions: Set[int] = set()
        self._enemy_active_skill_vs_sukuna: Dict[int, str] = {}
        self._nullified_enemy_actors: Set[int] = set()

    def domain_active(self) -> bool:
        return self.domain_turns_left > 0 and not self.burnout_active

    def technique_active(self) -> bool:
        return not self.burnout_active

    def _is_body_part(self, char: Optional[Character]) -> bool:
        return char is not None and (char is self.mouth_part or char is self.arms_part)

    def _add_excitement(self, amount: int) -> int:
        if amount <= 0 or self.excitement >= MAX_EXCITEMENT:
            return 0
        before = self.excitement
        self.excitement = min(MAX_EXCITEMENT, self.excitement + amount)
        return self.excitement - before

    def _both_parts_alive(self) -> bool:
        return bool(
            self.true_form_triggered
            and self.mouth_part is not None
            and self.mouth_part.alive
            and self.arms_part is not None
            and self.arms_part.alive
        )

    def _reset_wcs_challenge(self) -> None:
        self.both_parts_clash_wins = 0
        self.wcs_challenge_active = False
        self.wcs_challenge_turns_left = 0
        self.wcs_challenge_started_turn = -1
        self.wcs_challenge_wins = 0
        self.wcs_challenge_failed = False

    def _apply_healing(self, engine, amount: int, reason: str = "") -> int:
        if not self.owner or not self.owner.alive or amount <= 0:
            return 0
        final_heal = floor_mul(amount, self.black_flash_mult_pct, 100)
        before = self.owner.hp
        self.owner.hp = min(self.owner.max_hp, self.owner.hp + final_heal)
        gained = self.owner.hp - before
        if gained > 0:
            prefix = f"[{reason}] " if reason else ""
            engine.log(
                f"  {prefix}{self.owner.name} 回复 {gained} 点生命值 (HP: {self.owner.hp}/{self.owner.max_hp})"
            )
            engine.bgm.evaluate(reason="宿傩回复生命")
        return gained

    def execute_rct(self, engine, owner: Character) -> None:
        if self.rct_count > 0:
            self.rct_count -= 1
        five_pct = max(1, floor_mul(owner.max_hp, 5, 100))

        if self.burnout_active:
            self.burnout_active = False
            owner.hp = max(0, owner.hp - five_pct)
            engine.log(
                f"  [反转术式] {owner.name} 处于术式熔断，抵消术式熔断但失去 {five_pct} HP (HP: {owner.hp}/{owner.max_hp})"
            )
            engine._check_death(owner, killer=None)
            if owner.alive:
                self._try_activate_domain(engine)
        else:
            self._apply_healing(engine, five_pct, reason="反转术式")

    def on_register(self, engine, owner: Character) -> None:
        super().on_register(engine, owner)

        self.bind(engine, EventType.GAME_START, self._on_game_start, priority=0)
        self.bind(engine, EventType.ROUND_START, self._on_round_start, priority=0)
        self.bind(engine, EventType.ROUND_END, self._on_round_end, priority=50)
        self.bind(engine, EventType.TURN_START, self._on_turn_start, priority=5)
        self.bind(engine, EventType.TURN_END, self._on_turn_end_pre, priority=5)
        self.bind(engine, EventType.TURN_END, self._on_turn_end_post, priority=15)
        self.bind(engine, EventType.TURN_END, self._on_turn_end_late, priority=95)

        self.bind(engine, EventType.BEFORE_SELECT_ACTION, self._on_before_select, priority=-10)
        self.bind(engine, EventType.AFTER_SELECT_ACTION, self._on_after_select, priority=10)
        self.bind(engine, EventType.RESOLVE_MATCHUPS, self._on_resolve_matchups, priority=10)

        self.bind(engine, EventType.BEFORE_SKILL_ROLL, self._on_before_roll, priority=20)
        self.bind(engine, EventType.BEFORE_SKILL_ROLL, self._on_before_roll_detect_opp_buff, priority=999)
        self.bind(engine, EventType.AFTER_SKILL_ROLL, self._on_after_roll, priority=20)

        self.bind(engine, EventType.CLASH_START, self._on_clash_start, priority=10)
        self.bind(engine, EventType.CLASH_START, self._on_clash_start_intercept, priority=999999)
        self.bind(engine, EventType.CLASH_ROLL, self._on_clash_roll, priority=10)
        self.bind(engine, EventType.CLASH_WIN, self._on_clash_win, priority=10)
        self.bind(engine, EventType.CLASH_LOSE, self._on_clash_lose, priority=10)
        self.bind(engine, EventType.CLASH_CANCEL_99, self._on_clash_cancel_99, priority=10)

        self.bind(engine, EventType.UNBREAKABLE_ATTACK_START, self._on_unbreakable_start, priority=10)
        self.bind(engine, EventType.UNOPPOSED_ATTACK_START, self._on_unopposed_start, priority=-50)

        self.bind(engine, EventType.CALCULATE_DAMAGE, self._on_calc_damage, priority=30)
        self.bind(engine, EventType.BEFORE_TAKE_DAMAGE, self._on_before_take_damage_record_pre, priority=-999)
        self.bind(engine, EventType.BEFORE_TAKE_DAMAGE, self._on_before_take_damage, priority=-20)
        self.bind(engine, EventType.BEFORE_TAKE_DAMAGE, self._on_before_take_damage_sorry_passive, priority=50)
        self.bind(engine, EventType.BEFORE_TAKE_DAMAGE, self._on_before_take_damage_wcs_restore, priority=999)
        self.bind(engine, EventType.AFTER_TAKE_DAMAGE, self._on_after_take_damage, priority=20)

        self.bind(engine, EventType.STATUS_APPLY, self._on_status_apply, priority=20)
        self.bind(engine, EventType.CHARACTER_DEATH, self._on_character_death, priority=-40)
        self.bind(engine, EventType.CHARACTER_DEATH, self._on_part_death_notify, priority=100)

    # ------------------------------------------------------------------
    # 生命周期与领域 / 熔断 / 魔虚罗检查
    # ------------------------------------------------------------------
    def _on_game_start(self, ctx: EventContext) -> None:
        if self.owner:
            ctx.engine.log(f"  [咒术师] {self.owner.name} 开局获得 {self.cursed_energy} 层咒力！")

    def _on_round_start(self, ctx: EventContext) -> None:
        if self.burnout_active and self.owner and self.owner.alive:
            self.burnout_active = False
            ctx.engine.log(f"  [术式恢复] {self.owner.name} 的术式熔断结束，术式被动恢复生效！")
            self._try_activate_domain(ctx.engine)

    def _on_round_end(self, ctx: EventContext) -> None:
        engine = ctx.engine
        owner = self.owner
        if not owner or not owner.alive:
            return

        # 嵌合兽带有（术式）标记：术式熔断期间不会回血
        if self.technique_active() and not self.chimera_heal_disabled:
            heal_amt = max(1, floor_mul(owner.max_hp, 5, 100))
            self._apply_healing(engine, heal_amt, reason="十种影法术·嵌合兽")

        if self.rct_count >= 1:
            engine.log(f"  [反转术式溢出] {owner.name} 的反转术式已满，溢出自动施放！")
            self.execute_rct(engine, owner)
        else:
            self.rct_count = 1
            engine.log(f"  [反转术式] {owner.name} 获得 1 次【反转术式】储备（1/1）")

    def _try_activate_domain(self, engine) -> None:
        owner = self.owner
        if not owner or not owner.alive or self.burnout_active or self.domain_turns_left > 0:
            return
        if self.consumed_cursed_energy >= 20:
            self.consumed_cursed_energy -= 20
            duration = 3 if self.true_form_triggered else 5
            self.domain_turns_left = duration
            engine.log(
                f"  [领域展开] {owner.name} 展开【伏魔御厨子】（持续 {duration} 回合）！"
            )
            self._maintain_domain_bleed(engine)

    def _maintain_domain_bleed(self, engine) -> None:
        """领域开启及维持期间：除宿傩自身以及双嘴、四手外，场上所有人流血不低于 8（0层也会立即变8层）"""
        if not self.domain_active() or self._maintaining_bleed:
            return
        self._maintaining_bleed = True
        try:
            for c in engine.get_living_characters():
                if c is self.owner or self._is_body_part(c):
                    continue
                cur = c.get_status_stacks("流血")
                if cur < 8:
                    engine.apply_status(c, "流血", 8 - cur, source=self.owner)
        finally:
            self._maintaining_bleed = False

    def _check_mahoraga_unlock(self, engine) -> None:
        owner = self.owner
        if (
            not owner
            or not owner.alive
            or self.mahoraga_enabled
            or self.mahoraga_permanently_disabled
        ):
            return
        if owner.hp <= floor_mul(owner.max_hp, 90, 100):
            self.mahoraga_enabled = True
            engine.log(f"  [十种影法术·魔虚罗] {owner.name} 生命低于90%，法阵转动，开启魔虚罗适应！")

    def _on_turn_start(self, ctx: EventContext) -> None:
        engine = ctx.engine
        owner = self.owner
        if not owner or not owner.alive:
            return

        self._check_mahoraga_unlock(engine)

        self.cursed_energy += 1
        engine.log(f"  [咒术师] {owner.name} 回合开始咒力 +1（当前: {self.cursed_energy}）")

        if self.domain_active():
            engine.log(f"  [伏魔御厨子] 领域剩余 {self.domain_turns_left} 回合，斩击遍布战场！")
            for c in engine.get_living_characters():
                if c is not owner and not self._is_body_part(c):
                    engine.apply_status(c, "流血", 8, source=owner)
            self._maintain_domain_bleed(engine)

        if self.technique_active() and self.mahoraga_enabled:
            for st_name, st_obj in list(owner.statuses.items()):
                if st_obj.is_debuff and st_obj.stacks > 0:
                    self.adapt2_debuffs[st_name] = self.adapt2_debuffs.get(st_name, 0) + 1
                    engine.log(
                        f"  [适应二] {st_name}（适应2：{self.adapt2_debuffs[st_name]}）"
                    )

    def _on_turn_end_pre(self, ctx: EventContext) -> None:
        if not self.domain_active():
            return
        self._bleed_snapshot.clear()
        for c in ctx.engine.get_living_characters():
            if c is not self.owner and not self._is_body_part(c):
                self._bleed_snapshot[id(c)] = c.get_status_stacks("流血")

    def _on_turn_end_post(self, ctx: EventContext) -> None:
        engine = ctx.engine
        owner = self.owner
        if not owner or not owner.alive:
            return

        if self.domain_active():
            self._maintaining_bleed = True
            try:
                for c in engine.get_living_characters():
                    if c is owner or self._is_body_part(c):
                        continue
                    target_bleed = max(8, self._bleed_snapshot.get(id(c), 0))
                    cur = c.get_status_stacks("流血")
                    if cur < target_bleed:
                        if cur == 0:
                            engine.apply_status(c, "流血", target_bleed, source=owner)
                        else:
                            engine.set_status_stacks(c, "流血", target_bleed)
            finally:
                self._maintaining_bleed = False

        if self.technique_active() and self.mahoraga_enabled:
            for st_name, lvl in list(self.adapt2_debuffs.items()):
                if lvl >= 10:
                    cur = owner.get_status_stacks(st_name)
                    if cur > 0:
                        halved = floor_div(cur, 2)
                        engine.set_status_stacks(owner, st_name, halved)
                        engine.log(
                            f"  [适应二·10层] {owner.name} 身上的 {st_name} 回合结束再减半 -> {halved}"
                        )

    def _on_turn_end_late(self, ctx: EventContext) -> None:
        engine = ctx.engine
        owner = self.owner
        if not owner:
            return

        self._enemy_active_skill_vs_sukuna.clear()
        self._nullified_enemy_actors.clear()

        if self._temp_rct_skill and self._temp_rct_skill in owner.available_skills:
            owner.available_skills.remove(self._temp_rct_skill)
        self._temp_rct_skill = None

        if self.domain_turns_left > 0 and not self.burnout_active:
            self.domain_turns_left -= 1
            if self.domain_turns_left == 0:
                self.burnout_active = True
                engine.log(
                    f"  [术式熔断] {owner.name} 的【伏魔御厨子】结束，进入术式熔断状态直至下一轮开始！"
                )

        # 双嘴四手同时在场：3回合内全部拼点胜利 -> 再释放一次空间斩
        if self.wcs_challenge_active and engine.global_turn > self.wcs_challenge_started_turn:
            if not owner.alive or not self._both_parts_alive():
                engine.log("  [空间斩蓄势中断] 双嘴或四手已不在场，挑战重置！")
                self._reset_wcs_challenge()
            else:
                self.wcs_challenge_turns_left -= 1
                if self.wcs_challenge_turns_left <= 0:
                    if not self.wcs_challenge_failed and self.wcs_challenge_wins > 0:
                        enemies = engine.get_enemies(owner)
                        if enemies:
                            engine.log(
                                f"  [★ 空间斩再临 ★] 双嘴与四手同时在场且连续3回合拼点全胜"
                                f"（共 {self.wcs_challenge_wins} 次胜利），{owner.name} 再释放一次【空间斩】！"
                            )
                            engine.enqueue_action(
                                Action(
                                    actor=owner,
                                    skill=WorldCuttingSlashSkill(),
                                    targets=[enemies[0]],
                                    consume_from_hand=False,
                                    is_pursuit=True,
                                    clashable=False,
                                ),
                                front=True,
                            )
                    else:
                        engine.log("  [空间斩蓄势结束] 3回合内未完成有效全胜拼点，计数重置。")
                    self._reset_wcs_challenge()

    # ------------------------------------------------------------------
    # 选技前/后 & 打双嘴四手时宿傩代为拼点
    # ------------------------------------------------------------------
    def _on_before_select(self, ctx: EventContext) -> None:
        engine = ctx.engine
        char: Character = ctx.data["character"]
        owner = self.owner
        if not owner:
            return

        if char.team != owner.team:
            blocked_round = self.blocked_pursuit_targets_round.get(id(char))
            if blocked_round == engine.round_num and ctx.data["slot_idx"] > 0:
                engine.log(f"  [头抬太高了] {char.name} 本轮额外行动被取消！")
                ctx.cancelled = True
                return

        if char is not owner:
            return

        enemies_high_bleed = any(
            e.get_status_stacks("流血") >= 10 for e in engine.get_enemies(owner)
        )
        new_hand: List[Skill] = []
        for sk in owner.available_skills:
            if isinstance(sk, SukunaSkill3) and enemies_high_bleed:
                fuga = FugaSkill()
                new_hand.append(fuga)
                engine.log(f"  [「灶」显现] 检测到敌方流血 ≥ 10，【{sk.name}】转化为【{fuga.name}】！")
            else:
                new_hand.append(sk)
        owner.available_skills = new_hand

        for sk in owner.available_skills:
            if isinstance(sk, FugaSkill):
                if self.domain_active():
                    sk.is_aoe = True
                    sk.target_type = "all_enemies"
                else:
                    sk.is_aoe = False
                    sk.target_type = "single_enemy"

            if self.domain_active() and not isinstance(sk, ReverseCursedTechniqueSkill):
                sk.unbreakable = True

        if self.rct_count > 0 and not any(
            isinstance(s, ReverseCursedTechniqueSkill) for s in owner.available_skills
        ):
            self._temp_rct_skill = ReverseCursedTechniqueSkill()
            owner.available_skills.insert(0, self._temp_rct_skill)

    def _on_after_select(self, ctx: EventContext) -> None:
        action: Action = ctx.data["action"]
        owner = self.owner
        if not owner:
            return

        if action.actor is owner:
            if self._temp_rct_skill and self._temp_rct_skill in owner.available_skills:
                owner.available_skills.remove(self._temp_rct_skill)
            self._temp_rct_skill = None

            if action.skill and self.domain_active():
                action.skill.unbreakable = True
            return

        if self.domain_active() and action.actor.team != owner.team and action.skill is not None:
            tgt = action.actor
            loss = max(1, floor_mul(tgt.max_hp, 1, 100))
            engine = ctx.engine
            engine.log(f"  [伏魔御厨子] {tgt.name} 在领域内行动，受到 {loss} 点切割伤害！")
            engine.apply_damage(
                attacker=owner,
                target=tgt,
                raw_damage=loss,
                source_type="domain_dot",
                trigger_on_hit_statuses=False,
                allow_breath=False,
            )

    def _on_resolve_matchups(self, ctx: EventContext) -> None:
        """
        打「双嘴」或「四手」时，让宿傩前去迎击拼点；
        若敌方拼赢或碎币反击，最终受伤的对象依然还原为「双嘴」或「四手」。
        """
        owner = self.owner
        if not owner or not owner.alive or not self.true_form_triggered:
            return
        if not ((self.mouth_part and self.mouth_part.alive) or (self.arms_part and self.arms_part.alive)):
            return

        actions: List[Action] = ctx.data["actions"]

        def _can_clash(act: Action) -> bool:
            return (
                not act.cancelled
                and act.skill is not None
                and act.clashable
                and act.skill.clashable
                and (not act.skill.is_aoe or act.skill.clash_aoe)
            )

        # 找出宿傩所有可拼点动作，先标记已经和直指宿傩的敌人互指的动作
        sukuna_actions = [a for a in actions if a.actor is owner and _can_clash(a)]
        already_paired_sukuna: Set[int] = set()
        for s_act in sukuna_actions:
            for e_act in actions:
                if (
                    e_act.actor is not owner
                    and _can_clash(e_act)
                    and owner in e_act.targets
                    and e_act.actor in s_act.targets
                ):
                    already_paired_sukuna.add(id(s_act))
                    break

        free_sukuna = [s for s in sukuna_actions if id(s) not in already_paired_sukuna]
        if not free_sukuna:
            return

        for e_act in actions:
            if not free_sukuna:
                break
            if e_act.actor.team == owner.team or not _can_clash(e_act):
                continue

            part_tgt: Optional[Character] = None
            for t in e_act.targets:
                if self._is_body_part(t) and t.alive:
                    part_tgt = t
                    break
            if part_tgt is None:
                continue

            # 优先选用本来就指着该敌人的宿傩动作，否则任选一个空闲的宿傩动作
            chosen_s: Optional[Action] = None
            for s_act in free_sukuna:
                if e_act.actor in s_act.targets:
                    chosen_s = s_act
                    break
            if chosen_s is None:
                chosen_s = free_sukuna[0]

            free_sukuna.remove(chosen_s)
            e_act.extra["redirected_from_part"] = part_tgt
            e_act.targets = [owner if t is part_tgt else t for t in e_act.targets]
            if e_act.actor not in chosen_s.targets and not chosen_s.skill.is_aoe:
                chosen_s.targets = [e_act.actor]

            ctx.engine.log(
                f"  [护体迎击] {e_act.actor.name} 攻击【{part_tgt.name}】，{owner.name} 上前代为拼点！"
            )

    # ------------------------------------------------------------------
    # 掷骰与拼点阶段（免改 combat.py 接管含宿傩的拼点）
    # ------------------------------------------------------------------
    def _on_before_roll(self, ctx: EventContext) -> None:
        actor: Character = ctx.data["actor"]
        skill: Skill = ctx.data["skill"]
        roll_state: Dict[str, Any] = ctx.data["roll_state"]
        owner = self.owner
        if not owner or not owner.alive:
            return

        if actor is owner:
            if self.excitement > 0:
                base_exc = min(5, self.excitement)
                coin_exc = max(0, self.excitement - 5)
                roll_state["base_power"] += base_exc
                if coin_exc > 0:
                    for c in roll_state["coins"]:
                        c.value += coin_exc

            ce_power = floor_div(self.cursed_energy, 5)
            if ce_power > 0:
                roll_state["base_power"] += ce_power

            if self.mouth_part and self.mouth_part.alive:
                roll_state["base_power"] += 2

            if self.domain_active():
                skill.unbreakable = True
                for c in roll_state["coins"]:
                    c.unbreakable = True

        elif actor.team != owner.team:
            action: Action = roll_state["action"]
            clash_opp = roll_state.get("clash_opponent")
            targeting_sukuna = (
                (clash_opp is owner)
                or (owner in action.targets)
                or (action.extra.get("redirected_from_part") is not None)
            )
            skey = skill.skill_id or skill.name
            if targeting_sukuna:
                self._enemy_active_skill_vs_sukuna[id(actor)] = skey
            else:
                self._enemy_active_skill_vs_sukuna.pop(id(actor), None)

            if targeting_sukuna and self.technique_active() and self.mahoraga_enabled:
                lvl = self.adapt1_skills.get(skey, 0)
                if lvl >= 15:
                    roll_state["base_power"] = 0
                    for c in roll_state["coins"]:
                        c.value = 0
                    ctx.engine.log(
                        f"  [适应一·15层] {actor.name} 的【{skill.name}】数值已完全被归零！"
                    )
                elif lvl >= 10:
                    roll_state["base_power"] -= 3
                    ctx.engine.log(
                        f"  [适应一·10层] {actor.name} 的【{skill.name}】对宿傩威力 -3！"
                    )

    def _on_before_roll_detect_opp_buff(self, ctx: EventContext) -> None:
        actor: Character = ctx.data["actor"]
        skill: Skill = ctx.data["skill"]
        roll_state: Dict[str, Any] = ctx.data["roll_state"]
        owner = self.owner
        if not owner or actor is owner:
            return

        had_buff = (
            actor.get_status_stacks("强壮") > 0
            or actor.get_status_stacks("硬币威力强化") > 0
            or roll_state["base_power"] > skill.base_power
            or any(rc.value > oc.value for rc, oc in zip(roll_state["coins"], skill.coins))
        )
        roll_state["action"].extra["had_power_increase"] = had_buff

    def _on_after_roll(self, ctx: EventContext) -> None:
        actor: Character = ctx.data["actor"]
        skill: Skill = ctx.data["skill"]
        roll_state: Dict[str, Any] = ctx.data["roll_state"]
        owner = self.owner
        if not owner or actor is owner:
            return

        if self.technique_active() and self.mahoraga_enabled:
            action: Action = roll_state["action"]
            clash_opp = roll_state.get("clash_opponent")
            targeting_sukuna = (
                (clash_opp is owner)
                or (owner in action.targets)
                or (action.extra.get("redirected_from_part") is not None)
            )
            if targeting_sukuna:
                skey = skill.skill_id or skill.name
                if self.adapt1_skills.get(skey, 0) >= 15:
                    roll_state["total"] = 0

    def _on_clash_start(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive:
            return
        act1: Action = ctx.data["action1"]
        act2: Action = ctx.data["action2"]
        if owner not in (act1.actor, act2.actor):
            return

        opp_act = act2 if act1.actor is owner else act1
        if opp_act.skill:
            skey = opp_act.skill.skill_id or opp_act.skill.name
            self._enemy_active_skill_vs_sukuna[id(opp_act.actor)] = skey

        self.cursed_energy += 1
        ctx.engine.log(f"  [咒术师] {owner.name} 拼点获得 1 层咒力（当前: {self.cursed_energy}）")

        if self.technique_active() and self.mahoraga_enabled and opp_act.skill:
            skey = opp_act.skill.skill_id or opp_act.skill.name
            self.adapt1_skills[skey] = self.adapt1_skills.get(skey, 0) + 1
            ctx.engine.log(
                f"  [适应一] {opp_act.skill.name}（适应1：{self.adapt1_skills[skey]}）"
            )

    def _on_clash_start_intercept(self, ctx: EventContext) -> None:
        """
        无需改动 combat.py：利用 CLASH_START 取消原循环，在此运行支持回读 roll1/roll2 的拼点循环，
        并在分出胜负后把“打双嘴/四手”被重定向的受击目标还原回对应部位。
        """
        owner = self.owner
        if not owner or not owner.alive:
            return
        engine = ctx.engine
        act1: Action = ctx.data["action1"]
        act2: Action = ctx.data["action2"]
        if owner not in (act1.actor, act2.actor):
            return
        if not act1.actor.alive or not act2.actor.alive:
            return

        ctx.cancelled = True

        tie_count = 0
        winner_act: Optional[Action] = None
        loser_act: Optional[Action] = None
        win_roll = 0
        lose_roll = 0

        while True:
            r1 = engine.roll_skill(act1, clash_opponent=act2.actor)
            r2 = engine.roll_skill(act2, clash_opponent=act1.actor)
            clash_ctx = engine.events.emit(
                EventType.CLASH_ROLL, engine, action1=act1, action2=act2, roll1=r1, roll2=r2
            )
            r1 = floor_val(clash_ctx.data["roll1"])
            r2 = floor_val(clash_ctx.data["roll2"])

            if r1 > r2:
                winner_act, loser_act = act1, act2
                win_roll, lose_roll = r1, r2
                break
            elif r2 > r1:
                winner_act, loser_act = act2, act1
                win_roll, lose_roll = r2, r1
                break
            else:
                tie_count += 1
                engine.events.emit(
                    EventType.CLASH_TIE,
                    engine,
                    action1=act1,
                    action2=act2,
                    roll=r1,
                    tie_count=tie_count,
                )
                if tie_count >= engine.max_clash_ties:
                    engine.log(
                        f"  [平局取消] 连续 {engine.max_clash_ties} 次平局（点数 {r1}），直接取消双方技能！"
                    )
                    engine.events.emit(EventType.CLASH_CANCEL_99, engine, action1=act1, action2=act2)
                    return

        assert winner_act is not None and loser_act is not None
        assert winner_act.skill is not None and loser_act.skill is not None

        # 如果敌方原本打的是「双嘴」或「四手」，拼点完成后把受击目标还原为原部位
        opp_act = act2 if act1.actor is owner else act1
        part_tgt: Optional[Character] = opp_act.extra.get("redirected_from_part")
        if part_tgt is not None:
            opp_act.targets = [part_tgt if t is owner else t for t in opp_act.targets]

        loser_act.skill.on_clash_lose(engine, loser_act, winner_act, lose_roll)
        winner_act.skill.on_clash_win(engine, winner_act, loser_act, win_roll)

        unbreakable_act = engine._build_unbreakable_action(winner_act, loser_act, from_aoe=False)
        destroyed = unbreakable_act is None

        if destroyed:
            engine.log(
                f"  [拼点结果] {winner_act.actor.name}({win_roll}) 胜出！ "
                f"{loser_act.actor.name}({lose_roll}) 技能被摧毁"
            )
            engine.events.emit(
                EventType.CLASH_LOSE,
                engine,
                winner_action=winner_act,
                loser_action=loser_act,
                roll=lose_roll,
                destroyed=True,
            )
            engine.events.emit(EventType.SKILL_DESTROYED, engine, action=loser_act)
        else:
            assert unbreakable_act.skill is not None
            engine.log(
                f"  [拼点结果] {winner_act.actor.name}({win_roll}) 胜出！ "
                f"{loser_act.actor.name}({lose_roll}) 触发【不可摧毁】，保留为 [{unbreakable_act.skill.format_formula()}]"
            )
            engine.events.emit(
                EventType.CLASH_LOSE,
                engine,
                winner_action=winner_act,
                loser_action=loser_act,
                roll=lose_roll,
                destroyed=False,
                unbreakable_action=unbreakable_act,
            )

        engine.events.emit(
            EventType.CLASH_WIN,
            engine,
            winner_action=winner_act,
            loser_action=loser_act,
            roll=win_roll,
        )

        engine._execute_skill_hits(winner_act, win_roll)

        if unbreakable_act is not None and unbreakable_act.actor.alive and not engine.check_victory():
            engine._resolve_unbreakable_attack(unbreakable_act)

    def _on_clash_roll(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive:
            return
        act1: Action = ctx.data["action1"]
        act2: Action = ctx.data["action2"]
        if owner not in (act1.actor, act2.actor):
            return

        r1: int = ctx.data["roll1"]
        r2: int = ctx.data["roll2"]
        is_p1 = act1.actor is owner
        sukuna_act = act1 if is_p1 else act2
        opp_act = act2 if is_p1 else act1
        s_roll = r1 if is_p1 else r2
        o_roll = r2 if is_p1 else r1

        # 1) 黑闪：仅基础技能触发，点数相差 1（领先或落后都算）时转红币并使本次点数 ×2.5
        if (
            sukuna_act.skill
            and "sukuna_basic" in sukuna_act.skill.tags
            and abs(s_roll - o_roll) == 1
        ):
            engine = ctx.engine
            sukuna_act.skill.unbreakable = True
            sukuna_act.extra["black_flash_triggered"] = True

            s_roll = floor_mul(s_roll, 25, 10)
            ctx.data["roll1" if is_p1 else "roll2"] = s_roll

            engine.apply_status(owner, "呼吸", 20, source=owner)

            if self.black_flash_mult_pct == 100:
                self.black_flash_mult_pct = 120
            else:
                self.black_flash_mult_pct += 20

            if self.burnout_active:
                self.burnout_active = False
                engine.log(f"  [黑闪] {owner.name} 立即解除术式熔断！")
            self.cursed_energy += 15

            engine.log(
                f"  [★ 黑闪 ★] {owner.name} 与 {opp_act.actor.name} 点数差 1！"
                f"技能转为红币且点数 ×2.5 -> {s_roll}，获得20层呼吸、15层咒力（当前:{self.cursed_energy}），"
                f"回复/负面倍率提升至 {self.black_flash_mult_pct / 100:.1f}x！"
            )
            self._try_activate_domain(engine)

        # 2) 咒术师：点数小于对手时，消耗 5 层咒力将点数提升至与对手一致（强制打平）
        if s_roll < o_roll and self.cursed_energy >= 5:
            self.cursed_energy -= 5
            self.consumed_cursed_energy += 5
            s_roll = o_roll
            ctx.data["roll1" if is_p1 else "roll2"] = s_roll
            ctx.engine.log(
                f"  [咒术师] {owner.name} 点数不敌，消耗 5 层咒力将点数提升至 {s_roll} 与 {opp_act.actor.name} 打平！"
                f"（剩余咒力: {self.cursed_energy}）"
            )
            self._try_activate_domain(ctx.engine)

        sukuna_act.extra["last_clash_diff"] = s_roll - o_roll

        # 3) 兴致：打平（整场首次+2，之后+1）
        if s_roll == o_roll:
            req = 2 if not self.first_tie_or_lose_done else 1
            self.first_tie_or_lose_done = True
            added = self._add_excitement(req)
            ctx.engine.log(
                f"  [兴致] 拼点打平，{owner.name} 获得 {added} 层兴致（当前: {self.excitement}/{MAX_EXCITEMENT}）"
            )

    def _on_clash_win(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive:
            return
        win_act: Action = ctx.data["winner_action"]
        lose_act: Action = ctx.data["loser_action"]
        if win_act.actor is not owner:
            return

        engine = ctx.engine
        opp = lose_act.actor

        # 兴致减少条件：敌方拥有加威力状态，且点数相差 >= 5 时拼赢
        has_power_buff = bool(lose_act.extra.get("had_power_increase"))
        diff = win_act.extra.get("last_clash_diff", 0)
        if has_power_buff and diff >= 5 and self.excitement > 0:
            self.excitement -= 1
            engine.log(
                f"  [兴致下降] 以 {diff} 点优势压制持有威力增益的 {opp.name}，"
                f"{owner.name} 兴致 -1（当前: {self.excitement}/{MAX_EXCITEMENT}）"
            )

        # 头抬太高了：取消这个拼输的敌人本轮额外行动
        if engine.round_num >= self.head_too_high_ready_round:
            self.head_too_high_ready_round = engine.round_num + 2
            self.blocked_pursuit_targets_round[id(opp)] = engine.round_num
            removed_any = False
            for q_act in list(engine.action_queue):
                if q_act.actor is opp and not q_act.is_unbreakable_attack:
                    q_act.cancelled = True
                    removed_any = True
            engine.log(
                f"  [头抬太高了] {owner.name} 拼点胜出，本轮禁止 {opp.name} 除碎币外的一切额外行动！"
                + ("（已拦截队列中追击）" if removed_any else "")
            )

        # 原身启动：神武解追击
        if self.true_form_triggered and opp.alive:
            engine.log(f"  [神武解] {owner.name} 拼点胜利，发动【神武解】追击！")
            engine.enqueue_action(
                Action(
                    actor=owner,
                    skill=ShenWuJieSkill(),
                    targets=[opp],
                    consume_from_hand=False,
                    is_pursuit=True,
                    clashable=False,
                ),
                front=True,
            )

        # 双嘴四手同时在场拼点胜利统计 -> 5胜后开启3回合全胜空间斩挑战
        if self._both_parts_alive():
            if self.wcs_challenge_active:
                if not self.wcs_challenge_failed:
                    self.wcs_challenge_wins += 1
                    engine.log(
                        f"  [空间斩蓄势] 三回合内拼点胜利（当前已胜 {self.wcs_challenge_wins} 次，"
                        f"剩余 {self.wcs_challenge_turns_left} 回合）"
                    )
            else:
                self.both_parts_clash_wins += 1
                engine.log(
                    f"  [原身连胜] 双嘴四手同时在场拼点胜利：{self.both_parts_clash_wins}/5"
                )
                if self.both_parts_clash_wins >= 5:
                    self.wcs_challenge_active = True
                    self.wcs_challenge_turns_left = 3
                    self.wcs_challenge_started_turn = engine.global_turn
                    self.wcs_challenge_wins = 0
                    self.wcs_challenge_failed = False
                    engine.log(
                        f"  [★ 空间斩前置达成 ★] 双嘴四手在场已拼胜5次！"
                        f"接下来 3 回合内若拼点全部胜利，将再释放一次【空间斩】！"
                    )

    def _on_clash_lose(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive:
            return
        lose_act: Action = ctx.data["loser_action"]
        if lose_act.actor is not owner:
            return

        req = 2 if not self.first_tie_or_lose_done else 1
        self.first_tie_or_lose_done = True
        added = self._add_excitement(req)
        ctx.engine.log(
            f"  [兴致] 拼点失利，{owner.name} 获得 {added} 层兴致（当前: {self.excitement}/{MAX_EXCITEMENT}）"
        )

        if self.wcs_challenge_active:
            self.wcs_challenge_failed = True
            ctx.engine.log("  [空间斩蓄势中断] 宿傩拼点失利，3回合全胜条件中断并重置！")
            self._reset_wcs_challenge()

    def _on_clash_cancel_99(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not self.wcs_challenge_active:
            return
        act1: Action = ctx.data["action1"]
        act2: Action = ctx.data["action2"]
        if owner in (act1.actor, act2.actor):
            self.wcs_challenge_failed = True
            ctx.engine.log("  [空间斩蓄势中断] 拼点平局取消未获胜，3回合全胜条件重置！")
            self._reset_wcs_challenge()

    # ------------------------------------------------------------------
    # 拦截追击 / 碎红币反拼点
    # ------------------------------------------------------------------
    def _on_unopposed_start(self, ctx: EventContext) -> None:
        action: Action = ctx.data["action"]
        if self.blocked_pursuit_targets_round.get(id(action.actor)) == ctx.engine.round_num:
            if action.is_pursuit or action.extra.get("is_extra_action"):
                action.cancelled = True
                ctx.engine.log(f"  [头抬太高了] 拦截了 {action.actor.name} 的额外行动！")

    def _on_unbreakable_start(self, ctx: EventContext) -> None:
        """好好的品尝自己的那份不幸不就好了：敌方碎红币进攻宿傩时，使用额外2技能拼点"""
        owner = self.owner
        if not owner or not owner.alive:
            return
        act: Action = ctx.data["action"]
        if act.actor.team == owner.team or act.skill is None:
            return

        targets_sukuna = (owner in act.targets) or (act.skill.is_aoe)
        if not targets_sukuna:
            return

        engine = ctx.engine
        engine.log(
            f"  [好好的品尝自己的那份不幸不就好了] 面对 {act.actor.name} 的碎红币进攻，"
            f"{owner.name} 使用额外【捌·连斩】迎击拼点！"
        )
        counter_act = Action(
            actor=owner,
            skill=SukunaSkill2(),
            targets=[act.actor],
            consume_from_hand=False,
            clashable=True,
        )
        r_sukuna = engine.roll_skill(counter_act, clash_opponent=act.actor)
        r_enemy = engine.roll_skill(act, clash_opponent=owner)
        engine.log(f"  [迎击拼点] {owner.name}({r_sukuna}) VS {act.actor.name}({r_enemy})")

        if r_sukuna >= r_enemy:
            self._blocked_unbreakable_actions.add(id(act))
            engine.log(f"  [迎击胜出] {owner.name} 压制碎红币，使其无法对宿傩生效并反击！")
            engine._execute_skill_hits(counter_act, r_sukuna)
        else:
            engine.log(f"  [迎击失利] 碎红币继续命中 {owner.name}！")

    # ------------------------------------------------------------------
    # 伤害计算 / 免伤 / 抱歉切成三段 / 受击同步 / 适应一二三 / 安慰奖
    # ------------------------------------------------------------------
    def _on_calc_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        owner = self.owner
        if not owner or not owner.alive:
            return

        if dmg_ctx.source_type == "dot_bleed_burst":
            dmg_ctx.ignore_protection_fragile = True
            return
        if dmg_ctx.source_type == "domain_dot" or dmg_ctx.source_type.startswith("dot_"):
            return

        if dmg_ctx.attacker is owner:
            if self.cursed_energy > 0 and dmg_ctx.damage > 0:
                dmg_ctx.mul_percent(100 + self.cursed_energy)

            if self.true_form_triggered:
                dmg_ctx.mul_percent(80)

            if self.mouth_part and self.mouth_part.alive:
                dmg_ctx.mul_percent(120)

            action: Optional[Action] = ctx.data.get("action")
            if action and action.extra.get("sorry_double_damage"):
                dmg_ctx.mul_percent(200)
                ctx.engine.log(
                    f"  [抱歉抱歉，原本想把你切成三段的] 兴致已满强化追击伤害 ×2 -> {dmg_ctx.damage}！"
                )

            if self.technique_active() and self.mahoraga_enabled:
                for st_name, st_obj in dmg_ctx.target.statuses.items():
                    if st_obj.is_buff and st_obj.stacks > 0:
                        lvl = self.adapt3_buffs.get(st_name, 0)
                        if lvl >= 15:
                            dmg_ctx.ignore_protection_fragile = True
                        if lvl >= 5:
                            dmg_ctx.mul_percent(150)
                            ctx.engine.log(
                                f"  [适应三·5层] {dmg_ctx.target.name} 持有 {st_name}，宿傩伤害 ×1.5！"
                            )
                            break

    def _on_before_take_damage_record_pre(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        dmg_ctx.extra["pre_take_dmg"] = dmg_ctx.damage

    def _on_before_take_damage_sorry_passive(self, ctx: EventContext) -> None:
        """
        抱歉抱歉，原本想把你切成三段的：
        若攻击命中时敌方拥有buff抵消伤害，则使自己加二兴致，并立即追击一次二技能；
        若兴致已满，则不加兴致，依然追击二技能并将该追击二技能伤害 ×2。
        """
        owner = self.owner
        if not owner or not owner.alive:
            return

        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.attacker is not owner or dmg_ctx.target is owner:
            return
        if dmg_ctx.target.team == owner.team:
            return
        if dmg_ctx.skill is None or dmg_ctx.source_type == "domain_dot" or dmg_ctx.source_type.startswith("dot_"):
            return

        action: Optional[Action] = ctx.data.get("action")
        if action and action.extra.get("sorry_three_pieces_pursuit"):
            return

        if dmg_ctx.ignore_protection_fragile or dmg_ctx.extra.get("ignore_damage_reduction"):
            return

        target = dmg_ctx.target
        has_buff = any(st.is_buff and st.stacks > 0 for st in target.statuses.values())
        pre_dmg = dmg_ctx.extra.get("pre_take_dmg", dmg_ctx.damage)

        buff_offset_damage = (
            target.get_status_stacks("守护") > 0
            or (has_buff and (dmg_ctx.cancelled or dmg_ctx.damage < pre_dmg))
        )
        if not buff_offset_damage:
            return

        engine = ctx.engine
        if self.excitement >= MAX_EXCITEMENT:
            engine.log(
                f"  [抱歉抱歉，原本想把你切成三段的] {target.name} 以增益抵消了伤害！"
                f"{owner.name} 兴致已满（{self.excitement}/{MAX_EXCITEMENT}），"
                f"不加兴致并立即追击伤害 ×2 的【捌·连斩】！"
            )
            engine.enqueue_action(
                Action(
                    actor=owner,
                    skill=SukunaSkill2(),
                    targets=[target],
                    consume_from_hand=False,
                    is_pursuit=True,
                    clashable=False,
                    extra={
                        "sorry_three_pieces_pursuit": True,
                        "sorry_double_damage": True,
                    },
                ),
                front=True,
            )
        else:
            added = self._add_excitement(2)
            engine.log(
                f"  [抱歉抱歉，原本想把你切成三段的] {target.name} 以增益抵消了伤害！"
                f"{owner.name} 兴致 +{added}（当前: {self.excitement}/{MAX_EXCITEMENT}），并立即追击【捌·连斩】！"
            )
            engine.enqueue_action(
                Action(
                    actor=owner,
                    skill=SukunaSkill2(),
                    targets=[target],
                    consume_from_hand=False,
                    is_pursuit=True,
                    clashable=False,
                    extra={"sorry_three_pieces_pursuit": True},
                ),
                front=True,
            )

    def _on_before_take_damage(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        owner = self.owner
        if not owner or not owner.alive or dmg_ctx.target is not owner:
            return

        action: Optional[Action] = ctx.data.get("action")
        if action and id(action) in self._blocked_unbreakable_actions:
            dmg_ctx.cancelled = True
            return

        ignore_reduction = bool(dmg_ctx.extra.get("ignore_damage_reduction"))

        if (
            not ignore_reduction
            and self.true_form_triggered
            and not dmg_ctx.extra.get("from_body_part")
            and (
                (self.mouth_part and self.mouth_part.alive)
                or (self.arms_part and self.arms_part.alive)
            )
        ):
            dmg_ctx.cancelled = True
            ctx.engine.log(f"  [为咒术而生的肉体] {owner.name} 本体免疫本次直接伤害！")
            return

        if (
            not ignore_reduction
            and dmg_ctx.skill is not None
            and dmg_ctx.attacker is not None
            and dmg_ctx.attacker.team != owner.team
            and self.technique_active()
            and self.mahoraga_enabled
        ):
            skey = dmg_ctx.skill.skill_id or dmg_ctx.skill.name
            lvl = self.adapt1_skills.get(skey, 0)
            if lvl >= 15:
                dmg_ctx.damage = 0
                dmg_ctx.cancelled = True
                ctx.engine.log(f"  [适应一·15层] 完全免疫 {dmg_ctx.skill.name} 的伤害！")
                return
            elif lvl >= 5:
                dmg_ctx.mul_percent(70)

        if (
            not ignore_reduction
            and dmg_ctx.source_type.startswith("dot_")
            and self.technique_active()
            and self.mahoraga_enabled
        ):
            dot_map = {
                "dot_burn": "烧伤",
                "dot_tremor_burst": "震颤",
                "dot_bleed": "流血",
                "dot_bleed_burst": "流血",
                "dot_corrosion_hit": "腐蚀",
                "dot_corrosion_end": "腐蚀",
                "dot_rupture": "破裂",
            }
            st_name = dot_map.get(dmg_ctx.source_type)
            if st_name:
                lvl = self.adapt2_debuffs.get(st_name, 0)
                if lvl >= 15:
                    dmg_ctx.damage = 0
                    dmg_ctx.cancelled = True
                    return
                elif lvl >= 5:
                    dmg_ctx.damage = floor_div(dmg_ctx.damage, 2)

        if (
            not ignore_reduction
            and self.true_form_triggered
            and self.excitement >= 7
            and self.rct_count >= 1
            and dmg_ctx.skill is not None
            and dmg_ctx.damage >= 100
        ):
            self.rct_count -= 1
            loss = max(1, floor_mul(owner.max_hp, 5, 100))
            owner.hp = max(0, owner.hp - loss)
            dmg_ctx.cancelled = True
            if dmg_ctx.attacker is not None:
                self._nullified_enemy_actors.add(id(dmg_ctx.attacker))
            if action:
                action.extra["nullified_by_consolation"] = True
            ctx.engine.log(
                f"  [就当给你一个安慰奖吧] 无效化 {dmg_ctx.damage} 点技能伤害与负面，"
                f"{owner.name} 失去 {loss} HP 与 1 次反转术式！"
            )
            ctx.engine._check_death(owner, killer=dmg_ctx.attacker)

    def _on_before_take_damage_wcs_restore(self, ctx: EventContext) -> None:
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        if dmg_ctx.extra.get("ignore_damage_reduction") and "wcs_min_damage" in dmg_ctx.extra:
            dmg_ctx.damage = max(dmg_ctx.damage, dmg_ctx.extra["wcs_min_damage"])

    def _on_after_take_damage(self, ctx: EventContext) -> None:
        engine = ctx.engine
        dmg_ctx: DamageContext = ctx.data["damage_context"]
        actual_loss: int = ctx.data["actual_loss"]
        owner = self.owner
        if not owner:
            return

        if (
            self.true_form_triggered
            and self._is_body_part(dmg_ctx.target)
            and actual_loss > 0
            and owner.alive
        ):
            sync_loss = (
                floor_div(actual_loss, 2)
                if dmg_ctx.source_type == "aoe"
                else actual_loss
            )
            if sync_loss > 0:
                owner.hp = max(0, owner.hp - sync_loss)
                engine.log(
                    f"  [肉体同步] {dmg_ctx.target.name} 受创，同步 {sync_loss} 点伤害至 {owner.name} "
                    f"(HP: {owner.hp}/{owner.max_hp})"
                )
                engine._check_death(owner, killer=dmg_ctx.attacker)
                engine.bgm.evaluate(reason="宿傩部位同步扣血")
            return

        if dmg_ctx.attacker is owner and dmg_ctx.target is not owner and dmg_ctx.trigger_on_hit_statuses:
            tgt = dmg_ctx.target

            if (
                self.technique_active()
                and dmg_ctx.skill is not None
                and "sukuna_basic" in dmg_ctx.skill.tags
                and tgt.alive
            ):
                coin_count = len(dmg_ctx.skill.coins)
                if coin_count > 0:
                    engine.apply_status(tgt, "流血", coin_count, source=owner)

            if self.technique_active() and self.mahoraga_enabled and tgt.alive:
                # 适应二 15层：自身每枚硬币均可施加 1 层该负面效果
                if dmg_ctx.skill is not None and len(dmg_ctx.skill.coins) > 0:
                    coin_count = len(dmg_ctx.skill.coins)
                    for st_name, lvl in list(self.adapt2_debuffs.items()):
                        if lvl >= 15 and tgt.alive:
                            engine.apply_status(tgt, st_name, coin_count, source=owner)

                for st_name in list(tgt.statuses.keys()):
                    st_obj = tgt.statuses[st_name]
                    if st_obj.is_buff and st_obj.stacks > 0:
                        self.adapt3_buffs[st_name] = self.adapt3_buffs.get(st_name, 0) + 1
                        lvl = self.adapt3_buffs[st_name]
                        engine.log(f"  [适应三] {st_name}（适应3：{lvl}）")
                        if lvl >= 10:
                            halved = floor_div(st_obj.stacks, 2)
                            engine.set_status_stacks(tgt, st_name, halved)
                            engine.log(f"  [适应三·10层] {tgt.name} 的 {st_name} 层数减半为 {halved}！")

        if dmg_ctx.target is owner and actual_loss > 0:
            self._check_mahoraga_unlock(engine)

            if actual_loss >= 150 and self.technique_active():
                self.heavy_hit_count_150 += 1
                if self.heavy_hit_count_150 == 1:
                    self.chimera_heal_disabled = True
                    engine.log(f"  [嵌合兽破坏] {owner.name} 单次承受 {actual_loss} 伤害，失去嵌合兽回血！")
                elif self.heavy_hit_count_150 >= 2:
                    self.mahoraga_enabled = False
                    self.mahoraga_permanently_disabled = True
                    self.adapt1_skills.clear()
                    self.adapt2_debuffs.clear()
                    self.adapt3_buffs.clear()
                    engine.log(f"  [魔虚罗破坏] {owner.name} 再次承受 ≥150 伤害，魔虚罗失效并清空所有适应！")

            if (
                dmg_ctx.skill is not None
                and dmg_ctx.attacker is not None
                and dmg_ctx.attacker.team != owner.team
                and self.technique_active()
                and self.mahoraga_enabled
            ):
                skey = dmg_ctx.skill.skill_id or dmg_ctx.skill.name
                self.adapt1_skills[skey] = self.adapt1_skills.get(skey, 0) + 1
                engine.log(f"  [适应一] {dmg_ctx.skill.name}（适应1：{self.adapt1_skills[skey]}）")

            if (
                dmg_ctx.source_type.startswith("dot_")
                and self.technique_active()
                and self.mahoraga_enabled
            ):
                dot_map = {
                    "dot_burn": "烧伤",
                    "dot_tremor_burst": "震颤",
                    "dot_bleed": "流血",
                    "dot_bleed_burst": "流血",
                    "dot_corrosion_hit": "腐蚀",
                    "dot_corrosion_end": "腐蚀",
                    "dot_rupture": "破裂",
                }
                st_name = dot_map.get(dmg_ctx.source_type)
                if st_name:
                    self.adapt2_debuffs[st_name] = self.adapt2_debuffs.get(st_name, 0) + 1
                    engine.log(
                        f"  [适应二] {st_name}（适应2：{self.adapt2_debuffs[st_name]}）"
                    )

    def _on_status_apply(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive:
            return

        target: Character = ctx.data["target"]
        source: Optional[Character] = ctx.data.get("source")
        status_name: str = ctx.data["status_name"]
        status_obj: Optional[StatusEffect] = ctx.data.get("status_obj")

        is_debuff = (
            (status_obj.is_debuff if status_obj is not None else False)
            or (STATUS_CATEGORIES.get(status_name) == "debuff")
        )

        # 1) 敌方被宿傩判定为本技能无效（适应一15层 / 安慰奖）：阻止对宿傩施加负面效果
        if target is owner and is_debuff and source is not None:
            if id(source) in self._nullified_enemy_actors:
                ctx.cancelled = True
                return
            if self.technique_active() and self.mahoraga_enabled:
                skey = self._enemy_active_skill_vs_sukuna.get(id(source))
                if skey and self.adapt1_skills.get(skey, 0) >= 15:
                    ctx.cancelled = True
                    ctx.engine.log(
                        f"  [适应一·15层] {source.name} 的技能所有数值已归零，无法施加 {status_name}！"
                    )
                    return

        # 2) 适应二（>=15层）：不再会被对应负面效果施加
        if (
            target is owner
            and is_debuff
            and self.technique_active()
            and self.mahoraga_enabled
            and self.adapt2_debuffs.get(status_name, 0) >= 15
        ):
            ctx.cancelled = True
            ctx.engine.log(f"  [适应二·15层] {owner.name} 已完全免疫 {status_name} 的施加！")
            return

        # 3) 宿傩主动施加负面效果时的倍率叠加
        if source is owner and target is not owner and is_debuff:
            if self.arms_part and self.arms_part.alive:
                ctx.data["stacks"] = floor_mul(ctx.data["stacks"], 2, 1)

            if self.black_flash_mult_pct > 100:
                ctx.data["stacks"] = floor_mul(ctx.data["stacks"], self.black_flash_mult_pct, 100)

            if self.technique_active() and self.mahoraga_enabled:
                for st_name, st_val in target.statuses.items():
                    if st_val.is_buff and st_val.stacks > 0 and self.adapt3_buffs.get(st_name, 0) >= 5:
                        ctx.data["stacks"] = floor_mul(ctx.data["stacks"], 15, 10)
                        break

    # ------------------------------------------------------------------
    # 锁血一阶段（也就是说…是？赢了）、二阶段（原身启动！）与部位破坏提示
    # ------------------------------------------------------------------
    def _on_part_death_notify(self, ctx: EventContext) -> None:
        owner = self.owner
        char: Character = ctx.data["character"]
        if not owner or ctx.cancelled or char.hp > 0:
            return
        if not self._is_body_part(char):
            return

        other = self.arms_part if char is self.mouth_part else self.mouth_part
        other_alive = other is not None and other.alive and other.hp > 0
        if not other_alive and owner.alive:
            ctx.engine.log(
                f"  [为咒术而生的肉体] 「双嘴」与「四手」已全部破坏，{owner.name} 本体恢复可受伤害状态！"
            )
            if self.wcs_challenge_active:
                self._reset_wcs_challenge()

    def _on_character_death(self, ctx: EventContext) -> None:
        owner = self.owner
        char: Character = ctx.data["character"]
        killer: Optional[Character] = ctx.data.get("killer")
        dmg_ctx: Optional[DamageContext] = ctx.data.get("damage_context")
        if char is not owner or owner is None or owner.hp > 0:
            return

        if dmg_ctx and dmg_ctx.extra.get("ignore_death_immunity"):
            return

        engine = ctx.engine

        if not self.win_revive_triggered:
            self.win_revive_triggered = True
            owner.hp = 1
            ctx.cancelled = True
            engine.log(f"  [也就是说…是？赢了] {owner.name} 锁血至 1 HP！")
            self.execute_rct(engine, owner)

            target = killer if (killer and killer.alive) else None
            if target is None:
                enemies = engine.get_enemies(owner)
                target = enemies[0] if enemies else None

            if target is not None and owner.alive:
                engine.log(f"  [也就是说…是？赢了] {owner.name} 立即追击一发【空间斩】！")
                engine.enqueue_action(
                    Action(
                        actor=owner,
                        skill=WorldCuttingSlashSkill(),
                        targets=[target],
                        consume_from_hand=False,
                        is_pursuit=True,
                        clashable=False,
                    ),
                    front=True,
                )
            return

        if not self.true_form_triggered:
            self.true_form_triggered = True
            half_hp = max(1, floor_mul(owner.max_hp, 50, 100))
            owner.hp = half_hp
            ctx.cancelled = True

            engine.log(
                f"  [★ 原身启动！ ★] {owner.name} 回复至 50% HP ({owner.hp}/{owner.max_hp})！"
                f"获得【神武解】【安慰奖】【为咒术而生的肉体】（之后开启的领域持续回合 -2）！"
            )

            self.mouth_part = SukunaBodyPart(
                part_id=f"{owner.char_id}_mouth",
                name="双嘴",
                team=owner.team,
                sukuna=owner,
            )
            self.arms_part = SukunaBodyPart(
                part_id=f"{owner.char_id}_arms",
                name="四手",
                team=owner.team,
                sukuna=owner,
            )
            engine.add_character(self.mouth_part)
            engine.add_character(self.arms_part)
            engine.log("  [为咒术而生的肉体] 场上追加可攻击对象：【双嘴】(100 HP) 与 【四手】(100 HP)！")

            target = killer if (killer and killer.alive and killer is not owner) else None
            if target is None:
                enemies = engine.get_enemies(owner)
                target = enemies[0] if enemies else None

            if target is not None:
                engine.enqueue_action(
                    Action(
                        actor=owner,
                        skill=SukunaSkill3(),
                        targets=[target],
                        consume_from_hand=False,
                        is_pursuit=True,
                        clashable=False,
                    ),
                    front=True,
                )


def create_character(team: str = "P1", **kwargs) -> Character:
    char = Character(
        char_id=f"sukuna_{team}",
        name="两面宿傩",
        team=team,
        max_hp=SUKUNA_MAX_HP,
        passives=[SukunaCorePassive()],
    )
    char.set_standard_skills(SukunaSkill1(), SukunaSkill2(), SukunaSkill3())
    return char
