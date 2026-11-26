from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union

from .events import EventType

if TYPE_CHECKING:
    from .combat import CombatEngine
    from .models import Character


class AudioBackend:
    """音频后端基类"""

    def play(self, track: "BGMTrack", start_sec: float, loop: bool = True) -> None:
        pass

    def stop(self, track: Optional["BGMTrack"] = None) -> None:
        pass


class NullAudioBackend(AudioBackend):
    """纯文字空后端"""
    pass


class PygameAudioBackend(AudioBackend):
    """
    内置真实音频后端：
    自动从 bgm/ 目录按 track.file_path / track.track_id / track.title 查找 .mp3/.ogg/.wav/.flac。
    支持打断保存秒数、继承进度恢复以及默认单曲循环。
    """

    AUDIO_EXTS = (".mp3", ".ogg", ".wav", ".flac")

    def __init__(self, bgm_dir: Optional[Union[str, Path]] = None):
        self.bgm_dir = Path(bgm_dir) if bgm_dir is not None else Path("bgm")
        self.enabled = False
        self._pygame = None
        self._warned_missing_pygame = False

        try:
            os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
            import pygame

            pygame.mixer.init()
            self._pygame = pygame
            self.enabled = True
        except Exception:
            self._pygame = None
            self.enabled = False

    def _resolve_audio_file(self, track: "BGMTrack") -> Optional[Path]:
        candidates: List[Path] = []
        if track.file_path:
            p = Path(track.file_path)
            candidates.append(p)
            candidates.append(self.bgm_dir / track.file_path)

        for name in (track.track_id, track.title):
            if name:
                for ext in self.AUDIO_EXTS:
                    candidates.append(self.bgm_dir / f"{name}{ext}")

        for c in candidates:
            if c.exists() and c.is_file():
                return c
        return None

    def play(self, track: "BGMTrack", start_sec: float, loop: bool = True) -> None:
        audio_path = self._resolve_audio_file(track)
        if audio_path is None:
            return

        if not self.enabled or self._pygame is None:
            if not self._warned_missing_pygame:
                self._warned_missing_pygame = True
                print("[提示] 检测到 BGM 音频文件，如需实际出声请先安装: pip install pygame")
            return

        try:
            # 若角色卡未填 duration_sec，尝试自动读取曲长以精确取模循环进度
            if track.duration_sec is None:
                try:
                    track.duration_sec = float(
                        self._pygame.mixer.Sound(str(audio_path)).get_length()
                    )
                except Exception:
                    pass

            music = self._pygame.mixer.music
            music.load(str(audio_path))
            loops = -1 if loop else 0
            pos = float(start_sec)
            if track.duration_sec and track.duration_sec > 0 and pos >= track.duration_sec:
                pos = pos % track.duration_sec

            try:
                music.play(loops=loops, start=pos)
            except Exception:
                # 部分格式/版本不支持从指定秒数起播时，退回从头循环保证稳定出声
                music.play(loops=loops)
        except Exception as e:
            print(f"  [音频播放失败] {audio_path.name}: {e}")

    def stop(self, track: Optional["BGMTrack"] = None) -> None:
        if not self.enabled or self._pygame is None:
            return
        try:
            self._pygame.mixer.music.stop()
            if hasattr(self._pygame.mixer.music, "unload"):
                self._pygame.mixer.music.unload()
        except Exception:
            pass


