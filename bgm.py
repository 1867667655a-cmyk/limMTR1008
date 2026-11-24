from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from .events import EventType

if TYPE_CHECKING:
    from .combat import CombatEngine
    from .models import Character


class AudioBackend:
    """
    可选真实音频后端接口（默认纯文字版使用 NullAudioBackend，无任何第三方依赖）。
    后续若接入 pygame 等音频库，只需继承并重写 play / stop 即可。
    """

    def play(self, track: "BGMTrack", start_sec: float, loop: bool = True) -> None:
        pass

    def stop(self, track: Optional["BGMTrack"] = None) -> None:
        pass


class NullAudioBackend(AudioBackend):
    """纯引擎/文字模式空后端"""
    pass


class BGMTrack:
    """
    角色 BGM 曲目对象：
    - condition: fn(engine, track) -> bool，决定何时满足播放条件；也可由技能调用 activate() 手动开启
    - priority_fn: fn(engine, track) -> 数值或可比较元组，用于覆盖默认“血量高优先”规则
    - tier_override: 绝对层优先级（默认0）；若角色卡希望无视“刚播放回合最高”规则，可设为更高/更低值
    """

    def __init__(
        self,
        track_id: str,
        title: str,
        owner: Optional["Character"] = None,
        file_path: Optional[str] = None,
        duration_sec: Optional[float] = None,
        loop: bool = True,
        condition: Optional[Callable[["CombatEngine", "BGMTrack"], bool]] = None,
        priority_fn: Optional[Callable[["CombatEngine", "BGMTrack"], Any]] = None,
        priority_bonus: int = 0,
        tier_override: int = 0,
        stop_on_owner_death: bool = True,
        extra_data: Optional[Dict[str, Any]] = None,
    ):
        self.track_id = track_id
        self.title = title
        self.owner = owner
        self.file_path = file_path
        self.duration_sec = duration_sec
        self.loop = loop  # 默认单曲循环
        self.condition = condition
        self.priority_fn = priority_fn
        self.priority_bonus = priority_bonus
        self.tier_override = tier_override
        self.stop_on_owner_death = stop_on_owner_death
        self.extra_data: Dict[str, Any] = dict(extra_data) if extra_data else {}

        # 运行时状态
        self.manual_active: bool = False
        self.was_eligible: bool = False
        self.debut_turn: int = -1       # 记录最近一次达成播放条件的回合号
        self.progress_sec: float = 0.0  # 被打断时记录的播放进度（秒）
        self.play_count: int = 0
        self.register_order: int = 0

    def is_eligible(self, engine: "CombatEngine") -> bool:
        if self.owner and self.stop_on_owner_death and not self.owner.alive:
            return False
        if self.condition is not None:
            return bool(self.condition(engine, self))
        return self.manual_active

    def activate(self, engine: "CombatEngine", force_debut: bool = True) -> None:
        """技能或被动主动开启本 BGM"""
        self.manual_active = True
        if force_debut:
            self.debut_turn = max(1, engine.global_turn)
            self.was_eligible = True
        engine.bgm.evaluate(reason=f"{self.title} 触发播放")

    def deactivate(self, engine: "CombatEngine") -> None:
        """技能或被动主动关闭本 BGM"""
        self.manual_active = False
        self.was_eligible = False
        engine.bgm.evaluate(reason=f"{self.title} 停止条件")

    def set_priority_bonus(
        self, bonus: int, engine: Optional["CombatEngine"] = None
    ) -> None:
        """战斗途中突然更改 BGM 优先级加值"""
        self.priority_bonus = bonus
        if engine:
            engine.events.emit(EventType.BGM_PRIORITY_CHANGED, engine, track=self)
            engine.bgm.evaluate(reason=f"{self.title} 优先级变更")

    def set_priority_fn(
        self,
        fn: Optional[Callable[["CombatEngine", "BGMTrack"], Any]],
        engine: Optional["CombatEngine"] = None,
    ) -> None:
        """战斗途中突然更换 BGM 优先级判断函数"""
        self.priority_fn = fn
        if engine:
            engine.events.emit(EventType.BGM_PRIORITY_CHANGED, engine, track=self)
            engine.bgm.evaluate(reason=f"{self.title} 优先级规则变更")

    def set_tier_override(
        self, tier: int, engine: Optional["CombatEngine"] = None
    ) -> None:
        """战斗途中更改绝对优先级层级（可凌驾于刚播放回合最高规则之上）"""
        self.tier_override = tier
        if engine:
            engine.events.emit(EventType.BGM_PRIORITY_CHANGED, engine, track=self)
            engine.bgm.evaluate(reason=f"{self.title} 绝对优先级变更")

    def get_base_priority_score(self, engine: "CombatEngine") -> Any:
        if self.priority_fn is not None:
            return self.priority_fn(engine, self)
        owner_hp = self.owner.hp if self.owner else 0
        return owner_hp + self.priority_bonus

    def compute_sort_key(self, engine: "CombatEngine") -> Tuple[Any, ...]:
        """
        默认排序键（越大越优先）：
          (tier_override, is_debut_turn, base_score, -register_order)
        """
        current_turn = max(1, engine.global_turn)
        is_debut = 1 if self.debut_turn == current_turn else 0
        base_score = self.get_base_priority_score(engine)
        return (self.tier_override, is_debut, base_score, -self.register_order)


