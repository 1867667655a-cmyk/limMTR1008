from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .combat import CombatEngine
    from .models import Action, Character, Coin, Skill


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def wait_coin_key() -> str:
    """
    阻塞等待玩家按键：
      - Space / Enter -> 返回 "reveal"
      - 'a' / 'A' / 't' / 'T' -> 返回 "toggle"
    """
    if not _is_tty():
        raw = input().strip().lower()
        if raw in ("a", "t", "auto"):
            return "toggle"
        return "reveal"

    if os.name == "nt":
        import msvcrt

        while True:
            ch = msvcrt.getwch()
            if ch == "\x03":  # Ctrl+C
                raise KeyboardInterrupt
            if ch in ("\x00", "\xe0"):
                msvcrt.getwch()  # 丢弃功能键第二字节
                continue
            if ch in (" ", "\r", "\n"):
                return "reveal"
            if ch.lower() in ("a", "t"):
                return "toggle"
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch == "\x1b":  # 丢弃方向键等转义序列
                    import select
                    while select.select([sys.stdin], [], [], 0.0)[0]:
                        sys.stdin.read(1)
                    continue
                if ch in (" ", "\r", "\n"):
                    return "reveal"
                if ch.lower() in ("a", "t"):
                    return "toggle"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def poll_toggle_during_delay(delay: float) -> bool:
    """
    在自动投掷展示间隔内非阻塞监听按键，若按下 A/T 则返回 True 表示请求切换开关。
    """
    if not _is_tty():
        if delay > 0:
            time.sleep(delay)
        return False

    end_time = time.monotonic() + max(0.0, delay)

    if os.name == "nt":
        import msvcrt

        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == "\x03":
                    raise KeyboardInterrupt
                if ch in ("\x00", "\xe0"):
                    msvcrt.getwch()
                elif ch.lower() in ("a", "t"):
                    return True
            if time.monotonic() >= end_time:
                return False
            time.sleep(0.02)
    else:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                remaining = max(0.0, end_time - time.monotonic())
                rlist, _, _ = select.select([sys.stdin], [], [], min(0.05, remaining))
                if rlist:
                    ch = sys.stdin.read(1)
                    if ch == "\x1b":
                        while select.select([sys.stdin], [], [], 0.0)[0]:
                            sys.stdin.read(1)
                    elif ch.lower() in ("a", "t"):
                        return True
                if time.monotonic() >= end_time:
                    return False
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


class InteractiveRollHandler:
    """挂载到 CombatEngine 上的交互式投掷处理器"""

    def __init__(self, auto_delay: float = 0.18):
        self.auto_delay = auto_delay

    def on_roll_begin(
        self,
        engine: "CombatEngine",
        actor: "Character",
        skill: "Skill",
        base_power: int,
        total_coins: int,
    ) -> None:
        mode_str = "开启" if engine.auto_roll else "关闭"
        if total_coins == 0:
            engine.log(f"  [投掷] {actor.name} [{skill.name}] (基础值: {base_power}, 无变动值)")
        else:
            engine.log(
                f"  [投掷] {actor.name} [{skill.name}] "
                f"(基础值: {base_power}, 共 {total_coins} 枚变动值 | 自动投掷: {mode_str})"
            )

    def before_coin_reveal(
        self,
        engine: "CombatEngine",
        actor: "Character",
        skill: "Skill",
        coin_idx: int,
        total_coins: int,
        coin: "Coin",
    ) -> None:
        # 自动投掷开启时：允许在短暂间隔内按 A 随时打断关闭
        if engine.auto_roll:
            if poll_toggle_during_delay(self.auto_delay):
                engine.toggle_auto_roll(False)
                print(f"\n    [热键切换] 自动投掷已关闭！", flush=True)

        # 如果处于关闭状态（或刚才被打断关闭），则阻塞等待空格/Enter
        if not engine.auto_roll:
            hint = "[Space/Enter]" if _is_tty() else "[Enter/空格+Enter]"
            print(
                f"    第 {coin_idx}/{total_coins} 枚 (概率{int(coin.probability*100)}%, 面值{coin.value:+d}) "
                f"-> 按 {hint} 揭晓，按 [A] 开启自动: ",
                end="",
                flush=True,
            )
            cmd = wait_coin_key()
            print()  # 换行
            if cmd == "toggle":
                engine.toggle_auto_roll(True)
                print("    [热键切换] 自动投掷已开启，剩余硬币自动计算！", flush=True)

    def after_coin_reveal(
        self,
        engine: "CombatEngine",
        actor: "Character",
        skill: "Skill",
        coin_idx: int,
        total_coins: int,
        coin: "Coin",
        triggered: bool,
        added_val: int,
        running_total: int,
    ) -> None:
        face = f"正面 ({added_val:+d})" if triggered else "反面 (+0)"
        engine.log(f"    硬币 {coin_idx}/{total_coins}: [{face}] -> 当前点数: {running_total}")

    def on_roll_end(
        self,
        engine: "CombatEngine",
        actor: "Character",
        skill: "Skill",
        final_total: int,
    ) -> None:
        engine.log(f"  [投掷结果] {actor.name} [{skill.name}] 最终点数: {final_total}")
