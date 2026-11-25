from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple

from .events import EventContext, EventType
from .math_utils import floor_mul, floor_val

if TYPE_CHECKING:
    from .combat import CombatEngine


@dataclass
class Coin:
    """变动值（硬币）"""
    value: int
    probability: float = 0.5
    label: str = ""
    unbreakable: bool = False  # 是否为不可摧毁变动值
    on_roll: Optional[Callable[..., None]] = None

    def clone(self) -> "Coin":
        return Coin(
            value=self.value,
            probability=self.probability,
            label=self.label,
            unbreakable=self.unbreakable,
            on_roll=self.on_roll,
        )


@dataclass
class DamageContext:
    attacker: Optional["Character"]
    target: "Character"
    raw_damage: int
    damage: int
    skill: Optional["Skill"] = None
    source_type: str = "skill"  # "skill" | "dot" | "pursuit" | "unbreakable" | "aoe" | custom
    trigger_on_hit_statuses: bool = True
    allow_breath: bool = True
    cancelled: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def add_flat(self, amount: int) -> None:
        self.damage = floor_val(self.damage + amount)

    def mul_percent(self, percent: int) -> None:
        self.damage = floor_mul(self.damage, percent, 100)

    def mul_ratio(self, numerator: int, denominator: int) -> None:
        self.damage = floor_mul(self.damage, numerator, denominator)


class Skill:
    """技能基类"""

    def __init__(
        self,
        skill_id: str,
        name: str,
        tier: Any = 1,
        base_power: int = 0,
        coins: Optional[List[Coin]] = None,
        target_type: str = "single_enemy",
        clashable: bool = True,
        unbreakable: bool = False,              # True 表示整个技能为不可摧毁技能
        is_aoe: Optional[bool] = None,          # True 表示群攻技能（默认按 target_type / clash_aoe / indiscriminate 自动识别）
        clash_aoe: bool = False,                # True 表示拼点群攻（先与1人拼点，胜出直接释放群攻）
        indiscriminate: Optional[bool] = None,  # True 表示无差别攻击（同时攻击敌人与队友）
        allow_ally_target: Optional[bool] = None,  # None=跟随引擎设置；True=允许选队友；False=禁止选队友
        tags: Optional[Set[str]] = None,
        extra_data: Optional[Dict[str, Any]] = None,
    ):
        self.skill_id = skill_id
        self.name = name
        self.tier = tier
        self.base_power = base_power
        self.coins: List[Coin] = coins if coins is not None else []
        self.target_type = target_type
        self.clashable = clashable
        self.unbreakable = unbreakable
        self.clash_aoe: bool = bool(clash_aoe)
        self.indiscriminate: bool = (
            (target_type == "all_others")
            if indiscriminate is None
            else bool(indiscriminate)
        )
        self.is_aoe: bool = (
            (
                self.clash_aoe
                or self.indiscriminate
                or target_type in ("all_enemies", "all_allies", "all_other_allies", "all_others", "aoe")
            )
            if is_aoe is None
            else bool(is_aoe)
        )
        self.allow_ally_target: Optional[bool] = allow_ally_target
        self.tags: Set[str] = set(tags) if tags else set()
        self.extra_data: Dict[str, Any] = dict(extra_data) if extra_data else {}

    def has_unbreakable(self) -> bool:
        return self.unbreakable or any(c.unbreakable for c in self.coins)

    def format_formula(self) -> str:
        if not self.coins:
            formula = f"{self.base_power}[不可摧毁]" if self.unbreakable else f"{self.base_power}"
        else:
            parts = []
            for c in self.coins:
                s = f"+{c.value}" if c.value >= 0 else f"{c.value}"
                if c.unbreakable and not self.unbreakable:
                    s += "[固]"
                parts.append(s)
            formula = f"{self.base_power}{''.join(parts)}"
            if self.unbreakable:
                formula += "[不可摧毁]"

        if self.clash_aoe and self.indiscriminate:
            formula += "[无差别拼点群攻]"
        elif self.clash_aoe:
            formula += "[拼点群攻]"
        elif self.is_aoe and self.indiscriminate:
            formula += "[无差别群攻]"
        elif self.is_aoe:
            formula += "[群攻]"
        return formula

    def clone(self) -> "Skill":
        cls = self.__class__
        if cls is Skill:
            return Skill(
                skill_id=self.skill_id,
                name=self.name,
                tier=self.tier,
                base_power=self.base_power,
                coins=[c.clone() for c in self.coins],
                target_type=self.target_type,
                clashable=self.clashable,
                unbreakable=self.unbreakable,
                is_aoe=self.is_aoe,
                clash_aoe=self.clash_aoe,
                indiscriminate=self.indiscriminate,
                allow_ally_target=self.allow_ally_target,
                tags=set(self.tags),
                extra_data=dict(self.extra_data),
            )
        obj = cls.__new__(cls)
        obj.__dict__.update(self.__dict__)
        obj.coins = [c.clone() for c in self.coins]
        obj.tags = set(self.tags)
        obj.extra_data = dict(self.extra_data)
        return obj

    # ---- 钩子方法 ----
    def can_use(self, engine: "CombatEngine", actor: "Character") -> bool:
        return True

    def on_select(self, engine: "CombatEngine", action: "Action") -> None:
        pass

    def on_before_roll(
        self, engine: "CombatEngine", actor: "Character", roll_state: Dict[str, Any]
    ) -> None:
        pass

    def on_after_roll(
        self, engine: "CombatEngine", actor: "Character", roll_state: Dict[str, Any]
    ) -> None:
        pass

    def on_clash_start(
        self, engine: "CombatEngine", self_action: "Action", opp_action: "Action"
    ) -> None:
        pass

    def on_clash_win(
        self, engine: "CombatEngine", self_action: "Action", opp_action: "Action", roll_value: int
    ) -> None:
        pass

    def on_clash_lose(
        self, engine: "CombatEngine", self_action: "Action", opp_action: "Action", roll_value: int
    ) -> None:
        pass

    def on_unopposed_start(self, engine: "CombatEngine", action: "Action") -> None:
        pass

    def on_unbreakable_attack_start(self, engine: "CombatEngine", action: "Action") -> None:
        pass

    def on_aoe_start(self, engine: "CombatEngine", action: "Action") -> None:
        pass

    def on_aoe_end(
        self, engine: "CombatEngine", action: "Action", hit_targets: List["Character"]
    ) -> None:
        pass

    def modify_damage(
        self, engine: "CombatEngine", action: "Action", dmg_ctx: DamageContext
    ) -> None:
        pass

    def on_hit(
        self, engine: "CombatEngine", action: "Action", target: "Character", dmg_ctx: DamageContext
    ) -> None:
        pass

    def on_attack_end(self, engine: "CombatEngine", action: "Action") -> None:
        pass

    def __repr__(self) -> str:
        return f"<Skill {self.name}({self.format_formula()})>"


