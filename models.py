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
    # 可选动态条件/触发回调: fn(engine, skill, actor, triggered: bool)
    on_roll: Optional[Callable[..., None]] = None

    def clone(self) -> "Coin":
        return Coin(
            value=self.value,
            probability=self.probability,
            label=self.label,
            on_roll=self.on_roll,
        )


@dataclass
class DamageContext:
    attacker: Optional["Character"]
    target: "Character"
    raw_damage: int
    damage: int
    skill: Optional["Skill"] = None
    source_type: str = "skill"  # "skill" | "dot" | "pursuit" | "effect" | custom
    trigger_on_hit_statuses: bool = True  # False 用于防止 DoT 触发破裂/腐蚀死循环
    allow_breath: bool = True
    cancelled: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def add_flat(self, amount: int) -> None:
        self.damage = floor_val(self.damage + amount)

    def mul_percent(self, percent: int) -> None:
        """例如 percent=120 表示乘以 1.2 倍并向下取整"""
        self.damage = floor_mul(self.damage, percent, 100)

    def mul_ratio(self, numerator: int, denominator: int) -> None:
        self.damage = floor_mul(self.damage, numerator, denominator)


class Skill:
    """
    技能基类：既可以直接实例化使用，也可以在角色卡里继承并重写任意方法。
    """

    def __init__(
        self,
        skill_id: str,
        name: str,
        tier: Any = 1,  # 通常是 1 / 2 / 3，也兼容任意自定义标识（如 "EX", 4）
        base_power: int = 0,
        coins: Optional[List[Coin]] = None,
        target_type: str = "single_enemy",  # single_enemy | all_enemies | self | single_ally | custom
        clashable: bool = True,
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
        self.tags: Set[str] = set(tags) if tags else set()
        self.extra_data: Dict[str, Any] = dict(extra_data) if extra_data else {}

    def format_formula(self) -> str:
        if not self.coins:
            return f"{self.base_power}"
        coins_str = "".join(
            f"+{c.value}" if c.value >= 0 else f"{c.value}" for c in self.coins
        )
        return f"{self.base_power}{coins_str}"

    def clone(self) -> "Skill":
        # 如果角色卡继承了 Skill 并自定义了 __init__，优先尝试调用子类提供的 clone_custom 或回退
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
                tags=set(self.tags),
                extra_data=dict(self.extra_data),
            )
        obj = cls.__new__(cls)
        obj.__dict__.update(self.__dict__)
        obj.coins = [c.clone() for c in self.coins]
        obj.tags = set(self.tags)
        obj.extra_data = dict(self.extra_data)
        return obj

    # ---- 以下钩子均可在自定义 Skill 子类中按需重写 ----
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

    def on_unopposed_start(
        self, engine: "CombatEngine", action: "Action"
    ) -> None:
        pass

    def modify_damage(
        self, engine: "CombatEngine", action: "Action", dmg_ctx: DamageContext
    ) -> None:
        """用于技能自身的独立倍率、固定伤或特殊伤害公式"""
        pass

    def on_hit(
        self, engine: "CombatEngine", action: "Action", target: "Character", dmg_ctx: DamageContext
    ) -> None:
        """命中并造成伤害后触发（如施加烧伤、震颤引爆、触发追击等）"""
        pass

    def on_attack_end(
        self, engine: "CombatEngine", action: "Action"
    ) -> None:
        """攻击结算完全结束后触发（即使目标死亡或伤害为0）"""
        pass

    def __repr__(self) -> str:
        return f"<Skill {self.name}({self.format_formula()})>"


@dataclass
class Action:
    actor: "Character"
    skill: Optional[Skill]  # None 表示空过/无技能挨打
    targets: List["Character"] = field(default_factory=list)
    consume_from_hand: bool = True
    is_pursuit: bool = False  # 是否属于追击
    clashable: bool = True
    cancelled: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


class Passive:
    """
    角色被动/特性基类：
    1) 条件型被动可重写 is_active(engine, owner) -> bool（如血量低于10%常驻生效）
    2) 事件型被动在触发时调用 self.notify_triggered(engine) 即可统一显示与记录
    """

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
        """条件型被动可重写此方法，返回当前条件是否满足"""
        return False

    def is_triggered(self, engine: "CombatEngine", owner: "Character") -> bool:
        """
        判断当前画面是否应显示此被动处于触发状态：
        - 若重写了 is_active，则实时按条件判断（如回血超过10%后自动熄灭）
        - 否则按本回合是否调用过 notify_triggered 判断
        """
        if type(self).is_active is not Passive.is_active:
            return self.is_active(engine, owner)
        return self.last_triggered_turn == engine.global_turn

    def notify_triggered(
        self,
        engine: "CombatEngine",
        detail: str = "",
        log: bool = True,
    ) -> None:
        """标记被动已触发，并输出统一格式提示"""
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
    """状态效果基类：支持内置 7 种 DoT 以及角色卡任意自创 Buff/Debuff"""

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
    @property
    def is_buff(self) -> bool:
        return self.category == "buff"

    @property
    def is_debuff(self) -> bool:
        return self.category == "debuff"

class Character:
    """
    角色实体：所有属性、技能组成、被动、行动槽数量、选人策略均可自定义。
    """

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

        # 技能模板: [(Skill原型, 每轮分发数量), ...], 默认兼容 1个三技能 + 2个二技能 + 3个一技能
        self.skill_template: List[Tuple[Skill, int]] = skill_template or []
        self.available_skills: List[Skill] = []

        self.passives: List[Passive] = list(passives) if passives else []
        self.statuses: Dict[str, StatusEffect] = {}
        self.action_slots: int = action_slots
        self.custom_state: Dict[str, Any] = dict(custom_state) if custom_state else {}

        # 可由角色卡注入自定义 AI / 选择器函数:
        # fn(engine, character, slot_idx, previous_selections) -> Action
        self.ai_selector: Optional[Callable[..., Action]] = None
        self.bgm_tracks: List[Any] = list(bgm_tracks) if bgm_tracks else []

    def set_standard_skills(self, skill1: Skill, skill2: Skill, skill3: Skill) -> None:
        """快捷设置标准 3-2-1 技能组"""
        self.skill_template = [(skill3, 1), (skill2, 2), (skill1, 3)]

    def refill_skills(self) -> List[Skill]:
        """按模板生成新一轮技能"""
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

    def __repr__(self) -> str:
        return f"<Char {self.name}[{self.team}] HP:{self.hp}/{self.max_hp}>"
    def get_triggered_passives(self, engine: "CombatEngine") -> List[Passive]:
        return [p for p in self.passives if p.is_triggered(engine, self)]

    def format_triggered_passives(self, engine: "CombatEngine") -> str:
        triggered = self.get_triggered_passives(engine)
        return "、".join(p.name for p in triggered)
    def get_buffs(self) -> List[StatusEffect]:
        return [s for s in self.statuses.values() if s.is_buff]

    def get_debuffs(self) -> List[StatusEffect]:
        return [s for s in self.statuses.values() if s.is_debuff]