class BGMManager:
    """BGM 调度与抢麦管理器"""

    def __init__(
        self,
        engine: "CombatEngine",
        audio_backend: Optional[AudioBackend] = None,
    ):
        self.engine = engine
        self.audio_backend = audio_backend or NullAudioBackend()
        self.tracks: List[BGMTrack] = []
        self.current_track: Optional[BGMTrack] = None
        self._play_started_at: Optional[float] = None
        self._next_order: int = 1

    def register_track(self, track: BGMTrack, owner: Optional["Character"] = None) -> None:
        if owner is not None:
            track.owner = owner
        track.register_order = self._next_order
        self._next_order += 1
        self.tracks.append(track)

    def _sync_current_progress(self) -> float:
        """计算并保存当前正在播放曲目的进度"""
        if self.current_track is None or self._play_started_at is None:
            return 0.0
        now = time.monotonic()
        elapsed = max(0.0, now - self._play_started_at)
        self._play_started_at = now
        new_prog = self.current_track.progress_sec + elapsed
        if self.current_track.duration_sec and self.current_track.duration_sec > 0 and self.current_track.loop:
            new_prog = new_prog % self.current_track.duration_sec
        self.current_track.progress_sec = new_prog
        return new_prog

    def advance_simulated_time(self, seconds: float) -> None:
        """纯快速推演时也可手动推进模拟播放秒数（非必需，真实时间已自动计时）"""
        if self.current_track is not None and seconds > 0:
            new_prog = self.current_track.progress_sec + seconds
            if self.current_track.duration_sec and self.current_track.duration_sec > 0 and self.current_track.loop:
                new_prog = new_prog % self.current_track.duration_sec
            self.current_track.progress_sec = new_prog
            self._play_started_at = time.monotonic()

    def evaluate(self, reason: str = "") -> Optional[BGMTrack]:
        """重新评估场上所有可播放 BGM 并执行抢麦/继承播放"""
        current_turn = max(1, self.engine.global_turn)
        eligible_tracks: List[BGMTrack] = []

        for tr in self.tracks:
            ok = tr.is_eligible(self.engine)
            if ok:
                # 刚达成条件（从不可播变为可播），记录为本回合新触发（刚播放那回合优先级最高）
                if not tr.was_eligible:
                    tr.debut_turn = current_turn
                tr.was_eligible = True
                eligible_tracks.append(tr)
            else:
                tr.was_eligible = False

        if not eligible_tracks:
            if self.current_track is not None:
                saved = self._sync_current_progress()
                old = self.current_track
                self.audio_backend.stop(old)
                self.current_track = None
                self._play_started_at = None
                self.engine.log(
                    f"  [BGM停止] 《{old.title}》停止播放（已记录进度: {saved:.2f}s）"
                )
                self.engine.events.emit(
                    EventType.BGM_CHANGED, self.engine, old_track=old, new_track=None
                )
            return None

        # 为每首候选曲目计算排序键，并允许外部角色卡通过事件干涉评分
        scored: List[Tuple[Any, BGMTrack]] = []
        for tr in eligible_tracks:
            sort_key = tr.compute_sort_key(self.engine)
            ctx = self.engine.events.emit(
                EventType.BGM_PRIORITY_EVAL,
                self.engine,
                track=tr,
                sort_key=sort_key,
            )
            scored.append((ctx.data["sort_key"], tr))

        scored.sort(key=lambda x: x[0], reverse=True)
        winner = scored[0][1]

        if winner is self.current_track:
            return self.current_track

        # 发生切换或抢麦！
        old_track = self.current_track
        if old_track is not None:
            saved_prog = self._sync_current_progress()
            self.audio_backend.stop(old_track)
            owner_old = old_track.owner.name if old_track.owner else "场地"
            owner_new = winner.owner.name if winner.owner else "场地"
            action_word = "继续" if winner.play_count > 0 else "开始"
            self.engine.log(
                f"  [BGM抢麦] {owner_new} 的《{winner.title}》打断了 {owner_old} 的《{old_track.title}》"
                f"（保存旧曲进度: {saved_prog:.2f}s | 新曲{action_word}播放，进度: {winner.progress_sec:.2f}s，单曲循环）"
            )
        else:
            owner_new = winner.owner.name if winner.owner else "场地"
            if winner.play_count > 0:
                self.engine.log(
                    f"  [BGM恢复] 重新播放 {owner_new} 的《{winner.title}》"
                    f"（继承进度: {winner.progress_sec:.2f}s | 单曲循环）"
                )
            else:
                self.engine.log(
                    f"  [BGM播放] 开始播放 {owner_new} 的《{winner.title}》（单曲循环 | 进度: 0.00s）"
                )

        self.current_track = winner
        self._play_started_at = time.monotonic()
        winner.play_count += 1
        self.audio_backend.play(winner, start_sec=winner.progress_sec, loop=winner.loop)
        self.engine.events.emit(
            EventType.BGM_CHANGED,
            self.engine,
            old_track=old_track,
            new_track=winner,
            reason=reason,
        )
        return winner
