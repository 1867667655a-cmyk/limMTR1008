from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from engine import (
    STATUS_CATEGORIES,
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
    floor_div,
    floor_mul,
    floor_val,
)

SUKUNA_MAX_HP = 100
MAX_EXCITEMENT = 10  # [待确认] 兴致上限，如需其他数值可直接修改此常量
AIZO_AUDIO_PATH = r"C:\Users\jntmQ\Desktop\AIZO.mp3"
AIZO_DURATION_SEC = 215.0


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


class FallingBlossomEmotionStatus(StatusEffect):
    """落花之情：在伏魔御厨子领域内流血恢复正常扣层，领域结束后消失"""

    def __init__(self, stacks: int = 1):
        super().__init__("落花之情", stacks=stacks, category="neutral")

    def on_reapply(self, engine, add_stacks: int, extra: Dict[str, Any]) -> None:
        self.stacks = max(1, self.stacks)


class EnemyBurnoutStatus(StatusEffect):
    """术式熔断（敌方）：使自身正面状态效果暂时失效，持续到下一轮开始"""

    def __init__(self, stacks: int = 1):
        super().__init__("术式熔断", stacks=stacks, category="debuff")
        self.suspended_buffs: List[Tuple[str, StatusEffect]] = []

    def on_attach(self, engine, owner: Character) -> None:
        super().on_attach(engine, owner)

        suspended_names: List[str] = []
        for st_name, st_obj in list(owner.statuses.items()):
            if st_obj is not self and st_obj.is_buff and st_obj.stacks > 0:
                st_obj.on_detach(engine)
                owner.statuses.pop(st_name, None)
                self.suspended_buffs.append((st_name, st_obj))
                suspended_names.append(f"{st_name}({st_obj.stacks})")

        if suspended_names:
            engine.log(
                f"  [术式熔断] {owner.name} 的正面状态暂时失效：{'、'.join(suspended_names)}"
            )

        self.bind(engine, EventType.STATUS_APPLY, self._on_status_apply, priority=50)
        self.bind(engine, EventType.ROUND_START, self._on_round_start, priority=-5)

    def on_reapply(self, engine, add_stacks: int, extra: Dict[str, Any]) -> None:
        self.stacks = 1

    def _on_status_apply(self, ctx: EventContext) -> None:
        if ctx.data.get("target") is not self.owner or self.owner is None:
            return
        status_name: str = ctx.data["status_name"]
        status_obj: Optional[StatusEffect] = ctx.data.get("status_obj")
        extra_cat = ctx.data.get("extra", {}).get("category")
        is_buff = (
            (status_obj.is_buff if status_obj is not None else False)
            or (STATUS_CATEGORIES.get(status_name) == "buff")
            or (extra_cat == "buff")
        )
        if is_buff:
            ctx.cancelled = True
            ctx.engine.log(
                f"  [术式熔断] {self.owner.name} 处于术式熔断，正面状态【{status_name}】暂时失效！"
            )

    def _on_round_start(self, ctx: EventContext) -> None:
        owner = self.owner
        engine = ctx.engine
        if not owner:
            return
        restored = list(self.suspended_buffs)
        self.suspended_buffs.clear()
        engine.remove_status(owner, self.name)

        if owner.alive:
            for st_name, st_obj in restored:
                existing = owner.statuses.get(st_name)
                if existing is not None:
                    existing.on_reapply(engine, st_obj.stacks, {})
                else:
                    owner.statuses[st_name] = st_obj
                    st_obj.on_attach(engine, owner)
            if restored:
                names = "、".join(f"{n}({s.stacks})" for n, s in restored)
                engine.log(f"  [术式恢复] {owner.name} 的术式熔断结束，恢复正面状态：{names}")
            else:
                engine.log(f"  [术式恢复] {owner.name} 的术式熔断结束！")


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
    """2技能：8+4（最后1枚正面重投，每次正面+1易损，连续重投3次停止并引爆流血）"""

    def __init__(self):
        super().__init__(
            skill_id="sukuna_s2",
            name="捌·连斩",
            tier=2,
            base_power=8,
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
    """3技能：9+3+3+3（命中使敌方3回合受到流血施加×2；敌方流血>=10时转化为「灶」）"""

    def __init__(self):
        super().__init__(
            skill_id="sukuna_s3",
            name="捌·深斩",
            tier=3,
            base_power=9,
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
    """「灶」：15+5+5（流血×1.5并转化为烧伤；领域内威力+场上流血÷2且改为群攻）"""

    def __init__(self):
        super().__init__(
            skill_id="sukuna_fuga",
            name="「灶」",
            tier="EX",
            base_power=15,
            coins=[Coin(5), Coin(5)],
            tags={"sukuna_triggered"},
        )

    def _log_open_once(self, engine, actor: Character, action: Action) -> None:
        if not action.extra.get("fuga_opened"):
            action.extra["fuga_opened"] = True
            engine.log(f"  [{actor.name}] 「开」")

    def on_unopposed_start(self, engine, action: Action) -> None:
        self._log_open_once(engine, action.actor, action)

    def on_clash_start(self, engine, self_action: Action, opp_action: Action) -> None:
        self._log_open_once(engine, self_action.actor, self_action)

    def on_aoe_start(self, engine, action: Action) -> None:
        self._log_open_once(engine, action.actor, action)

    def on_unbreakable_attack_start(self, engine, action: Action) -> None:
        self._log_open_once(engine, action.actor, action)

    def on_before_roll(self, engine, actor: Character, roll_state: Dict[str, Any]) -> None:
        self._log_open_once(engine, actor, roll_state["action"])
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
    """空间斩：10+50（无视减伤、护盾、锁血，命中伤害×2）"""

    def __init__(self, chant: bool = True):
        super().__init__(
            skill_id="sukuna_wcs",
            name="空间斩",
            tier="EX",
            base_power=10,
            coins=[Coin(50)],
            clashable=False,
            tags={"sukuna_triggered", "world_cutting_slash"},
        )
        self.chant: bool = chant

    def _log_chant_once(self, engine, actor: Character, action: Action) -> None:
        if self.chant and not action.extra.get("wcs_chanted"):
            action.extra["wcs_chanted"] = True
            engine.log(f"  [{actor.name}] 「龙鳞」 「反发」 「双子流星」")

    def on_unopposed_start(self, engine, action: Action) -> None:
        self._log_chant_once(engine, action.actor, action)

    def on_clash_start(self, engine, self_action: Action, opp_action: Action) -> None:
        self._log_chant_once(engine, self_action.actor, self_action)

    def on_before_roll(self, engine, actor: Character, roll_state: Dict[str, Any]) -> None:
        self._log_chant_once(engine, actor, roll_state["action"])

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
            target_passive = _get_sukuna_passive(target)
            if target_passive:
                if target_passive.domain_turns_left > 0:
                    target_passive.domain_turns_left = 0
                    target_passive._on_domain_ended(engine)
                target_passive._kill_body_parts(engine)
            if (
                target.team != action.actor.team
                and not dmg_ctx.extra.get("wcs_kill_spoken")
            ):
                dmg_ctx.extra["wcs_kill_spoken"] = True
                engine.log(
                    f"  [{action.actor.name}] 「真是尽兴啊{target.name}，我恐怕这辈子都不会忘记你了吧」"
                )


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

    def can_use(self, engine, actor: Character) -> bool:
        p = _get_sukuna_passive(actor)
        if not p or p.rct_count <= 0:
            return False
        has_damaged_part = p.true_form_triggered and any(
            part is not None and (not part.alive or part.hp < part.max_hp)
            for part in (p.mouth_part, p.arms_part)
        )
        return p.burnout_active or actor.hp < actor.max_hp or has_damaged_part

    def on_select(self, engine, action: Action) -> None:
        actor = action.actor
        passive = _get_sukuna_passive(actor)
        if passive:
            passive.execute_rct(engine, actor)

        if not actor.alive:
            action.skill = None
            action.targets = []
            return

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
# 「双嘴」与「四手」实体部位（附着于宿傩身上的部位）
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
            "包含兴致、咒力、黑闪、伏魔御厨子、领域对拼、术式熔断、十种影法术、抱歉切成三段、好不容易来了点兴致就和你玩玩吧与原身启动全机制",
        )
        # 兴致相关（整场首次拼败/打平+2，之后+1）
        self.excitement: int = 0
        self.first_tie_or_lose_done: bool = False

        # 好不容易来了点兴致就和你玩玩吧（敌方连续拼输10次 -> 最大点数技能 vs 灶）
        self.enemy_consecutive_losses: Dict[int, int] = {}
        self._pending_fuga_clashes: List[Character] = []

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
        self._domain_burnout_targets: List[Character] = []

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

    def domain_active(self) -> bool:
        return bool(
            self.owner
            and self.owner.alive
            and self.domain_turns_left > 0
            and not self.burnout_active
        )

    def technique_active(self) -> bool:
        return not self.burnout_active

    def _is_body_part(self, char: Optional[Character]) -> bool:
        return char is not None and (char is self.mouth_part or char is self.arms_part)

    def _get_living_parts(self) -> List[SukunaBodyPart]:
        parts: List[SukunaBodyPart] = []
        if self.mouth_part and self.mouth_part.alive:
            parts.append(self.mouth_part)
        if self.arms_part and self.arms_part.alive:
            parts.append(self.arms_part)
        return parts

    def _is_manual_actor(self, engine, actor: Character) -> bool:
        if actor.ai_selector is not None:
            return False
        sel = engine.manual_selector
        if sel is None:
            return False
        if getattr(sel, "__name__", "") == "p1_manual_p2_ai" and actor.team != "P1":
            return False
        return True

    def _kill_body_parts(self, engine) -> None:
        for part in (self.mouth_part, self.arms_part):
            if part is not None and part.alive:
                part.hp = 0
                part.alive = False
                engine.log(f"  [躯体崩解] {self.owner.name} 阵亡，【{part.name}】随之消散！")

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

    def _pick_enemy_max_power_skill(self, enemy: Character) -> Optional[Skill]:
        candidates: List[Skill] = []
        for proto, _ in enemy.skill_template:
            candidates.append(proto)
        for sk in enemy.available_skills:
            candidates.append(sk)

        if not candidates:
            return None

        offensive_clashable = [
            sk for sk in candidates if sk.clashable and sk.target_type != "self"
        ]
        pool = (
            offensive_clashable
            or [sk for sk in candidates if sk.target_type != "self"]
            or candidates
        )

        def _max_roll_key(sk: Skill) -> Tuple[int, int, int]:
            max_roll = floor_val(sk.base_power) + sum(
                max(0, floor_val(c.value)) for c in sk.coins
            )
            return (max_roll, floor_val(sk.base_power), len(sk.coins))

        best = max(pool, key=_max_roll_key)
        return best.clone()

    def _activate_aizo_bgm(
        self, engine, reason: str = "好不容易来了点兴致就和你玩玩吧"
    ) -> None:
        owner = self.owner
        if not owner or not owner.alive:
            return
        owner.custom_state["aizo_active"] = True

        track = None
        for tr in getattr(owner, "bgm_tracks", []):
            if tr.extra_data.get("aizo_theme") or "aizo" in tr.track_id.lower():
                track = tr
                break
        if track is None:
            track = BGMTrack(
                track_id=f"{owner.char_id}_aizo",
                title="AIZO",
                file_path=AIZO_AUDIO_PATH,
                duration_sec=AIZO_DURATION_SEC,
                condition=lambda eng, tr: bool(tr.owner and tr.owner.custom_state.get("aizo_active")),
                stop_on_owner_death=True,
                extra_data={"aizo_theme": True},
            )
            owner.bgm_tracks.append(track)
            engine.bgm.register_track(track, owner=owner)

        track.debut_turn = max(1, engine.global_turn)
        engine.bgm.evaluate(reason=reason)

    def _flush_pending_fuga_clashes(self, engine) -> None:
        owner = self.owner
        if not owner or not owner.alive or not self._pending_fuga_clashes:
            self._pending_fuga_clashes.clear()
            return

        pending = list(self._pending_fuga_clashes)
        self._pending_fuga_clashes.clear()

        for opp in pending:
            if not owner.alive or not opp.alive or engine.check_victory():
                break

            enemy_skill = self._pick_enemy_max_power_skill(opp)
            if enemy_skill is None:
                continue

            fuga_skill = FugaSkill()
            if self.domain_active():
                fuga_skill.is_aoe = True
                fuga_skill.target_type = "all_enemies"
                fuga_skill.unbreakable = True

            engine.log(
                f"  [★ 好不容易来了点兴致就和你玩玩吧 ★] {opp.name} 已连续拼输 10 次！"
            )
            self._activate_aizo_bgm(engine, reason="好不容易来了点兴致就和你玩玩吧")
            engine.log(
                f"  [{owner.name}] 「不过我难得来了点兴致，那就用你最擅长的招式和你打」"
            )
            engine.log(
                f"  [{opp.name}] 「这是…火？」"
            )
            engine.log(
                f"  [{owner.name}] 「摆好架势，来拼火力吧」"
            )
            engine.apply_status(owner, "易损", 10, source=None)
            engine.apply_status(opp, "易损", 10, source=None)
            engine.log(
                f"  [好不容易来了点兴致就和你玩玩吧] 双方获得 10 层易损！"
                f"{owner.name} 以【{fuga_skill.name}({fuga_skill.format_formula()})】"
                f"与 {opp.name} 最大点数技能【{enemy_skill.name}({enemy_skill.format_formula()})】展开拼点！"
            )

            fuga_act = Action(
                actor=owner,
                skill=fuga_skill,
                targets=[opp],
                consume_from_hand=False,
                clashable=True,
            )
            enemy_act = Action(
                actor=opp,
                skill=enemy_skill,
                targets=[owner],
                consume_from_hand=False,
                clashable=True,
            )
            engine._resolve_clash(fuga_act, enemy_act)

    def _on_domain_ended(self, engine) -> None:
        """领域结束或破碎时：恢复手牌红币状态，清除场上落花之情，并使曾领域对拼落败的敌方进入术式熔断"""
        if self.owner:
            for sk in self.owner.available_skills:
                if not isinstance(sk, ReverseCursedTechniqueSkill):
                    sk.unbreakable = False

        for c in list(engine.characters):
            if c.get_status("落花之情") is not None:
                engine.remove_status(c, "落花之情")
                engine.log(f"  [落花之情] 领域已结束，{c.name} 身上的【落花之情】消散！")

        if self._domain_burnout_targets:
            targets = list(self._domain_burnout_targets)
            self._domain_burnout_targets.clear()
            for tgt in targets:
                if tgt.alive:
                    engine.log(
                        f"  [术式熔断] 领域结束，{tgt.name} 受到【术式熔断】影响，自身正面状态暂时失效直至下一轮开始！"
                    )
                    engine.apply_status(
                        tgt,
                        EnemyBurnoutStatus(stacks=1),
                        stacks=1,
                        source=None,
                    )

    def _shatter_domain(self, engine) -> None:
        """领域对拼失败：领域破碎并提前取消"""
        owner = self.owner
        if not owner:
            return
        self.domain_turns_left = 0
        self.burnout_active = True
        engine.log(
            f"  [领域破碎] {owner.name} 在领域对拼中落败，【伏魔御厨子】破碎并提前取消，{owner.name} 进入术式熔断！"
        )
        self._on_domain_ended(engine)

    def _run_domain_clash(self, engine, enemy_aoe_act: Action) -> None:
        owner = self.owner
        if not owner or not owner.alive or not self.domain_active():
            return
        opp = enemy_aoe_act.actor
        if not opp.alive or enemy_aoe_act.skill is None:
            return

        orig_skill_name = enemy_aoe_act.skill.name
        is_red_aoe = enemy_aoe_act.skill.has_unbreakable() or bool(
            enemy_aoe_act.extra.get("force_unbreakable")
        )

        fuga_skill = FugaSkill()
        fuga_skill.is_aoe = True
        fuga_skill.target_type = "all_enemies"
        fuga_skill.unbreakable = True

        fuga_act = Action(
            actor=owner,
            skill=fuga_skill,
            targets=[opp],
            consume_from_hand=False,
            clashable=True,
            extra={
                "is_domain_clash": True,
                "domain_clash_is_red": is_red_aoe,
                "domain_clash_opp_skill_name": orig_skill_name,
            },
        )
        enemy_aoe_act.extra["is_domain_clash_enemy"] = True

        self._activate_aizo_bgm(engine, reason="领域对拼")

        engine.log(
            f"  [★ 领域对拼 ★] {opp.name} 在【伏魔御厨子】内展开群攻【{orig_skill_name}】，"
            f"{owner.name} 使用【「灶」】展开领域对拼！"
        )
        engine._resolve_clash(fuga_act, enemy_aoe_act)

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

    def _heal_lowest_body_part(self, engine, heal_amount: int) -> None:
        if not self.true_form_triggered or heal_amount <= 0:
            return
        parts = [p for p in (self.mouth_part, self.arms_part) if p is not None]
        if not parts:
            return

        target_parts = [p for p in parts if not p.alive or p.hp < p.max_hp]
        if not target_parts:
            return

        lowest = min(target_parts, key=lambda p: (p.hp, 0 if p is self.mouth_part else 1))
        was_dead = not lowest.alive or lowest.hp <= 0
        before_hp = max(0, lowest.hp)
        lowest.hp = min(lowest.max_hp, before_hp + heal_amount)
        gained = lowest.hp - before_hp

        if was_dead and lowest.hp > 0:
            lowest.alive = True
            engine.log(
                f"  [反转术式] 【{lowest.name}】复活并回复 {gained} 点生命值 "
                f"(HP: {lowest.hp}/{lowest.max_hp})"
            )
        elif gained > 0:
            engine.log(
                f"  [反转术式] 【{lowest.name}】回复 {gained} 点生命值 "
                f"(HP: {lowest.hp}/{lowest.max_hp})"
            )

    def execute_rct(self, engine, owner: Character) -> None:
        if not owner.alive:
            return
        if self.rct_count > 0:
            self.rct_count -= 1
        five_pct = max(1, floor_mul(owner.max_hp, 5, 100))

        if self.burnout_active:
            if (owner.hp - five_pct) > 0:
                self.burnout_active = False
                owner.hp = owner.hp - five_pct
                engine.log(
                    f"  [反转术式] {owner.name} 处于术式熔断，抵消术式熔断但失去 {five_pct} HP (HP: {owner.hp}/{owner.max_hp})"
                )
                self._try_activate_domain(engine)
                return

            engine.log(
                f"  [反转术式] {owner.name} 当前生命（{owner.hp}）若扣除 {five_pct} HP 将阵亡或进入锁血，切换至正常回血！"
            )

        self._apply_healing(engine, five_pct, reason="反转术式")
        final_part_heal = floor_mul(five_pct, self.black_flash_mult_pct, 100)
        self._heal_lowest_body_part(engine, final_part_heal)

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
        self.bind(engine, EventType.AOE_START, self._on_aoe_start, priority=-10)
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
            engine.log(f"  [{owner.name}] 领域展开  「伏魔御厨子」")
            engine.log(
                f"  [领域展开] {owner.name} 展开【伏魔御厨子】（持续 {duration} 回合）！"
            )
            self._maintain_domain_bleed(engine)

    def _maintain_domain_bleed(self, engine) -> None:
        """领域开启及维持期间：除宿傩自身、双嘴四手与落花之情持有者外，场上所有人流血不低于 8"""
        if not self.domain_active() or self._maintaining_bleed:
            return
        self._maintaining_bleed = True
        try:
            for c in engine.get_living_characters():
                if c is self.owner or self._is_body_part(c):
                    continue
                if c.get_status("落花之情") is not None:
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
            engine.log(f"  [{owner.name}] 「别会错意了，这下是三打一」")
            engine.log(f"  [十种影法术·魔虚罗] {owner.name} 生命低于90%，法阵转动，开启魔虚罗适应！")

    def _on_turn_start(self, ctx: EventContext) -> None:
        engine = ctx.engine
        owner = self.owner
        if not owner or not owner.alive:
            return

        self._check_mahoraga_unlock(engine)

        living_parts = self._get_living_parts()
        if self.true_form_triggered and living_parts:
            p_str = " | ".join(f"{p.name}: {p.hp}/{p.max_hp} HP" for p in living_parts)
            engine.log(f"  [为咒术而生的肉体] 当前部位：{p_str}")

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
        self._flush_pending_fuga_clashes(ctx.engine)
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
                    if c.get_status("落花之情") is not None:
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

        if self._temp_rct_skill and self._temp_rct_skill in owner.available_skills:
            owner.available_skills.remove(self._temp_rct_skill)
        self._temp_rct_skill = None

        if not owner.alive:
            if self.domain_turns_left > 0:
                self.domain_turns_left = 0
                self._on_domain_ended(engine)
            self._kill_body_parts(engine)
            return

        if self.domain_turns_left > 0 and not self.burnout_active:
            self.domain_turns_left -= 1
            if self.domain_turns_left == 0:
                self.burnout_active = True
                engine.log(
                    f"  [术式熔断] {owner.name} 的【伏魔御厨子】结束，进入术式熔断状态直至下一轮开始！"
                )
                self._on_domain_ended(engine)

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
    # 选技前/后与领域对拼拦截
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

            if not isinstance(sk, ReverseCursedTechniqueSkill):
                sk.unbreakable = self.domain_active()

        if self.rct_count > 0 and not any(
            isinstance(s, ReverseCursedTechniqueSkill) for s in owner.available_skills
        ):
            rct_sk = ReverseCursedTechniqueSkill()
            if rct_sk.can_use(engine, owner):
                self._temp_rct_skill = rct_sk
                owner.available_skills.insert(0, rct_sk)

    def _on_after_select(self, ctx: EventContext) -> None:
        action: Action = ctx.data["action"]
        owner = self.owner
        engine = ctx.engine
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
            engine.log(f"  [伏魔御厨子] {tgt.name} 在领域内行动，受到 {loss} 点切割伤害！")
            engine.apply_damage(
                attacker=owner,
                target=tgt,
                raw_damage=loss,
                source_type="domain_dot",
                trigger_on_hit_statuses=False,
                allow_breath=False,
            )

        targets_sukuna = (
            owner in action.targets
            or (
                action.skill is not None
                and action.skill.is_aoe
                and not action.targets
                and not action.skill.clash_aoe
                and (
                    action.skill.indiscriminate
                    or action.skill.target_type in ("all_enemies", "all_others", "aoe")
                )
            )
        )
        living_parts = self._get_living_parts()
        if (
            self.true_form_triggered
            and living_parts
            and action.actor.alive
            and action.actor.team != owner.team
            and action.skill is not None
            and not action.cancelled
            and targets_sukuna
        ):
            if len(living_parts) == 1:
                chosen_part = living_parts[0]
                engine.log(
                    f"  [为咒术而生的肉体] 仅剩【{chosen_part.name}】({chosen_part.hp}/{chosen_part.max_hp} HP)，"
                    f"本次攻击同步至【{chosen_part.name}】"
                )
            elif self._is_manual_actor(engine, action.actor):
                print(
                    f"  [为咒术而生的肉体] 已选定攻击 {owner.name}，请选择同步伤害的部位："
                )
                for idx, part in enumerate(living_parts):
                    print(f"    {idx}: {part.name} (HP:{part.hp}/{part.max_hp})")
                while True:
                    raw = input("  请输入部位编号: ").strip()
                    if raw.isdigit() and 0 <= int(raw) < len(living_parts):
                        chosen_part = living_parts[int(raw)]
                        break
                    print("  输入无效，请重新输入。")
            else:
                chosen_part = engine.rng.choice(living_parts)
                engine.log(
                    f"  [为咒术而生的肉体] {action.actor.name} 选择攻击部位：【{chosen_part.name}】"
                    f"(HP:{chosen_part.hp}/{chosen_part.max_hp})"
                )
            action.extra["chosen_part"] = chosen_part

    def _on_aoe_start(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive or not self.domain_active():
            return

        aoe_act: Action = ctx.data["action"]
        if (
            aoe_act.actor.team == owner.team
            or not aoe_act.actor.alive
            or aoe_act.skill is None
            or not aoe_act.skill.is_aoe
            or aoe_act.is_unbreakable_attack
            or aoe_act.extra.get("domain_clash_handled")
        ):
            return

        aoe_act.extra["domain_clash_handled"] = True
        ctx.cancelled = True

        clash_enemy_act = Action(
            actor=aoe_act.actor,
            skill=aoe_act.skill.clone(),
            targets=[owner],
            consume_from_hand=False,
            clashable=True,
            extra=dict(aoe_act.extra),
        )
        self._run_domain_clash(ctx.engine, clash_enemy_act)

    def _on_resolve_matchups(self, ctx: EventContext) -> None:
        owner = self.owner
        if not owner or not owner.alive:
            return

        actions: List[Action] = ctx.data["actions"]

        if self.domain_active():
            for e_act in list(actions):
                if (
                    e_act.cancelled
                    or e_act.actor.team == owner.team
                    or not e_act.actor.alive
                    or e_act.skill is None
                    or not e_act.skill.is_aoe
                    or e_act.is_unbreakable_attack
                    or e_act.extra.get("domain_clash_handled")
                ):
                    continue
                if not self.domain_active() or not owner.alive:
                    break

                e_act.cancelled = True
                e_act.extra["domain_clash_handled"] = True
                clash_enemy_act = Action(
                    actor=e_act.actor,
                    skill=e_act.skill.clone(),
                    targets=[owner],
                    consume_from_hand=False,
                    clashable=True,
                    extra=dict(e_act.extra),
                )
                self._run_domain_clash(ctx.engine, clash_enemy_act)

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
                or (
                    action.skill is not None
                    and action.skill.is_aoe
                    and not action.targets
                    and not action.skill.clash_aoe
                    and (
                        action.skill.indiscriminate
                        or action.skill.target_type in ("all_enemies", "all_others", "aoe")
                    )
                )
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
                or (
                    action.skill is not None
                    and action.skill.is_aoe
                    and not action.targets
                    and not action.skill.clash_aoe
                    and (
                        action.skill.indiscriminate
                        or action.skill.target_type in ("all_enemies", "all_others", "aoe")
                    )
                )
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

        if self.technique_active() and self.mahoraga_enabled and opp_act.skill:
            skey = opp_act.skill.skill_id or opp_act.skill.name
            self.adapt1_skills[skey] = self.adapt1_skills.get(skey, 0) + 1
            ctx.engine.log(
                f"  [适应一] {opp_act.skill.name}（适应1：{self.adapt1_skills[skey]}）"
            )

    def _on_clash_start_intercept(self, ctx: EventContext) -> None:
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

        if loser_act.extra.get("is_domain_clash"):
            loser_act.extra["destroy_unbreakable"] = True
            self._shatter_domain(engine)

        loser_act.skill.on_clash_lose(engine, loser_act, winner_act, lose_roll)
        winner_act.skill.on_clash_win(engine, winner_act, loser_act, win_roll)

        unbreakable_act = engine._build_unbreakable_action(winner_act, loser_act, from_aoe=False)
        if unbreakable_act is not None and "chosen_part" in loser_act.extra:
            unbreakable_act.extra["chosen_part"] = loser_act.extra["chosen_part"]
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

        if winner_act.extra.get("is_domain_clash"):
            opp = loser_act.actor
            if opp not in self._domain_burnout_targets:
                self._domain_burnout_targets.append(opp)
            if opp.alive and winner_act.extra.get("domain_clash_is_red"):
                orig_name = winner_act.extra.get(
                    "domain_clash_opp_skill_name", loser_act.skill.name
                )
                engine.log(
                    f"  [{opp.name}] 「虽然在学会{orig_name}之后就没用过了，但效果还不错吧？」"
                )
                engine.apply_status(
                    opp,
                    FallingBlossomEmotionStatus(stacks=1),
                    stacks=1,
                    source=None,
                )

        if unbreakable_act is not None and unbreakable_act.actor.alive and not engine.check_victory():
            engine._resolve_unbreakable_attack(unbreakable_act)

        self._flush_pending_fuga_clashes(engine)

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

        if s_roll < o_roll and self.cursed_energy >= 7:
            self.cursed_energy -= 7
            self.consumed_cursed_energy += 7
            s_roll = o_roll
            ctx.data["roll1" if is_p1 else "roll2"] = s_roll
            ctx.engine.log(
                f"  [咒术师] {owner.name} 点数不敌，消耗 7 层咒力将点数提升至 {s_roll} 与 {opp_act.actor.name} 打平！"
                f"（剩余咒力: {self.cursed_energy}）"
            )
            self._try_activate_domain(ctx.engine)

        sukuna_act.extra["last_clash_diff"] = s_roll - o_roll

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

        if win_act.actor.team != owner.team:
            opp_id = id(win_act.actor)
            if self.enemy_consecutive_losses.get(opp_id, 0) > 0:
                self.enemy_consecutive_losses[opp_id] = 0
                ctx.engine.log(
                    f"  [好不容易来了点兴致就和你玩玩吧] {win_act.actor.name} 拼点获胜，连续拼输计数清零！"
                )
            return

        if win_act.actor is not owner:
            return

        engine = ctx.engine
        opp = lose_act.actor

        self.cursed_energy += 2
        engine.log(
            f"  [咒术师] {owner.name} 拼点胜利，获得 2 层咒力（当前: {self.cursed_energy}）"
        )

        has_power_buff = bool(lose_act.extra.get("had_power_increase"))
        diff = win_act.extra.get("last_clash_diff", 0)
        if has_power_buff and diff >= 3 and self.excitement > 0:
            self.excitement -= 1
            engine.log(
                f"  [兴致下降] 以 {diff} 点优势压制持有威力增益的 {opp.name}，"
                f"{owner.name} 兴致 -1（当前: {self.excitement}/{MAX_EXCITEMENT}）"
            )

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
        win_act: Action = ctx.data["winner_action"]

        if lose_act.actor.team != owner.team and win_act.actor.team == owner.team:
            opp = lose_act.actor
            opp_id = id(opp)
            losses = self.enemy_consecutive_losses.get(opp_id, 0) + 1
            self.enemy_consecutive_losses[opp_id] = losses
            ctx.engine.log(
                f"  [好不容易来了点兴致就和你玩玩吧] {opp.name} 连续拼输：{losses}/10"
            )
            if losses >= 10:
                self.enemy_consecutive_losses[opp_id] = 0
                if opp not in self._pending_fuga_clashes:
                    self._pending_fuga_clashes.append(opp)
            return

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
    # 伤害计算 / 减免 / 抱歉切成三段 / 受击同步 / 适应一二三 / 安慰奖
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
            blocked = min(100, dmg_ctx.damage)
            dmg_ctx.damage = max(0, dmg_ctx.damage - 100)
            ctx.engine.log(
                f"  [就当给你一个安慰奖吧] 抵挡 {blocked} 点技能伤害"
                f"（剩余 {dmg_ctx.damage}），"
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
            dmg_ctx.attacker is owner
            and dmg_ctx.skill is not None
            and "world_cutting_slash" in dmg_ctx.skill.tags
            and dmg_ctx.target.team != owner.team
            and not dmg_ctx.target.alive
            and not dmg_ctx.extra.get("wcs_kill_spoken")
        ):
            dmg_ctx.extra["wcs_kill_spoken"] = True
            engine.log(
                f"  [{owner.name}] 「真是尽兴啊{dmg_ctx.target.name}，我恐怕这辈子都不会忘记你了吧」"
            )

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

            if (
                self.true_form_triggered
                and dmg_ctx.skill is not None
                and dmg_ctx.attacker is not None
                and dmg_ctx.attacker.team != owner.team
            ):
                living_parts = self._get_living_parts()
                if living_parts:
                    action: Optional[Action] = ctx.data.get("action")
                    part = action.extra.get("chosen_part") if action else None
                    if part is None or not part.alive:
                        part = engine.rng.choice(living_parts)

                    part.hp = max(0, part.hp - actual_loss)
                    engine.log(
                        f"  [肉体同步] {owner.name} 受创，同步 {actual_loss} 点伤害至【{part.name}】 "
                        f"(HP: {part.hp}/{part.max_hp})"
                    )
                    if part.hp <= 0 and part.alive:
                        part.alive = False
                        engine.log(f"  [为咒术而生的肉体] 【{part.name}】已被破坏！")
                        if not self._get_living_parts():
                            engine.log(
                                f"  [为咒术而生的肉体] 「双嘴」与「四手」已全部破坏！"
                            )
                            if self.wcs_challenge_active:
                                self._reset_wcs_challenge()

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
        extra_cat = ctx.data.get("extra", {}).get("category")

        is_debuff = (
            (status_obj.is_debuff if status_obj is not None else False)
            or (STATUS_CATEGORIES.get(status_name) == "debuff")
            or (extra_cat == "debuff")
        )

        if target is owner and is_debuff and source is not None:
            if self.technique_active() and self.mahoraga_enabled:
                skey = self._enemy_active_skill_vs_sukuna.get(id(source))
                if skey and self.adapt1_skills.get(skey, 0) >= 15:
                    ctx.cancelled = True
                    ctx.engine.log(
                        f"  [适应一·15层] {source.name} 的技能所有数值已归零，无法施加 {status_name}！"
                    )
                    return

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
    # 锁血一阶段（也就是说…是？赢了）与二阶段（原身启动！）
    # ------------------------------------------------------------------
    def _on_character_death(self, ctx: EventContext) -> None:
        owner = self.owner
        char: Character = ctx.data["character"]
        killer: Optional[Character] = ctx.data.get("killer")
        dmg_ctx: Optional[DamageContext] = ctx.data.get("damage_context")
        if char is not owner or owner is None or owner.hp > 0:
            return

        if dmg_ctx and dmg_ctx.extra.get("ignore_death_immunity"):
            if self.domain_turns_left > 0:
                self.domain_turns_left = 0
                self._on_domain_ended(ctx.engine)
            self._kill_body_parts(ctx.engine)
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
                        skill=WorldCuttingSlashSkill(chant=False),
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
            engine.log("  [为咒术而生的肉体] 场上追加可攻击部位：【双嘴】(100 HP) 与 【四手】(100 HP)！")

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
            return

        if self.domain_turns_left > 0:
            self.domain_turns_left = 0
            self._on_domain_ended(engine)
        self._kill_body_parts(engine)


def create_character(team: str = "P1", **kwargs: Any) -> Character:
    bgm = BGMTrack(
        track_id=f"sukuna_{team}_aizo",
        title="AIZO",
        file_path=AIZO_AUDIO_PATH,
        duration_sec=AIZO_DURATION_SEC,
        condition=lambda eng, tr: bool(tr.owner and tr.owner.custom_state.get("aizo_active")),
        stop_on_owner_death=True,
        extra_data={"aizo_theme": True},
    )
    char = Character(
        char_id=f"sukuna_{team}",
        name="两面宿傩",
        team=team,
        max_hp=SUKUNA_MAX_HP,
        passives=[SukunaCorePassive()],
        bgm_tracks=[bgm],
        custom_state={
            "aizo_active": False,
        },
    )
    char.set_standard_skills(SukunaSkill1(), SukunaSkill2(), SukunaSkill3())
    return char