@dataclass
class Action:
    actor: "Character"
    skill: Optional[Skill]
    targets: List["Character"] = field(default_factory=list)
    consume_from_hand: bool = True
    is_pursuit: bool = False
    is_unbreakable_attack: bool = False
    clashable: bool = True
    cancelled: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


class Passive:
    """角色被动/特性基类"""

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self.owner: Optional["Character"] = None
        self.last_triggered_turn: int = -1
        self.last_triggered_round: int = -1
        self._listener_ids: List[int] = []

    def bind(
        self,
        engine: "CombatEngine",
        event_type: EventType,
        callback: Callable[[EventContext], None],
        priority: int = 0,
    ) -> None:
        lid = engine.events.subscribe(event_type, callback, priority=priority, owner=self)
        self._listener_ids.append(lid)

    def on_register(self, engine: "CombatEngine", owner: "Character") -> None:
        self.owner = owner

    def on_unregister(self, engine: "CombatEngine", owner: "Character") -> None:
        engine.events.unsubscribe_by_owner(self)
        self._listener_ids.clear()
        self.owner = None

    def is_active(self, engine: "CombatEngine", owner: "Character") -> bool:
        return False

    def is_triggered(self, engine: "CombatEngine", owner: "Character") -> bool:
        if type(self).is_active is not Passive.is_active:
            return self.is_active(engine, owner)
        return self.last_triggered_turn == engine.global_turn

    def notify_triggered(
        self,
        engine: "CombatEngine",
        detail: str = "",
        log: bool = True,
    ) -> None:
        self.last_triggered_turn = engine.global_turn
        self.last_triggered_round = engine.round_num
        engine.events.emit(
            EventType.PASSIVE_TRIGGERED,
            engine,
            passive=self,
            owner=self.owner,
            detail=detail,
        )
        if log:
            owner_prefix = f"[{self.owner.name}] " if self.owner else ""
            suffix = f"（{detail}）" if detail else ""
            engine.log(f"  {owner_prefix}当前触发被动：{self.name}{suffix}")


