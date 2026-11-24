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
    ) -> int:
        if not target.alive:
            return 0

        dmg_ctx = DamageContext(
            attacker=attacker,
            target=target,
            raw_damage=floor_val(raw_damage),
            damage=floor_val(raw_damage),
            skill=skill,
            source_type=source_type,
            trigger_on_hit_statuses=trigger_on_hit_statuses,
            allow_breath=allow_breath,
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
        usable = char.get_usable_skills(self)
        enemies = self.get_enemies(char)
        if not usable or not enemies:
            return Action(actor=char, skill=None, targets=[], consume_from_hand=False)

        skill = self.rng.choice(usable)
        if skill.target_type == "all_enemies":
            targets = list(enemies)
        elif skill.target_type == "self":
            targets = [char]
        elif skill.target_type == "single_ally":
            allies = self.get_allies(char)
            targets = [self.rng.choice(allies)] if allies else [char]
        else:
            targets = [self.rng.choice(enemies)]

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
                tgt_names = ",".join(t.name for t in action.targets)
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

        planned = [a for a in self.current_turn_selections if not a.cancelled and a.skill is not None]
        self.events.emit(EventType.RESOLVE_MATCHUPS, self, actions=planned)

        clashes, unopposed = self._pair_actions(planned)

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

        for i, a1 in enumerate(actions):
            if i in used or a1.cancelled or not a1.skill:
                continue
            matched = False
            if a1.clashable and a1.skill.clashable:
                for j in range(i + 1, len(actions)):
                    if j in used:
                        continue
                    a2 = actions[j]
                    if a2.cancelled or not a2.skill or not a2.clashable or not a2.skill.clashable:
                        continue
                    if a1.actor.team == a2.actor.team:
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
            self._resolve_unopposed(act)

    # ------------------------------------------------------------------
    # 拼点与不可摧毁结算
    # ------------------------------------------------------------------
    def _build_unbreakable_action(
        self, winner_act: Action, loser_act: Action
    ) -> Optional[Action]:
        """评估拼点失败方是否保留不可摧毁技能/变动值，并兼容外部强行摧毁等钩子"""
        assert loser_act.skill is not None
        skill = loser_act.skill

        # 快捷标记兼容（比如胜者 on_clash_win 里写 opp_action.extra["destroy_unbreakable"] = True）
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

        ctx = self.events.emit(
            EventType.BEFORE_UNBREAKABLE_RESOLVE,
            self,
            winner_action=winner_act,
            loser_action=loser_act,
            surviving_coins=surviving_coins,
            broken_coins=broken_coins,
            survives=survives,
            forced_coin_value=1,
        )

        if ctx.cancelled or not ctx.data["survives"]:
            return None

        final_coins: List[Coin] = ctx.data["surviving_coins"]
        forced_val = floor_val(ctx.data["forced_coin_value"])

        # 若既不是0硬币不可摧毁技能，也没有任何存活硬币，则视为彻底摧毁
        if not final_coins and not (skill.unbreakable or force_unbreakable):
            return None

        new_skill = skill.clone()
        new_skill.coins = []
        for c in final_coins:
            nc = c.clone()
            nc.value = forced_val  # 不可摧毁变动值强制变为 +1（额外变动值加成仍会在 roll_skill 中叠加）
            new_skill.coins.append(nc)

        return Action(
            actor=loser_act.actor,
            skill=new_skill,
            targets=list(loser_act.targets),
            consume_from_hand=False,
            is_pursuit=False,
            is_unbreakable_attack=True,
            clashable=False,
            extra={
                "after_clash_lose": True,
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

        # 败者与胜者拼点结果回调
        loser_act.skill.on_clash_lose(self, loser_act, winner_act, lose_roll)
        winner_act.skill.on_clash_win(self, winner_act, loser_act, win_roll)

        unbreakable_act = self._build_unbreakable_action(winner_act, loser_act)
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

        # 1) 胜者攻击结算
        self._execute_skill_hits(winner_act, win_roll)

        # 2) 若败者仍存活且保留了不可摧毁技能，则进行不可摧毁反击
        if unbreakable_act is not None and unbreakable_act.actor.alive and not self.check_victory():
            self._resolve_unbreakable_attack(unbreakable_act)

    def _resolve_unbreakable_attack(self, act: Action) -> None:
        if not act.actor.alive or act.skill is None:
            return
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
        targets = [t for t in act.targets if t.alive]
        if not targets and act.skill.target_type == "self" and act.actor.alive:
            targets = [act.actor]

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
                source_type=source_type,
                trigger_on_hit_statuses=True,
                allow_breath=True,
            )

        act.skill.on_attack_end(self, act)
