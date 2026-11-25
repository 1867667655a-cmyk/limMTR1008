from __future__ import annotations

import random
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, Union

from .bgm import AudioBackend, BGMManager
from .events import EventBus, EventType
from .math_utils import floor_val
from .models import Action, Character, Coin, DamageContext, Skill, StatusEffect
from .statuses import STATUS_REGISTRY, register_default_statuses


class CombatEngine:
    def __init__(
        self,
        characters: Optional[List[Character]] = None,
        turns_per_round: int = 6,
        max_clash_ties: int = 99,
        max_rounds: int = 50,
        seed: Optional[int] = None,
        logger: Optional[Callable[[str], None]] = None,
        auto_roll: bool = True,
        roll_handler: Optional[Any] = None,
        show_turn_board: bool = True,
        audio_backend: Optional[AudioBackend] = None,
        allow_ally_target: bool = True,
        allow_ally_clash: bool = True,
    ):
        register_default_statuses()
        self.events = EventBus()
        self.characters: List[Character] = []
        self.turns_per_round = turns_per_round
        self.max_clash_ties = max_clash_ties
        self.max_rounds = max_rounds
        self.rng = random.Random(seed)
        self.logger = logger or print

        self.auto_roll: bool = auto_roll
        self.roll_handler: Optional[Any] = roll_handler
        self.show_turn_board: bool = show_turn_board
        self.allow_ally_target: bool = allow_ally_target
        self.allow_ally_clash: bool = allow_ally_clash
        self.bgm = BGMManager(self, audio_backend=audio_backend)

        self.round_num: int = 0
        self.turn_in_round: int = 0
        self.global_turn: int = 0
        self._tie_turn_starter: str = "P1"

        self.action_queue: Deque[Action] = deque()
        self.current_turn_selections: List[Action] = []
        self.game_over: bool = False
        self.winner_team: Optional[str] = None

        self.manual_selector: Optional[
            Callable[["CombatEngine", Character, int, List[Action]], Action]
        ] = None

        if characters:
            for c in characters:
                self.add_character(c)

    def log(self, msg: str) -> None:
        if self.logger:
            self.logger(msg)

    def toggle_auto_roll(self, enabled: Optional[bool] = None) -> bool:
        self.auto_roll = (not self.auto_roll) if enabled is None else bool(enabled)
        self.events.emit(EventType.AUTO_ROLL_TOGGLED, self, auto_roll=self.auto_roll)
        return self.auto_roll

    def add_character(self, char: Character) -> None:
        self.characters.append(char)
        for p in char.passives:
            p.on_register(self, char)
        for tr in getattr(char, "bgm_tracks", []):
            self.bgm.register_track(tr, owner=char)

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    def get_living_characters(self, team: Optional[str] = None) -> List[Character]:
        return [
            c
            for c in self.characters
            if c.alive and (team is None or c.team == team)
        ]

    def get_enemies(self, char: Character) -> List[Character]:
        return [c for c in self.characters if c.alive and c.team != char.team]

    def get_allies(self, char: Character, include_self: bool = True) -> List[Character]:
        return [
            c
            for c in self.characters
            if c.alive and c.team == char.team and (include_self or c is not char)
        ]

    def get_teams_alive(self) -> List[str]:
        seen: List[str] = []
        for c in self.characters:
            if c.alive and c.team not in seen:
                seen.append(c.team)
        return seen

    def check_victory(self) -> Optional[str]:
        teams = self.get_teams_alive()
        if len(teams) == 1:
            self.game_over = True
            self.winner_team = teams[0]
            return self.winner_team
        if len(teams) == 0:
            self.game_over = True
            self.winner_team = "DRAW"
            return "DRAW"
        return None

    def can_target_ally(self, char: Character, skill: Skill) -> bool:
        if skill.target_type in (
            "single_ally",
            "other_ally",
            "single_any",
            "all_allies",
            "all_other_allies",
            "all_others",
        ) or skill.indiscriminate:
            return True
        if skill.target_type == "self":
            return False
        if skill.allow_ally_target is not None:
            return bool(skill.allow_ally_target)
        return self.allow_ally_target

    def get_selectable_targets(
        self, char: Character, skill: Skill, for_ai: bool = False
    ) -> List[Character]:
        enemies = self.get_enemies(char)
        other_allies = self.get_allies(char, include_self=False)
        allies = self.get_allies(char, include_self=True)

        if skill.target_type == "self":
            return [char] if char.alive else []
        if skill.target_type == "single_ally":
            return allies
        if skill.target_type == "other_ally":
            return other_allies
        if skill.target_type == "single_any":
            return enemies + other_allies
        if skill.target_type == "all_allies":
            return allies
        if skill.target_type == "all_other_allies":
            return other_allies
        if skill.is_aoe and not skill.clash_aoe:
            if skill.indiscriminate or skill.target_type == "all_others":
                return [c for c in self.get_living_characters() if c is not char]
            return enemies

        # 单体攻击与拼点群攻的拼点目标：AI 默认不主动攻击队友，玩家手动时可选择攻击队友
        if not for_ai and self.can_target_ally(char, skill):
            return enemies + other_allies
        return enemies

    def _get_aoe_targets(self, act: Action) -> List[Character]:
        assert act.skill is not None
        if act.skill.indiscriminate or act.skill.target_type == "all_others":
            return [c for c in self.get_living_characters() if c is not act.actor]
        if act.skill.target_type == "all_allies":
            return self.get_allies(act.actor, include_self=True)
        if act.skill.target_type == "all_other_allies":
            return self.get_allies(act.actor, include_self=False)
        return self.get_enemies(act.actor)

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------
    def apply_status(
        self,
        target: Character,
        status: Union[str, StatusEffect],
        stacks: int = 1,
        source: Optional[Character] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[StatusEffect]:
        if not target.alive:
            return None
        stacks = floor_val(stacks)
        if stacks <= 0 and isinstance(status, str):
            return None

        extra = extra or {}
        if isinstance(status, str):
            name = status
            new_obj: Optional[StatusEffect] = None
        else:
            name = status.name
            new_obj = status
            stacks = status.stacks

        ctx = self.events.emit(
            EventType.STATUS_APPLY,
            self,
            target=target,
            source=source,
            status_name=name,
            status_obj=new_obj,
            stacks=stacks,
            extra=extra,
        )
        if ctx.cancelled:
            return None

        stacks = floor_val(ctx.data["stacks"])
        name = ctx.data["status_name"]
        new_obj = ctx.data["status_obj"]

        existing = target.statuses.get(name)
        if existing:
            existing.on_reapply(self, stacks, extra)
            if existing.stacks <= 0:
                self.remove_status(target, name)
                return None
            self.bgm.evaluate(reason=f"状态变化:{name}")
            return existing

        if stacks <= 0:
            return None

        if new_obj is None:
            cls = STATUS_REGISTRY.get(name)
            if cls is None:
                category = extra.get("category", "neutral")
                new_obj = StatusEffect(name=name, stacks=stacks, category=category)
            else:
                new_obj = cls(stacks=stacks)
        else:
            new_obj.stacks = stacks

        target.statuses[name] = new_obj
        new_obj.on_attach(self, target)
        self.bgm.evaluate(reason=f"获得状态:{name}")
        return new_obj

    def set_status_stacks(self, target: Character, name: str, new_stacks: int) -> None:
        st = target.statuses.get(name)
        if not st:
            return
        new_stacks = floor_val(new_stacks)
        if new_stacks <= 0:
            self.remove_status(target, name)
        else:
            st.stacks = new_stacks

    def consume_status(self, target: Character, name: str, amount: int) -> bool:
        amount = floor_val(amount)
        st = target.statuses.get(name)
        if not st or st.stacks < amount:
            return False
        self.set_status_stacks(target, name, st.stacks - amount)
        return True

    def remove_status(self, target: Character, name: str) -> None:
        st = target.statuses.pop(name, None)
        if st:
            self.events.emit(EventType.STATUS_REMOVE, self, target=target, status=st)
            st.on_detach(self)
            self.bgm.evaluate(reason=f"移除状态:{name}")

    # ------------------------------------------------------------------
    # 掷骰与伤害管线
    # ------------------------------------------------------------------
    def roll_skill(
        self,
        action: Action,
        clash_opponent: Optional[Character] = None,
    ) -> int:
        skill = action.skill
        actor = action.actor
        if skill is None:
            return 0

        roll_state: Dict[str, Any] = {
            "base_power": floor_val(skill.base_power),
            "coins": [c.clone() for c in skill.coins],
            "coin_results": [],
            "total": 0,
            "action": action,
            "clash_opponent": clash_opponent,
        }

        skill.on_before_roll(self, actor, roll_state)
        self.events.emit(
            EventType.BEFORE_SKILL_ROLL,
            self,
            actor=actor,
            skill=skill,
            action=action,
            roll_state=roll_state,
        )

        total = floor_val(roll_state["base_power"])
        coins = roll_state["coins"]
        total_coins = len(coins)

        if self.roll_handler:
            self.roll_handler.on_roll_begin(self, actor, skill, total, total_coins)

        coin_results = []
        for idx, coin in enumerate(coins, start=1):
            if self.roll_handler:
                self.roll_handler.before_coin_reveal(self, actor, skill, idx, total_coins, coin)

            triggered = self.rng.random() < coin.probability
            val = floor_val(coin.value) if triggered else 0
            total += val
            coin_results.append((coin, triggered, val))

            if self.roll_handler:
                self.roll_handler.after_coin_reveal(
                    self, actor, skill, idx, total_coins, coin, triggered, val, total
                )
            else:
                face = f"正面({val:+d})" if triggered else "反面(+0)"
                self.log(
                    f"    [{actor.name}:{skill.name}] 硬币 {idx}/{total_coins} -> {face} (当前:{total})"
                )

            if coin.on_roll:
                coin.on_roll(self, skill, actor, triggered)

        roll_state["coin_results"] = coin_results
        roll_state["total"] = floor_val(total)

        skill.on_after_roll(self, actor, roll_state)
        self.events.emit(
            EventType.AFTER_SKILL_ROLL,
            self,
            actor=actor,
            skill=skill,
            action=action,
            roll_state=roll_state,
        )

        final_total = floor_val(roll_state["total"])
        if self.roll_handler:
            self.roll_handler.on_roll_end(self, actor, skill, final_total)

        return final_total

    def apply_damage(
        self,
        attacker: Optional[Character],
        target: Character,
        raw_damage: int,
        skill: Optional[Skill] = None,
        action: Optional[Action] = None,
        source_type: str = "skill",
        trigger_on_hit_statuses: bool = True,
        allow_breath: bool = True,
        is_dot: Optional[bool] = None,
        is_bonus_damage: bool = False,
        ignore_protection_fragile: Optional[bool] = None,
    ) -> int:
        if not target.alive:
            return 0

        resolved_is_dot = source_type.startswith("dot") if is_dot is None else is_dot
        resolved_is_bonus = is_bonus_damage or (
            source_type in ("bonus", "additional", "extra", "追加伤害")
        )
        resolved_ignore_pf = (
            (resolved_is_dot or resolved_is_bonus)
            if ignore_protection_fragile is None
            else ignore_protection_fragile
        )

        dmg_ctx = DamageContext(
            attacker=attacker,
            target=target,
            raw_damage=floor_val(raw_damage),
            damage=floor_val(raw_damage),
            skill=skill,
            source_type=source_type,
            trigger_on_hit_statuses=trigger_on_hit_statuses,
            allow_breath=allow_breath,
            is_dot=resolved_is_dot,
            is_bonus_damage=resolved_is_bonus,
            ignore_protection_fragile=resolved_ignore_pf,
        )

        if skill and action:
            skill.modify_damage(self, action, dmg_ctx)

        self.events.emit(
            EventType.CALCULATE_DAMAGE,
            self,
            damage_context=dmg_ctx,
            action=action,
        )
        if dmg_ctx.cancelled:
            return 0

        self.events.emit(
            EventType.BEFORE_TAKE_DAMAGE,
            self,
            damage_context=dmg_ctx,
            action=action,
        )
        if dmg_ctx.cancelled:
            return 0

        final_dmg = max(0, floor_val(dmg_ctx.damage))
        dmg_ctx.damage = final_dmg

        actual_loss = min(target.hp, final_dmg)
        target.hp = max(0, target.hp - final_dmg)

        src_label = f"{attacker.name} -> " if attacker else ""
        self.log(
            f"  [伤害] {src_label}{target.name} 受到 {final_dmg} 点伤害 "
            f"(HP: {target.hp}/{target.max_hp})"
        )

        self.events.emit(
            EventType.AFTER_TAKE_DAMAGE,
            self,
            damage_context=dmg_ctx,
            actual_loss=actual_loss,
            action=action,
        )

        if skill and action and target.alive:
            skill.on_hit(self, action, target, dmg_ctx)

        self._check_death(target, killer=attacker, damage_context=dmg_ctx)
        self.bgm.evaluate(reason="生命值变化")
        return actual_loss

    def apply_bonus_damage(
        self,
        attacker: Optional[Character],
        target: Character,
        raw_damage: int,
        skill: Optional[Skill] = None,
        action: Optional[Action] = None,
        trigger_on_hit_statuses: bool = False,
        allow_breath: bool = False,
        source_type: str = "bonus",
    ) -> int:
        """
        追加伤害专用接口：
        自动标记 is_bonus_damage=True、ignore_protection_fragile=True，
        不会因【易损】加伤，也不会因【守护】减伤。
        """
        return self.apply_damage(
            attacker=attacker,
            target=target,
            raw_damage=raw_damage,
            skill=skill,
            action=action,
            source_type=source_type,
            trigger_on_hit_statuses=trigger_on_hit_statuses,
            allow_breath=allow_breath,
            is_bonus_damage=True,
            ignore_protection_fragile=True,
        )

    def _check_death(
        self,
        char: Character,
        killer: Optional[Character] = None,
        damage_context: Optional[DamageContext] = None,
    ) -> None:
        if char.hp <= 0 and char.alive:
            ctx = self.events.emit(
                EventType.CHARACTER_DEATH,
                self,
                character=char,
                killer=killer,
                damage_context=damage_context,
            )
            if not ctx.cancelled and char.hp <= 0:
                char.alive = False
                self.log(f"  [阵亡] {char.name} 倒下了！")

    # ------------------------------------------------------------------
    # 追击与外部动作插入
    # ------------------------------------------------------------------
    def enqueue_action(self, action: Action, front: bool = False) -> None:
        if front:
            self.action_queue.appendleft(action)
        else:
            self.action_queue.append(action)

    # ------------------------------------------------------------------
    # 选技顺序逻辑
    # ------------------------------------------------------------------
    def compute_selection_order(self) -> List[Tuple[Character, int]]:
        living = self.get_living_characters()
        all_slots: List[Tuple[Character, int]] = []
        for c in living:
            for slot_idx in range(c.action_slots):
                all_slots.append((c, slot_idx))

        hp_buckets: Dict[int, List[Tuple[Character, int]]] = {}
        for item in all_slots:
            hp_buckets.setdefault(item[0].hp, []).append(item)

        ordered: List[Tuple[Character, int]] = []
        starter = self._tie_turn_starter
        for hp_val in sorted(hp_buckets.keys(), reverse=True):
            bucket = hp_buckets[hp_val]
            teams_in_bucket: Dict[str, List[Tuple[Character, int]]] = {}
            for item in bucket:
                teams_in_bucket.setdefault(item[0].team, []).append(item)

            team_names = list(teams_in_bucket.keys())
            if starter in team_names:
                team_names.remove(starter)
                team_names.insert(0, starter)

            while any(teams_in_bucket[t] for t in team_names):
                for t in team_names:
                    if teams_in_bucket[t]:
                        ordered.append(teams_in_bucket[t].pop(0))

        ctx = self.events.emit(
            EventType.SELECTION_ORDER, self, order=ordered, tie_starter=starter
        )
        return ctx.data["order"]

    def _default_ai_select(
        self, char: Character, slot_idx: int, previous_selections: List[Action]
    ) -> Action:
        usable = [
            sk
            for sk in char.get_usable_skills(self)
            if self.get_selectable_targets(char, sk, for_ai=True)
        ]
        if not usable:
            return Action(actor=char, skill=None, targets=[], consume_from_hand=False)

        skill = self.rng.choice(usable)
        pool = self.get_selectable_targets(char, skill, for_ai=True)

        if skill.clash_aoe:
            # 拼点群攻：只选 1 名目标作为拼点对象，胜出后再释放全范围打击
            targets = [self.rng.choice(pool)]
        elif skill.is_aoe:
            targets = list(pool)
        elif skill.target_type == "self":
            targets = [char]
        else:
            targets = [self.rng.choice(pool)]

        return Action(actor=char, skill=skill, targets=targets)

    # ------------------------------------------------------------------
    # 主流程控制
    # ------------------------------------------------------------------
    def _log_turn_board(self) -> None:
        self.log("[场上状态]")
        if self.bgm.current_track:
            tr = self.bgm.current_track
            prog = self.bgm._sync_current_progress()
            owner_name = tr.owner.name if tr.owner else "场地"
            self.log(f"  [当前BGM] {owner_name} - 《{tr.title}》(单曲循环 | 进度: {prog:.2f}s)")
        for c in self.get_living_characters():
            st_str = (
                " | 状态: "
                + ", ".join(
                    f"{k}({'益' if v.is_buff else '减' if v.is_debuff else '中'}):{v.stacks}"
                    for k, v in c.statuses.items()
                )
                if c.statuses
                else ""
            )
            self.log(
                f"  [{c.team}] {c.name} HP:{c.hp}/{c.max_hp} "
                f"(余牌:{len(c.available_skills)}){st_str}"
            )
            p_str = c.format_triggered_passives(self)
            if p_str:
                self.log(f"    当前触发被动：{p_str}")

    def run_game(self) -> Optional[str]:
        self.log("=== 战斗开始 ===")
        self.events.emit(EventType.GAME_START, self)
        self.bgm.evaluate(reason="战斗开始")

        while not self.game_over and self.round_num < self.max_rounds:
            self.round_num += 1
            self.run_round()
            if self.check_victory():
                break

        self.events.emit(EventType.GAME_END, self, winner=self.winner_team)
        self.log(f"=== 战斗结束，胜者：{self.winner_team} ===")
        return self.winner_team

    def run_round(self) -> None:
        self.log(f"\n================ 第 {self.round_num} 轮 ================")

        for c in self.get_living_characters():
            if len(c.available_skills) == 0:
                ctx = self.events.emit(
                    EventType.SKILL_DISTRIBUTE, self, character=c, should_refill=True
                )
                if not ctx.cancelled and ctx.data["should_refill"]:
                    added = c.refill_skills()
                    self.log(f"[技能分发] {c.name} 技能已用尽，补充 {len(added)} 个技能")
            else:
                self.events.emit(
                    EventType.SKILL_DISTRIBUTE, self, character=c, should_refill=False
                )
                self.log(
                    f"[技能分发] {c.name} 上轮仍有 {len(c.available_skills)} 个技能未用完，本轮不补充！"
                )

        self.events.emit(EventType.ROUND_START, self, round_num=self.round_num)

        for t in range(1, self.turns_per_round + 1):
            if self.check_victory():
                break
            self.turn_in_round = t
            self.global_turn += 1
            self.run_turn()

        self.events.emit(EventType.ROUND_END, self, round_num=self.round_num)

    def run_turn(self) -> None:
        self.log(f"\n--- 第 {self.round_num} 轮 / 第 {self.turn_in_round} 回合 ---")
        self.events.emit(
            EventType.TURN_START,
            self,
            round_num=self.round_num,
            turn_in_round=self.turn_in_round,
            global_turn=self.global_turn,
        )
        self.bgm.evaluate(reason="回合开始")

        if self.show_turn_board:
            self._log_turn_board()

        order = self.compute_selection_order()
        self.current_turn_selections = []

        for char, slot_idx in order:
            if not char.alive:
                continue
            pre_ctx = self.events.emit(
                EventType.BEFORE_SELECT_ACTION,
                self,
                character=char,
                slot_idx=slot_idx,
                previous_selections=self.current_turn_selections,
            )
            if pre_ctx.cancelled:
                continue

            if char.ai_selector:
                action = char.ai_selector(self, char, slot_idx, list(self.current_turn_selections))
            elif self.manual_selector:
                action = self.manual_selector(self, char, slot_idx, list(self.current_turn_selections))
            else:
                action = self._default_ai_select(char, slot_idx, list(self.current_turn_selections))

            if action.skill is not None:
                if action.consume_from_hand and action.skill in char.available_skills:
                    char.available_skills.remove(action.skill)
                action.skill.on_select(self, action)

            self.current_turn_selections.append(action)
            self.events.emit(
                EventType.AFTER_SELECT_ACTION,
                self,
                action=action,
                all_selections=self.current_turn_selections,
            )

            if action.skill:
                tgt_names = ",".join(
                    f"{t.name}[队友]" if (t.team == char.team and t is not char) else t.name
                    for t in action.targets
                )
                self.log(
                    f"[选技] {char.name} 选择 [{action.skill.name}({action.skill.format_formula()})] -> {tgt_names}"
                )
            else:
                self.log(f"[选技] {char.name} 无可用技能（本回合只能挨打）")

        teams = self.get_teams_alive()
        if len(teams) >= 2:
            if self._tie_turn_starter in teams:
                idx = teams.index(self._tie_turn_starter)
                self._tie_turn_starter = teams[(idx + 1) % len(teams)]
            else:
                self._tie_turn_starter = teams[0]

        planned = [
            a for a in self.current_turn_selections
            if not a.cancelled and a.skill is not None
        ]

        # 1) 非拼点群攻优先结算（同队普通群攻不争夺释放权，除非含无差别群攻或有敌对群攻）
        non_clash_aoes = [
            a for a in planned
            if a.skill is not None and a.skill.is_aoe and not a.skill.clash_aoe and a.actor.alive
        ]
        if len(non_clash_aoes) == 1:
            self._resolve_aoe(non_clash_aoes[0])
            self._drain_pursuit_queue()
        elif len(non_clash_aoes) > 1:
            if self._need_aoe_contest(non_clash_aoes):
                winners = self._resolve_aoe_contest(non_clash_aoes)
            else:
                winners = non_clash_aoes

            for aoe_act in winners:
                if self.check_victory():
                    break
                if aoe_act.cancelled or not aoe_act.actor.alive:
                    continue
                self._resolve_aoe(
                    aoe_act,
                    pre_rolled_aoe=aoe_act.extra.get("aoe_contest_roll"),
                )
                self._drain_pursuit_queue()

            # 若有在群攻竞争中落败的非拼点红币群攻，等胜出的群攻全部释放完后，再以碎币范围生效
            if winners and not self.check_victory():
                for act in non_clash_aoes:
                    if self.check_victory():
                        break
                    if (
                        act.extra.get("aoe_contest_lost")
                        and act.actor.alive
                        and act.skill is not None
                        and (act.skill.has_unbreakable() or act.extra.get("force_unbreakable"))
                    ):
                        u_act = self._build_unbreakable_action(winners[0], act, from_aoe=True)
                        if u_act is not None:
                            self._resolve_unbreakable_attack(u_act)
                            self._drain_pursuit_queue()

        # 2) 拼点群攻与其余常规技能进入拼点/单方面攻击阶段
        remaining = [
            a for a in planned
            if not a.cancelled
            and a.skill is not None
            and not a.extra.get("aoe_resolved")
        ]
        self.events.emit(EventType.RESOLVE_MATCHUPS, self, actions=remaining)

        clashes, unopposed = self._pair_actions(remaining)

        for act1, act2 in clashes:
            if self.check_victory():
                break
            self._resolve_clash(act1, act2)
            self._drain_pursuit_queue()

        for act in unopposed:
            self.action_queue.append(act)
        self._drain_pursuit_queue()

        if not self.check_victory():
            self.events.emit(
                EventType.TURN_END,
                self,
                round_num=self.round_num,
                turn_in_round=self.turn_in_round,
            )
        self._drain_pursuit_queue()
        self.bgm.evaluate(reason="回合结束")

    def _pair_actions(
        self, actions: List[Action]
    ) -> Tuple[List[Tuple[Action, Action]], List[Action]]:
        used = set()
        clashes: List[Tuple[Action, Action]] = []
        unopposed: List[Action] = []

        def _can_clash(act: Action) -> bool:
            return (
                not act.cancelled
                and act.skill is not None
                and act.clashable
                and act.skill.clashable
                and (not act.skill.is_aoe or act.skill.clash_aoe)
            )

        # 1) 优先为【拼点群攻】配对（先匹配互指，若目标选了别人则强制拉住该目标进入1v1拼点）
        for i, a1 in enumerate(actions):
            if i in used or not _can_clash(a1) or not a1.skill.clash_aoe:
                continue

            matched_idx = None
            for j, a2 in enumerate(actions):
                if j == i or j in used or not _can_clash(a2):
                    continue
                if a1.actor is a2.actor:
                    continue
                if not self.allow_ally_clash and a1.actor.team == a2.actor.team:
                    continue
                if a2.actor in a1.targets and a1.actor in a2.targets:
                    matched_idx = j
                    break

            if matched_idx is None:
                for j, a2 in enumerate(actions):
                    if j == i or j in used or not _can_clash(a2):
                        continue
                    if a1.actor is a2.actor:
                        continue
                    if not self.allow_ally_clash and a1.actor.team == a2.actor.team:
                        continue
                    if a2.actor in a1.targets:
                        matched_idx = j
                        break

            if matched_idx is not None:
                used.add(i)
                used.add(matched_idx)
                clashes.append((a1, actions[matched_idx]))

        # 2) 常规技能按双方互指配对拼点
        for i, a1 in enumerate(actions):
            if i in used or a1.cancelled or not a1.skill:
                continue
            matched = False
            if _can_clash(a1):
                for j in range(i + 1, len(actions)):
                    if j in used:
                        continue
                    a2 = actions[j]
                    if not _can_clash(a2):
                        continue
                    if a1.actor is a2.actor:
                        continue
                    if not self.allow_ally_clash and a1.actor.team == a2.actor.team:
                        continue
                    if a2.actor in a1.targets and a1.actor in a2.targets:
                        used.add(i)
                        used.add(j)
                        clashes.append((a1, a2))
                        matched = True
                        break
            if not matched:
                used.add(i)
                unopposed.append(a1)

        return clashes, unopposed

    def _drain_pursuit_queue(self) -> None:
        while self.action_queue and not self.check_victory():
            act = self.action_queue.popleft()
            if act.cancelled or not act.actor.alive or act.skill is None:
                continue
            if act.skill.is_aoe and not act.skill.clash_aoe:
                self._resolve_aoe(act)
            else:
                self._resolve_unopposed(act)

    # ------------------------------------------------------------------
    # 非拼点群攻结算（不视为拼点）
    # ------------------------------------------------------------------
    def _need_aoe_contest(self, aoe_actions: List[Action]) -> bool:
        """
        只有存在至少一对存在竞争关系的群攻时，才需要先行掷骰争夺释放权：
        - 处于不同队伍（多方敌对同时放群攻）
        - 或其中存在无差别群攻（无差别会波及队友，即使队友之间也要争夺）
        """
        if len(aoe_actions) <= 1:
            return False
        if any(a.skill is not None and a.skill.indiscriminate for a in aoe_actions):
            return True
        teams = {a.actor.team for a in aoe_actions}
        return len(teams) > 1

    def _are_aoe_rivals(self, a: Action, b: Action) -> bool:
        if a is b:
            return False
        if a.actor.team != b.actor.team:
            return True
        return bool(
            (a.skill and a.skill.indiscriminate)
            or (b.skill and b.skill.indiscriminate)
        )

    def _resolve_aoe_contest(self, aoe_actions: List[Action]) -> List[Action]:
        """
        多方/无差别非拼点群攻竞争：
        - 敌对队伍之间互相争夺释放权；
        - 同队队友之间不互相争夺，除非其中存在无差别群攻。
        返回所有最终获准释放的群攻 Action 列表。
        """
        self.log(
            f"[群攻竞争] 场上共 {len(aoe_actions)} 个群攻技能存在释放冲突，先行掷骰决定释放权！"
        )
        start_ctx = self.events.emit(
            EventType.AOE_CONTEST_START, self, actions=aoe_actions
        )
        if start_ctx.cancelled:
            for act in aoe_actions:
                act.cancelled = True
            return []

        contenders = [a for a in aoe_actions if not a.cancelled and a.actor.alive]
        final_winners: List[Action] = []
        tie_count = 0

        while contenders:
            rolls: Dict[int, int] = {}
            roll_list: List[Tuple[Action, int]] = []
            for act in contenders:
                if not act.actor.alive or act.cancelled:
                    continue
                r = self.roll_skill(act, clash_opponent=None)
                act.extra["aoe_contest_roll"] = r
                if not self.roll_handler:
                    self.log(f"  [群攻先行掷骰] {act.actor.name}[{act.skill.name}] 掷出 {r} 点")
                rolls[id(act)] = r
                roll_list.append((act, r))

            if not roll_list:
                return final_winners

            self.events.emit(
                EventType.AOE_CONTEST_ROLL,
                self,
                results=roll_list,
                tie_count=tie_count,
            )

            # 1) 淘汰所有点数严格小于其竞争对手最大点数的群攻
            survivors: List[Action] = []
            eliminated: List[Tuple[Action, int, int]] = []
            for act, r in roll_list:
                rival_rolls = [
                    other_r
                    for other_act, other_r in roll_list
                    if self._are_aoe_rivals(act, other_act)
                ]
                if not rival_rolls:
                    survivors.append(act)
                    continue
                max_rival = max(rival_rolls)
                if r < max_rival:
                    eliminated.append((act, r, max_rival))
                else:
                    survivors.append(act)

            for loser_act, loser_roll, max_rival in eliminated:
                loser_act.cancelled = True
                loser_act.extra["aoe_contest_lost"] = True
                if loser_act.skill and loser_act.skill.has_unbreakable():
                    self.log(
                        f"  [群攻落败] {loser_act.actor.name}[{loser_act.skill.name}] "
                        f"点数 {loser_roll} < {max_rival}，将在群攻结束后以碎币生效！"
                    )
                else:
                    self.log(
                        f"  [群攻落败] {loser_act.actor.name}[{loser_act.skill.name}] "
                        f"点数 {loser_roll} < {max_rival}，本回合无法释放群攻！"
                    )
                self.events.emit(
                    EventType.AOE_CONTEST_LOSE,
                    self,
                    action=loser_act,
                    roll=loser_roll,
                    max_roll=max_rival,
                )

            if not survivors:
                return final_winners

            # 2) 在存活者之间检查是否仍有竞争对手平局
            tied_contenders: List[Action] = []
            decided_winners: List[Action] = []
            for act in survivors:
                r = rolls[id(act)]
                surviving_rival_rolls = [
                    rolls[id(other)]
                    for other in survivors
                    if self._are_aoe_rivals(act, other)
                ]
                if surviving_rival_rolls and r <= max(surviving_rival_rolls):
                    tied_contenders.append(act)
                else:
                    decided_winners.append(act)

            for win_act in decided_winners:
                win_roll = win_act.extra["aoe_contest_roll"]
                self.log(
                    f"  [群攻胜出] {win_act.actor.name}[{win_act.skill.name}] "
                    f"以 {win_roll} 点获得本回合群攻释放权！"
                )
                self.events.emit(
                    EventType.AOE_CONTEST_WIN,
                    self,
                    action=win_act,
                    roll=win_roll,
                )
                final_winners.append(win_act)

            if not tied_contenders:
                return final_winners

            tie_count += 1
            if tie_count >= self.max_clash_ties:
                self.log(
                    f"  [群攻平局取消] 连续 {self.max_clash_ties} 次平局，取消平局双方群攻！"
                )
                for act in tied_contenders:
                    act.cancelled = True
                    act.extra["aoe_contest_lost"] = True
                return final_winners

            names = "、".join(a.actor.name for a in tied_contenders)
            self.log(f"  [群攻平局] {names} 点数打平，平局重投以决出释放权！")
            contenders = tied_contenders

        return final_winners

    def _resolve_aoe(
        self,
        aoe_act: Action,
        pre_rolled_aoe: Optional[int] = None,
    ) -> None:
        if aoe_act.cancelled or not aoe_act.actor.alive or aoe_act.skill is None:
            return

        skill = aoe_act.skill
        if aoe_act.targets:
            targets = [t for t in aoe_act.targets if t.alive]
        else:
            targets = self._get_aoe_targets(aoe_act)

        if not targets:
            aoe_act.extra["aoe_resolved"] = True
            return

        label = "无差别群攻" if skill.indiscriminate else "群攻"
        tgt_names = ",".join(t.name for t in targets)
        self.log(
            f"[{label}] {aoe_act.actor.name}[{skill.name}({skill.format_formula()})] -> {tgt_names}"
        )
        skill.on_aoe_start(self, aoe_act)
        start_ctx = self.events.emit(
            EventType.AOE_START, self, action=aoe_act, targets=targets
        )
        if start_ctx.cancelled or not aoe_act.actor.alive:
            aoe_act.extra["aoe_resolved"] = True
            return

        # 1) 自身先掷出群攻技能点数（若已在群攻竞争中掷过，则直接沿用）
        if pre_rolled_aoe is not None:
            aoe_roll = floor_val(pre_rolled_aoe)
            self.log(f"  [群攻点数] {aoe_act.actor.name} 沿用先行掷出的 {aoe_roll} 点")
        else:
            aoe_roll = self.roll_skill(aoe_act, clash_opponent=None)
            if not self.roll_handler:
                self.log(f"  [群攻掷骰] {aoe_act.actor.name} 掷出 {aoe_roll} 点")

        # 2) 其他玩家同时掷出各自技能点数并判定
        hit_targets: List[Character] = []
        deferred_unbreakables: List[Action] = []

        for tgt in targets:
            if not tgt.alive:
                continue

            active_actions = [
                a for a in self.current_turn_selections
                if a.actor is tgt and not a.cancelled and a.skill is not None
            ]
            contest_lost_actions = [
                a for a in self.current_turn_selections
                if a.actor is tgt
                and a.extra.get("aoe_contest_lost")
                and "aoe_contest_roll" in a.extra
            ]

            if not active_actions and not contest_lost_actions:
                t_ctx = self.events.emit(
                    EventType.AOE_TARGET_ROLL,
                    self,
                    aoe_action=aoe_act,
                    aoe_roll=aoe_roll,
                    target=tgt,
                    target_action=None,
                    target_roll=0,
                )
                eff_roll = floor_val(t_ctx.data["target_roll"])
                if eff_roll < aoe_roll:
                    self.log(
                        f"  [群攻判定] {tgt.name} 本回合无可用技能（点数 {eff_roll} < {aoe_roll}），受到群攻影响！"
                    )
                    hit_targets.append(tgt)
                else:
                    self.log(
                        f"  [群攻判定] {tgt.name}（点数 {eff_roll} >= {aoe_roll}）未受群攻影响"
                    )
                continue

            rolls_for_tgt: List[int] = []

            # 刚在群攻竞争中落败的群攻技能直接沿用先行点数
            for lost_act in contest_lost_actions:
                raw_t_roll = floor_val(lost_act.extra["aoe_contest_roll"])
                t_ctx = self.events.emit(
                    EventType.AOE_TARGET_ROLL,
                    self,
                    aoe_action=aoe_act,
                    aoe_roll=aoe_roll,
                    target=tgt,
                    target_action=lost_act,
                    target_roll=raw_t_roll,
                )
                eff_roll = floor_val(t_ctx.data["target_roll"])
                rolls_for_tgt.append(eff_roll)
                if eff_roll < aoe_roll:
                    self.log(
                        f"  [群攻判定] {tgt.name}[{lost_act.skill.name}] 先行点数 {eff_roll} < {aoe_roll}，"
                        f"将受到群攻影响！"
                    )
                else:
                    self.log(
                        f"  [群攻判定] {tgt.name}[{lost_act.skill.name}] 先行点数 {eff_roll} >= {aoe_roll}，"
                        f"虽未夺得释放权但抵御了本次群攻！"
                    )

            # 其余未取消的技能（含普通技能、同队先释放的群攻或拼点群攻）正常掷点数对抗
            for t_act in active_actions:
                u_act: Optional[Action] = None
                raw_t_roll = self.roll_skill(t_act, clash_opponent=None)
                t_ctx = self.events.emit(
                    EventType.AOE_TARGET_ROLL,
                    self,
                    aoe_action=aoe_act,
                    aoe_roll=aoe_roll,
                    target=tgt,
                    target_action=t_act,
                    target_roll=raw_t_roll,
                )
                eff_roll = floor_val(t_ctx.data["target_roll"])
                rolls_for_tgt.append(eff_roll)

                if eff_roll < aoe_roll:
                    already_fired = bool(t_act.extra.get("aoe_resolved"))
                    t_act.cancelled = True
                    self.events.emit(
                        EventType.AOE_SUPPRESS,
                        self,
                        aoe_action=aoe_act,
                        aoe_roll=aoe_roll,
                        target=tgt,
                        target_action=t_act,
                        target_roll=eff_roll,
                    )
                    if already_fired:
                        self.log(
                            f"  [群攻判定] {tgt.name}[{t_act.skill.name}] 点数 {eff_roll} < {aoe_roll}，将受到群攻影响！"
                        )
                    else:
                        if t_act.skill.has_unbreakable() or t_act.extra.get("force_unbreakable"):
                            u_act = self._build_unbreakable_action(aoe_act, t_act, from_aoe=True)

                        if u_act is not None:
                            assert u_act.skill is not None
                            deferred_unbreakables.append(u_act)
                            if u_act.skill.is_aoe:
                                orig_tgts = ",".join(x.name for x in self._get_aoe_targets(u_act)) or "敌方全体"
                                desc = f"以范围群攻({orig_tgts})生效"
                            else:
                                orig_tgts = ",".join(x.name for x in u_act.targets) or tgt.name
                                desc = f"攻击原定目标({orig_tgts})"
                            self.log(
                                f"  [群攻判定] {tgt.name}[{t_act.skill.name}] 点数 {eff_roll} < {aoe_roll}，"
                                f"触发【不可摧毁】碎币 [{u_act.skill.format_formula()}]，将在群攻结束后{desc}！"
                            )
                        else:
                            self.log(
                                f"  [群攻判定] {tgt.name}[{t_act.skill.name}] 点数 {eff_roll} < {aoe_roll}，"
                                f"本回合无法使用且将受到群攻影响！"
                            )
                else:
                    self.log(
                        f"  [群攻判定] {tgt.name}[{t_act.skill.name}] 点数 {eff_roll} >= {aoe_roll}，抵御群攻！"
                    )

            if max(rolls_for_tgt) < aoe_roll:
                hit_targets.append(tgt)

        # 3) 对所有点数小于群攻的玩家结算伤害、负面Buff与追加机制
        for tgt in hit_targets:
            if not aoe_act.actor.alive:
                break
            if not tgt.alive:
                continue
            self.apply_damage(
                attacker=aoe_act.actor,
                target=tgt,
                raw_damage=aoe_roll,
                skill=skill,
                action=aoe_act,
                source_type="aoe",
                trigger_on_hit_statuses=True,
                allow_breath=True,
            )

        skill.on_attack_end(self, aoe_act)
        skill.on_aoe_end(self, aoe_act, hit_targets)
        self.events.emit(
            EventType.AOE_END,
            self,
            action=aoe_act,
            aoe_roll=aoe_roll,
            hit_targets=hit_targets,
        )
        aoe_act.extra["aoe_resolved"] = True

        # 4) 等群攻完整释放完毕后，存活的红币技能/红币群攻以碎币生效
        for deferred_act in deferred_unbreakables:
            if self.check_victory():
                break
            if deferred_act.actor.alive:
                self._resolve_unbreakable_attack(deferred_act)
                self._drain_pursuit_queue()

    # ------------------------------------------------------------------
    # 拼点与不可摧毁结算
    # ------------------------------------------------------------------
    def _build_unbreakable_action(
        self,
        winner_act: Action,
        loser_act: Action,
        from_aoe: bool = False,
    ) -> Optional[Action]:
        """评估拼点/抗群攻失败方是否保留不可摧毁技能/变动值，并兼容外部强行摧毁等钩子"""
        assert loser_act.skill is not None
        skill = loser_act.skill

        force_destroy = bool(loser_act.extra.get("destroy_unbreakable", False))
        force_unbreakable = bool(loser_act.extra.get("force_unbreakable", False))

        if force_destroy:
            surviving_coins: List[Coin] = []
            broken_coins: List[Coin] = [c.clone() for c in skill.coins]
            survives = False
        elif force_unbreakable or skill.unbreakable:
            surviving_coins = [c.clone() for c in skill.coins]
            broken_coins = []
            survives = True
        else:
            surviving_coins = [c.clone() for c in skill.coins if c.unbreakable]
            broken_coins = [c.clone() for c in skill.coins if not c.unbreakable]
            survives = len(surviving_coins) > 0

        # 红币群攻碎后默认依然保留范围攻击；单体红币则只朝原预选目标攻击
        default_keep_aoe = loser_act.extra.get("unbreakable_keep_aoe", skill.is_aoe)

        ctx = self.events.emit(
            EventType.BEFORE_UNBREAKABLE_RESOLVE,
            self,
            winner_action=winner_act,
            loser_action=loser_act,
            surviving_coins=surviving_coins,
            broken_coins=broken_coins,
            survives=survives,
            forced_coin_value=1,
            keep_aoe=bool(default_keep_aoe),
            targets=list(loser_act.targets),
            from_aoe=from_aoe,
        )

        if ctx.cancelled or not ctx.data["survives"]:
            return None

        final_coins: List[Coin] = ctx.data["surviving_coins"]
        forced_val = floor_val(ctx.data["forced_coin_value"])

        if not final_coins and not (skill.unbreakable or force_unbreakable):
            return None

        new_skill = skill.clone()
        new_skill.coins = []
        for c in final_coins:
            nc = c.clone()
            nc.value = forced_val
            new_skill.coins.append(nc)

        if ctx.data.get("keep_aoe", False):
            new_skill.is_aoe = True
            new_skill.clash_aoe = False
            new_skill.indiscriminate = skill.indiscriminate
        else:
            new_skill.is_aoe = False
            new_skill.clash_aoe = False
            new_skill.indiscriminate = False

        return Action(
            actor=loser_act.actor,
            skill=new_skill,
            targets=list(ctx.data["targets"]),
            consume_from_hand=False,
            is_pursuit=False,
            is_unbreakable_attack=True,
            clashable=False,
            extra={
                "after_clash_lose": not from_aoe,
                "after_aoe_lose": from_aoe,
                "broken_coins_count": len(ctx.data["broken_coins"]),
                "clash_winner": winner_act.actor,
            },
        )

    def _resolve_clash(self, act1: Action, act2: Action) -> None:
        if not act1.actor.alive or not act2.actor.alive:
            if act1.actor.alive and not act2.actor.alive:
                self._resolve_unopposed(act1)
            elif act2.actor.alive and not act1.actor.alive:
                self._resolve_unopposed(act2)
            return

        self.log(
            f"[拼点] {act1.actor.name}[{act1.skill.name}] VS {act2.actor.name}[{act2.skill.name}]"
        )
        act1.skill.on_clash_start(self, act1, act2)
        act2.skill.on_clash_start(self, act2, act1)

        start_ctx = self.events.emit(EventType.CLASH_START, self, action1=act1, action2=act2)
        if start_ctx.cancelled or not act1.actor.alive or not act2.actor.alive:
            return

        tie_count = 0
        winner_act: Optional[Action] = None
        loser_act: Optional[Action] = None
        win_roll = 0
        lose_roll = 0

        while True:
            r1 = self.roll_skill(act1, clash_opponent=act2.actor)
            r2 = self.roll_skill(act2, clash_opponent=act1.actor)
            self.events.emit(
                EventType.CLASH_ROLL, self, action1=act1, action2=act2, roll1=r1, roll2=r2
            )

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
                self.events.emit(
                    EventType.CLASH_TIE,
                    self,
                    action1=act1,
                    action2=act2,
                    roll=r1,
                    tie_count=tie_count,
                )
                if tie_count >= self.max_clash_ties:
                    self.log(
                        f"  [平局取消] 连续 {self.max_clash_ties} 次平局（点数 {r1}），直接取消双方技能！"
                    )
                    self.events.emit(EventType.CLASH_CANCEL_99, self, action1=act1, action2=act2)
                    return

        loser_act.skill.on_clash_lose(self, loser_act, winner_act, lose_roll)
        winner_act.skill.on_clash_win(self, winner_act, loser_act, win_roll)

        unbreakable_act = self._build_unbreakable_action(winner_act, loser_act, from_aoe=False)
        destroyed = unbreakable_act is None

        if destroyed:
            self.log(
                f"  [拼点结果] {winner_act.actor.name}({win_roll}) 胜出！ "
                f"{loser_act.actor.name}({lose_roll}) 技能被摧毁"
            )
            self.events.emit(
                EventType.CLASH_LOSE,
                self,
                winner_action=winner_act,
                loser_action=loser_act,
                roll=lose_roll,
                destroyed=True,
            )
            self.events.emit(EventType.SKILL_DESTROYED, self, action=loser_act)
        else:
            assert unbreakable_act.skill is not None
            self.log(
                f"  [拼点结果] {winner_act.actor.name}({win_roll}) 胜出！ "
                f"{loser_act.actor.name}({lose_roll}) 触发【不可摧毁】，保留为 [{unbreakable_act.skill.format_formula()}]"
            )
            self.events.emit(
                EventType.CLASH_LOSE,
                self,
                winner_action=winner_act,
                loser_action=loser_act,
                roll=lose_roll,
                destroyed=False,
                unbreakable_action=unbreakable_act,
            )

        self.events.emit(
            EventType.CLASH_WIN,
            self,
            winner_action=winner_act,
            loser_action=loser_act,
            roll=win_roll,
        )

        # 1) 胜者攻击结算（若胜者为拼点群攻，此处会先完整释放全范围群攻）
        self._execute_skill_hits(winner_act, win_roll)

        # 2) 若败者仍存活且保留了不可摧毁技能，等胜者群攻放完后再以碎币生效
        if unbreakable_act is not None and unbreakable_act.actor.alive and not self.check_victory():
            self._resolve_unbreakable_attack(unbreakable_act)

    def _resolve_unbreakable_attack(self, act: Action) -> None:
        if not act.actor.alive or act.skill is None:
            return
        if act.skill.is_aoe:
            tgt_names = ",".join(t.name for t in self._get_aoe_targets(act))
        else:
            tgt_names = ",".join(t.name for t in act.targets if t.alive)
        if not tgt_names and act.skill.target_type != "self":
            return
        self.log(
            f"[不可摧毁攻击] {act.actor.name}[{act.skill.name}({act.skill.format_formula()})] -> {tgt_names or act.actor.name}"
        )
        act.skill.on_unbreakable_attack_start(self, act)
        self.events.emit(EventType.UNBREAKABLE_ATTACK_START, self, action=act)
        roll_val = self.roll_skill(act)
        if not self.roll_handler:
            self.log(f"  [掷骰] {act.actor.name} 不可摧毁攻击掷出 {roll_val} 点")
        self._execute_skill_hits(act, roll_val, source_type="unbreakable")

    def _resolve_unopposed(self, act: Action) -> None:
        if not act.actor.alive or act.skill is None:
            return
        prefix = "追击" if act.is_pursuit else "单方面攻击"
        if act.skill.is_aoe:
            tgt_names = ",".join(t.name for t in self._get_aoe_targets(act))
        else:
            tgt_names = ",".join(t.name for t in act.targets if t.alive)
        if not tgt_names and act.skill.target_type != "self":
            return
        self.log(f"[{prefix}] {act.actor.name}[{act.skill.name}] -> {tgt_names or act.actor.name}")
        act.skill.on_unopposed_start(self, act)
        self.events.emit(EventType.UNOPPOSED_ATTACK_START, self, action=act)
        roll_val = self.roll_skill(act)
        if not self.roll_handler:
            self.log(f"  [掷骰] {act.actor.name} 掷出 {roll_val} 点")
        self._execute_skill_hits(
            act, roll_val, source_type="pursuit" if act.is_pursuit else "skill"
        )

    def _execute_skill_hits(
        self, act: Action, roll_val: int, source_type: str = "skill"
    ) -> None:
        assert act.skill is not None
        if act.skill.is_aoe:
            targets = self._get_aoe_targets(act)
            if act.is_unbreakable_attack and targets:
                mode = "无差别碎币群攻" if act.skill.indiscriminate else "碎币群攻"
                tgt_names = ",".join(t.name for t in targets)
                self.log(
                    f"  [{mode}释放] {act.actor.name}[{act.skill.name}] 以碎币释放范围攻击 -> {tgt_names}"
                )
            elif act.skill.clash_aoe and targets:
                mode = "无差别拼点群攻" if act.skill.indiscriminate else "拼点群攻"
                tgt_names = ",".join(t.name for t in targets)
                self.log(
                    f"  [{mode}释放] {act.actor.name}[{act.skill.name}] 直接释放群攻 -> {tgt_names}"
                )
        else:
            targets = [t for t in act.targets if t.alive]
            if not targets and act.skill.target_type == "self" and act.actor.alive:
                targets = [act.actor]

        final_source = (
            "aoe" if (act.skill.is_aoe and source_type == "skill") else source_type
        )
        for tgt in targets:
            if not act.actor.alive:
                break
            if not tgt.alive:
                continue
            self.apply_damage(
                attacker=act.actor,
                target=tgt,
                raw_damage=roll_val,
                skill=act.skill,
                action=act,
                source_type=final_source,
                trigger_on_hit_statuses=True,
                allow_breath=True,
            )

        act.skill.on_attack_end(self, act)