class StatusEffect:
    """状态效果基类"""

    def __init__(
        self,
        name: str,
        stacks: int = 1,
        category: str = "neutral",  # "buff" | "debuff" | "neutral"
        data: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.stacks = floor_val(stacks)
        self.category = category
        self.data: Dict[str, Any] = dict(data) if data else {}
        self.owner: Optional["Character"] = None
        self._listener_ids: List[int] = []

    @property
    def is_buff(self) -> bool:
        return self.category == "buff"

    @property
    def is_debuff(self) -> bool:
        return self.category == "debuff"

    def bind(
        self,
        engine: "CombatEngine",
        event_type: EventType,
        callback: Callable[[EventContext], None],
        priority: int = 0,
    ) -> None:
        lid = engine.events.subscribe(event_type, callback, priority=priority, owner=self)
        self._listener_ids.append(lid)

    def on_attach(self, engine: "CombatEngine", owner: "Character") -> None:
        self.owner = owner

    def on_reapply(self, engine: "CombatEngine", add_stacks: int, extra: Dict[str, Any]) -> None:
        self.stacks = floor_val(self.stacks + add_stacks)

    def on_detach(self, engine: "CombatEngine") -> None:
        engine.events.unsubscribe_by_owner(self)
        self._listener_ids.clear()
        self.owner = None


class Character:
    """角色实体"""

    def __init__(
        self,
        char_id: str,
        name: str,
        team: str,
        max_hp: int = 100,
        hp: Optional[int] = None,
        skill_template: Optional[List[Tuple[Skill, int]]] = None,
        passives: Optional[List[Passive]] = None,
        action_slots: int = 1,
        custom_state: Optional[Dict[str, Any]] = None,
        bgm_tracks: Optional[List[Any]] = None,
    ):
        self.char_id = char_id
        self.name = name
        self.team = team
        self.max_hp = floor_val(max_hp)
        self.hp = floor_val(hp) if hp is not None else self.max_hp
        self.alive: bool = self.hp > 0

        self.skill_template: List[Tuple[Skill, int]] = skill_template or []
        self.available_skills: List[Skill] = []

        self.passives: List[Passive] = list(passives) if passives else []
        self.statuses: Dict[str, StatusEffect] = {}
        self.action_slots: int = action_slots
        self.custom_state: Dict[str, Any] = dict(custom_state) if custom_state else {}
        self.bgm_tracks: List[Any] = list(bgm_tracks) if bgm_tracks else []

        self.ai_selector: Optional[Callable[..., Action]] = None

    def set_standard_skills(self, skill1: Skill, skill2: Skill, skill3: Skill) -> None:
        self.skill_template = [(skill3, 1), (skill2, 2), (skill1, 3)]

    def refill_skills(self) -> List[Skill]:
        new_skills: List[Skill] = []
        for proto, count in self.skill_template:
            for _ in range(count):
                new_skills.append(proto.clone())
        self.available_skills.extend(new_skills)
        return new_skills

    def get_usable_skills(self, engine: "CombatEngine") -> List[Skill]:
        return [s for s in self.available_skills if s.can_use(engine, self)]

    def get_status(self, name: str) -> Optional[StatusEffect]:
        return self.statuses.get(name)

    def get_status_stacks(self, name: str) -> int:
        st = self.statuses.get(name)
        return st.stacks if st else 0

    def get_buffs(self) -> List[StatusEffect]:
        return [s for s in self.statuses.values() if s.is_buff]

    def get_debuffs(self) -> List[StatusEffect]:
        return [s for s in self.statuses.values() if s.is_debuff]

    def get_triggered_passives(self, engine: "CombatEngine") -> List[Passive]:
        return [p for p in self.passives if p.is_triggered(engine, self)]

    def format_triggered_passives(self, engine: "CombatEngine") -> str:
        triggered = self.get_triggered_passives(engine)
        return "、".join(p.name for p in triggered)

    def __repr__(self) -> str:
        return f"<Char {self.name}[{self.team}] HP:{self.hp}/{self.max_hp}>"