class BGMTrack:
    """角色 BGM 曲目对象（默认单曲循环）"""

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
        self.loop = loop
        self.condition = condition
        self.priority_fn = priority_fn
        self.priority_bonus = priority_bonus
        self.tier_override = tier_override
        self.stop_on_owner_death = stop_on_owner_death
        self.extra_data: Dict[str, Any] = dict(extra_data) if extra_data else {}

        self.manual_active: bool = False
        self.was_eligible: bool = False
        self.debut_turn: int = -1
        self.progress_sec: float = 0.0
        self.play_count: int = 0
        self.register_order: int = 0

    def is_eligible(self, engine: "CombatEngine") -> bool:
        if self.owner and self.stop_on_owner_death and not self.owner.alive:
            return False
        if self.condition is not None:
            return bool(self.condition(engine, self))
        return self.manual_active

    def activate(self, engine: "CombatEngine", force_debut: bool = True) -> None:
        self.manual_active = True
        if force_debut:
            self.debut_turn = max(1, engine.global_turn)
            self.was_eligible = True
        engine.bgm.evaluate(reason=f"{self.title} 触发播放")

    def deactivate(self, engine: "CombatEngine") -> None:
        self.manual_active = False
        self.was_eligible = False
        engine.bgm.evaluate(reason=f"{self.title} 停止条件")

    def set_priority_bonus(
        self, bonus: int, engine: Optional["CombatEngine"] = None
    ) -> None:
        self.priority_bonus = bonus
        if engine:
            engine.events.emit(EventType.BGM_PRIORITY_CHANGED, engine, track=self)
            engine.bgm.evaluate(reason=f"{self.title} 优先级变更")

    def set_priority_fn(
        self,
        fn: Optional[Callable[["CombatEngine", "BGMTrack"], Any]],
        engine: Optional["CombatEngine"] = None,
    ) -> None:
        self.priority_fn = fn
        if engine:
            engine.events.emit(EventType.BGM_PRIORITY_CHANGED, engine, track=self)
            engine.bgm.evaluate(reason=f"{self.title} 优先级规则变更")

    def set_tier_override(
        self, tier: int, engine: Optional["CombatEngine"] = None
    ) -> None:
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
        if self.current_track is None or self._play_started_at is None:
            return 0.0
        now = time.monotonic()
        elapsed = max(0.0, now - self._play_started_at)
        self._play_started_at = now
        new_prog = self.current_track.progress_sec + elapsed
        if (
            self.current_track.duration_sec
            and self.current_track.duration_sec > 0
            and self.current_track.loop
        ):
            new_prog = new_prog % self.current_track.duration_sec
        self.current_track.progress_sec = new_prog
        return new_prog

    def stop_all(self) -> None:
        if self.current_track is not None:
            self._sync_current_progress()
            old = self.current_track
            self.audio_backend.stop(old)
            self.current_track = None
            self._play_started_at = None
        else:
            self.audio_backend.stop(None)

    def evaluate(self, reason: str = "") -> Optional[BGMTrack]:
        current_turn = max(1, self.engine.global_turn)
        eligible_tracks: List[BGMTrack] = []

        for tr in self.tracks:
            ok = tr.is_eligible(self.engine)
            if ok:
                if not tr.was_eligible:
                    tr.debut_turn = current_turn
                tr.was_eligible = True
                eligible_tracks.append(tr)
            else:
                tr.was_eligible = False

        if not eligible_tracks:
            if self.current_track is not None:
                self._sync_current_progress()
                old = self.current_track
                self.audio_backend.stop(old)
                self.current_track = None
                self._play_started_at = None
                self.engine.events.emit(
                    EventType.BGM_CHANGED, self.engine, old_track=old, new_track=None
                )
            return None

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

        old_track = self.current_track
        if old_track is not None:
            self._sync_current_progress()
            self.audio_backend.stop(old_track)

        self.current_track = winner
        self._play_started_at = time.monotonic()
        winner.play_count += 1
        self.audio_backend.play(winner, start_sec=winner.progress_sec, loop=winner.loop)

        owner_new = winner.owner.name if winner.owner else "场地"
        self.engine.log(f"  [当前BGM] 《{winner.title}》({owner_new})")

        self.engine.events.emit(
            EventType.BGM_CHANGED,
            self.engine,
            old_track=old_track,
            new_track=winner,
            reason=reason,
        )
        return winner
