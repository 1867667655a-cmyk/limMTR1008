from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List


class EventType(Enum):
    # 游戏与轮次生命周期
    GAME_START = auto()
    GAME_END = auto()
    ROUND_START = auto()
    ROUND_END = auto()
    SKILL_DISTRIBUTE = auto()  # 技能分发/补充

    # 回合与选技阶段
    TURN_START = auto()
    TURN_END = auto()
    SELECTION_ORDER = auto()     # 决定选人顺序
    BEFORE_SELECT_ACTION = auto()
    AFTER_SELECT_ACTION = auto()
    RESOLVE_MATCHUPS = auto()    # 决定谁与谁拼点/拦截/重定向

    # 拼点与掷骰阶段
    CLASH_START = auto()
    CLASH_ROLL = auto()          # 每次对拼掷骰
    CLASH_TIE = auto()
    CLASH_CANCEL_99 = auto()
    CLASH_WIN = auto()
    CLASH_LOSE = auto()
    UNOPPOSED_ATTACK_START = auto()

    # 群攻阶段（非拼点群攻与竞争）
    AOE_CONTEST_START = auto()   # 多方非拼点群攻抢先掷骰开始
    AOE_CONTEST_ROLL = auto()    # 多方非拼点群攻各自掷骰结果
    AOE_CONTEST_WIN = auto()     # 点数最大者获得释放权
    AOE_CONTEST_LOSE = auto()    # 点数不足者群攻被取消

    AOE_START = auto()           # 群攻开始（自身掷骰前）
    AOE_TARGET_ROLL = auto()     # 其他玩家掷出对抗点数后（可修改 target_roll）
    AOE_SUPPRESS = auto()        # 玩家点数小于群攻，技能本回合被禁用
    AOE_END = auto()             # 群攻结算结束

    # 不可摧毁技能/变动值阶段
    BEFORE_UNBREAKABLE_RESOLVE = auto()  # 可用于强行摧毁不可摧毁技能、修改保底点数等
    UNBREAKABLE_ATTACK_START = auto()

    BEFORE_SKILL_ROLL = auto()   # 修改基础值、硬币数量、硬币面值、概率
    AFTER_SKILL_ROLL = auto()    # 重投、强制加减点数
    SKILL_DESTROYED = auto()

    # 伤害与状态阶段
    CALCULATE_DAMAGE = auto()    # 计算倍率、增伤、呼吸等
    BEFORE_TAKE_DAMAGE = auto()  # 护盾、减伤、免死等
    AFTER_TAKE_DAMAGE = auto()   # 受击后触发（如破裂、腐蚀）

    STATUS_APPLY = auto()
    STATUS_REMOVE = auto()
    TREMOR_BURST = auto()        # 震颤引爆

    CHARACTER_DEATH = auto()
    AUTO_ROLL_TOGGLED = auto()   # 自动投掷开关切换
    PASSIVE_TRIGGERED = auto()   # 被动触发

    # BGM 系统
    BGM_PRIORITY_EVAL = auto()
    BGM_PRIORITY_CHANGED = auto()
    BGM_CHANGED = auto()


@dataclass
class EventContext:
    event_type: EventType
    engine: Any
    data: Dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False


@dataclass
class _Listener:
    listener_id: int
    event_type: EventType
    callback: Callable[[EventContext], None]
    priority: int
    owner: Any = None


class EventBus:
    def __init__(self):
        self._listeners: Dict[EventType, List[_Listener]] = {et: [] for et in EventType}
        self._next_id: int = 1

    def subscribe(
        self,
        event_type: EventType,
        callback: Callable[[EventContext], None],
        priority: int = 0,
        owner: Any = None,
    ) -> int:
        lid = self._next_id
        self._next_id += 1
        listener = _Listener(lid, event_type, callback, priority, owner)
        lst = self._listeners[event_type]
        lst.append(listener)
        lst.sort(key=lambda x: x.priority)
        return lid

    def unsubscribe(self, listener_id: int) -> None:
        for lst in self._listeners.values():
            for i, item in enumerate(lst):
                if item.listener_id == listener_id:
                    lst.pop(i)
                    return

    def unsubscribe_by_owner(self, owner: Any) -> None:
        if owner is None:
            return
        for et in self._listeners:
            self._listeners[et] = [x for x in self._listeners[et] if x.owner is not owner]

    def emit(self, event_type: EventType, engine: Any, **kwargs) -> EventContext:
        ctx = EventContext(event_type=event_type, engine=engine, data=kwargs)
        for listener in list(self._listeners[event_type]):
            if ctx.cancelled:
                break
            listener.callback(ctx)
        return ctx
