from __future__ import annotations

import random
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, Union

from .bgm import AudioBackend, BGMManager
from .events import EventBus, EventType
from .math_utils import floor_val
from .models import Action, Character, DamageContext, Skill, StatusEffect
from .statuses import STATUS_CATEGORIES, STATUS_REGISTRY, register_default_statuses


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
        self._evaluating_bgm: bool = False

        self.round_num: int = 0
        self.turn_in_round: int = 0
        self.global_turn: int = 0
        self._tie_turn_starter: str = "P1"  # 同血量时先手队伍：P1先手，然后轮流

        self.action_queue: Deque[Action] = deque()
        self.current_turn_selections: List[Action] = []
        self.game_over: bool = False
        self.winner_team: Optional[str] = None

        # 外部交互回调，如果角色无 ai_selector 且提供了此回调，则交由外部（如CLI）选择
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
        """随时随地开启/关闭自动投掷"""
        self.auto_roll = (not self.auto_roll) if enabled is None else bool(enabled)
        self.events.emit(EventType.AUTO_ROLL_TOGGLED, self, auto_roll=self.auto_roll)
        return self.auto_roll

    def add_character(self, char: Character) -> None:
        self.characters.append(char)
        for p in char.passives:
            p.on_register(self, char)
        for tr in getattr(char, "bgm_tracks", []):
            self.bgm.register_track(tr, owner=char)

    def _evaluate_bgm(self, reason: str = "") -> None:
        """带重入保护的 BGM 评估，防止 BGM_CHANGED 回调触发死循环"""
        if self._evaluating_bgm or self.game_over:
            return
        self._evaluating_bgm = True
        try:
            self.bgm.evaluate(reason=reason)
        finally:
            self._evaluating_bgm = False

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
    # 状态管理（兼容注册表、分类表与任意自定义 StatusEffect 实例）
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
            self._evaluate_bgm(f"状态叠加:{name}")
            return existing

        if stacks <= 0:
            return None

        if new_obj is None:
            cls = STATUS_REGISTRY.get(name)
            if cls is None:
                cat = extra.get("category", STATUS_CATEGORIES.get(name, "neutral"))
                new_obj = StatusEffect(name=name, stacks=stacks, category=cat)
            else:
                new_obj = cls(stacks=stacks)
        else:
            new_obj.stacks = stacks

        target.statuses[name] = new_obj
        new_obj.on_attach(self, target)
        self._evaluate_bgm(f"状态施加:{name}")
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
            self._evaluate_bgm(f"状态变化:{name}")

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
            self._evaluate_bgm(f"状态移除:{name}")

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

        # 技能自身先调整（如自带倍率、基础伤害重写等）
        if skill and action:
            skill.modify_damage(self, action, dmg_ctx)

        # 全局/被动/伤害提升/呼吸等事件介入计算最终伤害
        self.events.emit(
            EventType.CALCULATE_DAMAGE,
            self,
            damage_context=dmg_ctx,
            action=action,
        )
        if dmg_ctx.cancelled:
            return 0

        # 受击前调整（如守护减伤、护盾、免伤）
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

        # 受击后触发（破裂、腐蚀等；由 DoT 引起的伤害标记 trigger_on_hit_statuses=False 防止死循环）
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
        self._evaluate_bgm("生命值变动")
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
        """供角色卡实现追击（front=True 立即插队执行）或额外攻击"""
        if front:
            self.action_queue.appendleft(action)
        else:
            self.action_queue.append(action)

    # ------------------------------------------------------------------
    # 选技顺序逻辑
    # ------------------------------------------------------------------
    def compute_selection_order(self) -> List[Tuple[Character, int]]:
        """
        返回本回合的 (角色, 行动槽序号) 选择顺序：
        - 血量高者先选；
        - 血量完全相等时：以 _tie_turn_starter (首回合为P1) 先手，并在同血量间轮流交替。
        """
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
        if self.bgm.current_track:
            tr = self.bgm.current_track
            prog = self.bgm._sync_current_progress()
            owner_name = tr.owner.name if tr.owner else "场地"
            self.log(f"[当前BGM] {owner_name} - 《{tr.title}》(单曲循环 | 进度: {prog:.2f}s)")

        self.log("[场上状态]")
        for c in self.get_living_characters():
            st_str = (
                " | 状态: " + ", ".join(f"{k}:{v.stacks}" for k, v in c.statuses.items())
                if c.statuses
                else ""
            )
            self.log(
                f"  [{c.team}] {c.name} HP:{c.hp}/{c.max_hp} "
                f"(余牌:{len(c.available_skills)}){st_str}"
            )
            if hasattr(c, "format_triggered_passives"):
                p_str = c.format_triggered_passives(self)
            else:
                p_str = "、".join(
                    p.name for p in c.passives if hasattr(p, "is_triggered") and p.is_triggered(self, c)
                )
            if p_str:
                self.log(f"    当前触发被动：{p_str}")

    def run_game(self) -> Optional[str]:
        self.log("=== 战斗开始 ===")
        self.events.emit(EventType.GAME_START, self)
        self._evaluate_bgm("战斗开始")

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

        # 仅当某角色上一轮用光了所有技能（available_skills 为空），才在新一轮开始时补充
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
        self._evaluate_bgm("轮次开始")

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
        # 在打印面板与选技前刷新 BGM（上一回合新触发的 BGM 在此失去“首回合最高优先级”）
        self._evaluate_bgm("回合开始")

        if self.show_turn_board:
            self._log_turn_board()

        # 1. 决定选择顺序并依次选技（后选方可通过 previous_selections 看到先选方选择）
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
            self._evaluate_bgm("选技后")

            if action.skill:
                tgt_names = ",".join(t.name for t in action.targets)
                self.log(
                    f"[选技] {char.name} 选择 [{action.skill.name}({action.skill.format_formula()})] -> {tgt_names}"
                )
            else:
                self.log(f"[选技] {char.name} 无可用技能（本回合只能挨打）")

        # 轮流切换下回合的同血量先手方
        teams = self.get_teams_alive()
        if len(teams) >= 2:
            if self._tie_turn_starter in teams:
                idx = teams.index(self._tie_turn_starter)
                self._tie_turn_starter = teams[(idx + 1) % len(teams)]
            else:
                self._tie_turn_starter = teams[0]

        # 2. 匹配拼点 / 单方面攻击，允许被动在此修改匹配结果
        planned = [a for a in self.current_turn_selections if not a.cancelled and a.skill is not None]
        self.events.emit(EventType.RESOLVE_MATCHUPS, self, actions=planned)

        clashes, unopposed = self._pair_actions(planned)

        # 执行拼点
        for act1, act2 in clashes:
            if self.check_victory():
                break
            self._resolve_clash(act1, act2)
            self._drain_pursuit_queue()

        # 执行剩余单方面行动及所有追击
        for act in unopposed:
            self.action_queue.append(act)
        self._drain_pursuit_queue()

        # 3. 回合结束阶段（结算 Burn/Tremor/Bleed/Corrosion 衰减以及每回合清零Buff等）
        if not self.check_victory():
            self.events.emit(
                EventType.TURN_END,
                self,
                round_num=self.round_num,
                turn_in_round=self.turn_in_round,
            )
        self._drain_pursuit_queue()
        self._evaluate_bgm("回合结束")

    def _pair_actions(
        self, actions: List[Action]
    ) -> Tuple[List[Tuple[Action, Action]], List[Action]]:
        """
        多对多匹配规则：
        如果 A 选择对拼敌方 B（且在 B 的目标里），而 B 也选择了 A（且均未配对、可拼点），则配对拼点；
        其余未配对的技能按顺序作为单方面行动执行。
        """
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

        self.log(
            f"  [拼点结果] {winner_act.actor.name}({win_roll}) 胜出！ "
            f"{loser_act.actor.name}({lose_roll}) 技能被摧毁"
        )

        # 败者技能摧毁
        loser_act.skill.on_clash_lose(self, loser_act, winner_act, lose_roll)
        self.events.emit(
            EventType.CLASH_LOSE,
            self,
            winner_action=winner_act,
            loser_action=loser_act,
            roll=lose_roll,
        )
        self.events.emit(EventType.SKILL_DESTROYED, self, action=loser_act)

        # 胜者结算攻击
        winner_act.skill.on_clash_win(self, winner_act, loser_act, win_roll)
        self.events.emit(
            EventType.CLASH_WIN,
            self,
            winner_action=winner_act,
            loser_action=loser_act,
            roll=win_roll,
        )

        self._execute_skill_hits(winner_act, win_roll)

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
        self._execute_skill_hits(act, roll_val)

    def _execute_skill_hits(self, act: Action, roll_val: int) -> None:
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
                source_type="pursuit" if act.is_pursuit else "skill",
                trigger_on_hit_statuses=True,
                allow_breath=True,
            )

        act.skill.on_attack_end(self, act)
