from __future__ import annotations

import builtins
import inspect
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

from engine import (
    Action,
    Character,
    Coin,
    CombatEngine,
    EventType,
    Skill,
    load_character_from_file,
    load_characters_from_dir,
)

try:
    from engine import NullAudioBackend
except ImportError:
    NullAudioBackend = None  # type: ignore[assignment,misc]


class BattleAborted(Exception):
    """用户中止战斗或关闭窗口"""


# ==============================================================================
# 界面配色与字体
# ==============================================================================
BG_DARK = "#101218"
BG_PANEL = "#181B24"
BG_CARD = "#222634"
BG_CARD_ALT = "#2A2F42"
BG_CARD_DEAD = "#17181F"
BG_INPUT = "#2E3448"
FG_TEXT = "#EDF0F5"
FG_MUTED = "#8E96AB"
ACCENT_GOLD = "#E8BA5A"
ACCENT_RED = "#E05A67"
ACCENT_GREEN = "#4CC082"
ACCENT_BLUE = "#5998F7"
ACCENT_PURPLE = "#B37FEB"
BORDER_COLOR = "#343B52"

FONT_NAME = "Microsoft YaHei UI"


# ==============================================================================
# 仅拦截战斗线程的标准输出（避免干扰主线程）
# ==============================================================================
class _ScopedStdout:
    def __init__(
        self,
        orig_stdout: Any,
        worker_ident_fn: Callable[[], Optional[int]],
        log_cb: Callable[[str], None],
        recent_lines: List[str],
    ):
        self.orig_stdout = orig_stdout
        self.worker_ident_fn = worker_ident_fn
        self.log_cb = log_cb
        self.recent_lines = recent_lines
        self._buf = ""
        self._lock = threading.RLock()

    def write(self, s: str) -> int:
        if not s:
            return 0
        if threading.get_ident() != self.worker_ident_fn():
            return self.orig_stdout.write(s) if self.orig_stdout else len(s)
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                clean = line.rstrip("\r")
                if clean.strip():
                    self.recent_lines.append(clean)
                    if len(self.recent_lines) > 25:
                        self.recent_lines.pop(0)
                    self.log_cb(clean)
        return len(s)

    def flush(self) -> None:
        if threading.get_ident() != self.worker_ident_fn():
            if self.orig_stdout:
                self.orig_stdout.flush()
            return
        with self._lock:
            if self._buf.strip():
                clean = self._buf.strip("\r\n")
                self.recent_lines.append(clean)
                if len(self.recent_lines) > 25:
                    self.recent_lines.pop(0)
                self.log_cb(clean)
                self._buf = ""


# ==============================================================================
# GUI 交互式硬币投掷处理器
# ==============================================================================
class GUIRollHandler:
    def __init__(self, app: "GameWindow", auto_delay: float = 0.16):
        self.app = app
        self.auto_delay = auto_delay

    def on_roll_begin(
        self,
        engine: CombatEngine,
        actor: Character,
        skill: Skill,
        base_power: int,
        total_coins: int,
    ) -> None:
        self.app.check_abort()
        coins_meta = [
            {
                "value": c.value,
                "probability": c.probability,
                "unbreakable": bool(c.unbreakable or skill.unbreakable),
            }
            for c in getattr(skill, "coins", [])
        ]
        self.app.post_event(
            "roll_begin",
            {
                "actor": actor.name,
                "team": actor.team,
                "skill": skill.name,
                "formula": skill.format_formula(),
                "base_power": base_power,
                "total_coins": total_coins,
                "coins_meta": coins_meta,
            },
        )
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
        engine: CombatEngine,
        actor: Character,
        skill: Skill,
        coin_idx: int,
        total_coins: int,
        coin: Coin,
    ) -> None:
        self.app.check_abort()
        if engine.auto_roll:
            end_t = time.monotonic() + max(0.0, self.auto_delay)
            while time.monotonic() < end_t:
                self.app.check_abort()
                if not engine.auto_roll:
                    break
                time.sleep(0.025)

        if not engine.auto_roll:
            self.app.wait_for_coin(
                {
                    "actor": actor.name,
                    "skill": skill.name,
                    "coin_idx": coin_idx,
                    "total_coins": total_coins,
                    "value": coin.value,
                    "probability": coin.probability,
                }
            )

    def after_coin_reveal(
        self,
        engine: CombatEngine,
        actor: Character,
        skill: Skill,
        coin_idx: int,
        total_coins: int,
        coin: Coin,
        triggered: bool,
        added_val: int,
        running_total: int,
    ) -> None:
        self.app.check_abort()
        self.app.post_event(
            "coin_reveal",
            {
                "coin_idx": coin_idx,
                "triggered": triggered,
                "added_val": added_val,
                "running_total": running_total,
            },
        )
        face = f"正面 ({added_val:+d})" if triggered else "反面 (+0)"
        engine.log(f"    硬币 {coin_idx}/{total_coins}: [{face}] -> 当前点数: {running_total}")

    def on_roll_end(
        self,
        engine: CombatEngine,
        actor: Character,
        skill: Skill,
        final_total: int,
    ) -> None:
        self.app.check_abort()
        self.app.post_event("roll_end", {"final_total": final_total})
        engine.log(f"  [投掷结果] {actor.name} [{skill.name}] 最终点数: {final_total}")
        if engine.auto_roll:
            time.sleep(0.08)


# ==============================================================================
# 主窗口应用
# ==============================================================================
class GameWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("limMTR 回合制拼点对战引擎")
        self.root.geometry("1320x880")
        self.root.minsize(1100, 760)
        self.root.configure(bg=BG_DARK)

        self.char_dir = Path(__file__).parent / "characters"
        self.bgm_dir = Path(__file__).parent / "bgm"

        # 战前配置状态
        self.available_files: List[Tuple[Path, str]] = []
        self.p1_paths: List[Path] = []
        self.p2_paths: List[Path] = []
        self.mode_var = tk.StringVar(value="2")
        self.auto_roll_var = tk.BooleanVar(value=False)
        self.enable_bgm_var = tk.BooleanVar(value=True)
        self.delay_var = tk.DoubleVar(value=0.16)

        # 运行时状态
        self.engine: Optional[CombatEngine] = None
        self.battle_thread: Optional[threading.Thread] = None
        self._worker_ident: Optional[int] = None
        self.ui_queue: queue.Queue[Tuple[str, Any]] = queue.Queue()
        self._abort_flag = False

        self._action_event = threading.Event()
        self._action_result: Optional[Action] = None
        self._pending_action_ctx: Optional[Dict[str, Any]] = None

        self._coin_event = threading.Event()
        self._waiting_coin = False

        self._input_event = threading.Event()
        self._input_result: str = "0"
        self._recent_stdout_lines: List[str] = []

        self._coin_labels: List[tk.Label] = []
        self._active_dialog: Optional[tk.Toplevel] = None

        self._init_styles()
        self.container = tk.Frame(self.root, bg=BG_DARK)
        self.container.pack(fill=tk.BOTH, expand=True)

        self.build_setup_page()

        self.root.bind("<space>", self._on_space_key)
        self.root.bind("<Return>", self._on_space_key)
        self.root.bind("<KeyPress-a>", self._on_a_key)
        self.root.bind("<KeyPress-A>", self._on_a_key)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.root.after(25, self._poll_ui_queue)

    def _init_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "HP.Horizontal.TProgressbar",
            troughcolor="#12141C",
            background=ACCENT_GREEN,
            bordercolor=BORDER_COLOR,
            lightcolor=ACCENT_GREEN,
            darkcolor=ACCENT_GREEN,
            thickness=11,
        )

    def _clear_container(self) -> None:
        for w in self.container.winfo_children():
            w.destroy()

    # ==========================================================================
    # 页面 1：阵容与规则配置
    # ==========================================================================
    def build_setup_page(self) -> None:
        self._clear_container()

        header = tk.Frame(self.container, bg=BG_PANEL, padx=22, pady=14)
        header.pack(fill=tk.X)
        tk.Label(
            header, text="limMTR · 战前阵容配置", bg=BG_PANEL, fg=ACCENT_GOLD,
            font=(FONT_NAME, 17, "bold")
        ).pack(side=tk.LEFT)
        tk.Label(
            header, text="双击角色可快速加入 P1，或使用按钮分配至 P1 / P2",
            bg=BG_PANEL, fg=FG_MUTED, font=(FONT_NAME, 10)
        ).pack(side=tk.LEFT, padx=18)

        body = tk.Frame(self.container, bg=BG_DARK, padx=20, pady=14)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)

        # P1 列
        p1_col = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.SOLID)
        p1_col.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Label(
            p1_col, text="P1 阵营（玩家默认队）", bg=BG_PANEL, fg=ACCENT_BLUE,
            font=(FONT_NAME, 12, "bold"), pady=10
        ).pack(fill=tk.X)
        self.p1_lb = tk.Listbox(
            p1_col, bg=BG_CARD, fg=FG_TEXT, selectbackground=ACCENT_BLUE,
            font=(FONT_NAME, 11), relief=tk.FLAT, highlightthickness=0
        )
        self.p1_lb.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        p1_ops = tk.Frame(p1_col, bg=BG_PANEL, pady=8)
        p1_ops.pack(fill=tk.X, padx=10)
        tk.Button(
            p1_ops, text="移除选中", command=lambda: self._remove_char("P1"),
            bg=BG_INPUT, fg=FG_TEXT, relief=tk.FLAT, padx=10, pady=4, font=(FONT_NAME, 9)
        ).pack(side=tk.LEFT)
        tk.Button(
            p1_ops, text="清空", command=lambda: self._clear_team("P1"),
            bg=BG_INPUT, fg=ACCENT_RED, relief=tk.FLAT, padx=10, pady=4, font=(FONT_NAME, 9)
        ).pack(side=tk.RIGHT)

        # 中间角色库
        lib_col = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.SOLID)
        lib_col.grid(row=0, column=1, sticky="nsew", padx=8)
        tk.Label(
            lib_col, text="角色卡库 (characters/)", bg=BG_PANEL, fg=ACCENT_GOLD,
            font=(FONT_NAME, 12, "bold"), pady=10
        ).pack(fill=tk.X)
        self.lib_lb = tk.Listbox(
            lib_col, bg=BG_CARD, fg=FG_TEXT, selectbackground=ACCENT_GOLD, selectforeground="#111",
            font=(FONT_NAME, 11), relief=tk.FLAT, highlightthickness=0
        )
        self.lib_lb.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        self.lib_lb.bind("<Double-Button-1>", lambda e: self._add_char("P1"))

        lib_ops1 = tk.Frame(lib_col, bg=BG_PANEL, pady=6)
        lib_ops1.pack(fill=tk.X, padx=10)
        tk.Button(
            lib_ops1, text="← 加入 P1", command=lambda: self._add_char("P1"),
            bg=ACCENT_BLUE, fg="#FFF", relief=tk.FLAT, padx=12, pady=6,
            font=(FONT_NAME, 10, "bold"), cursor="hand2"
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        tk.Button(
            lib_ops1, text="加入 P2 →", command=lambda: self._add_char("P2"),
            bg=ACCENT_RED, fg="#FFF", relief=tk.FLAT, padx=12, pady=6,
            font=(FONT_NAME, 10, "bold"), cursor="hand2"
        ).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(4, 0))

        lib_ops2 = tk.Frame(lib_col, bg=BG_PANEL, pady=6)
        lib_ops2.pack(fill=tk.X, padx=10, pady=(0, 4))
        tk.Button(
            lib_ops2, text="刷新目录", command=self._refresh_library,
            bg=BG_INPUT, fg=FG_TEXT, relief=tk.FLAT, padx=10, pady=4, font=(FONT_NAME, 9)
        ).pack(side=tk.LEFT)
        tk.Button(
            lib_ops2, text="浏览外部 .py...", command=self._browse_external,
            bg=BG_INPUT, fg=ACCENT_GOLD, relief=tk.FLAT, padx=10, pady=4, font=(FONT_NAME, 9)
        ).pack(side=tk.RIGHT)

        # P2 列
        p2_col = tk.Frame(body, bg=BG_PANEL, bd=1, relief=tk.SOLID)
        p2_col.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        tk.Label(
            p2_col, text="P2 阵营（敌方 / 对手）", bg=BG_PANEL, fg=ACCENT_RED,
            font=(FONT_NAME, 12, "bold"), pady=10
        ).pack(fill=tk.X)
        self.p2_lb = tk.Listbox(
            p2_col, bg=BG_CARD, fg=FG_TEXT, selectbackground=ACCENT_RED,
            font=(FONT_NAME, 11), relief=tk.FLAT, highlightthickness=0
        )
        self.p2_lb.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        p2_ops = tk.Frame(p2_col, bg=BG_PANEL, pady=8)
        p2_ops.pack(fill=tk.X, padx=10)
        tk.Button(
            p2_ops, text="移除选中", command=lambda: self._remove_char("P2"),
            bg=BG_INPUT, fg=FG_TEXT, relief=tk.FLAT, padx=10, pady=4, font=(FONT_NAME, 9)
        ).pack(side=tk.LEFT)
        tk.Button(
            p2_ops, text="清空", command=lambda: self._clear_team("P2"),
            bg=BG_INPUT, fg=ACCENT_RED, relief=tk.FLAT, padx=10, pady=4, font=(FONT_NAME, 9)
        ).pack(side=tk.RIGHT)

        # 底部控制栏
        footer = tk.Frame(self.container, bg=BG_PANEL, padx=22, pady=14)
        footer.pack(fill=tk.X)

        opts = tk.Frame(footer, bg=BG_PANEL)
        opts.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(opts, text="操控模式:", bg=BG_PANEL, fg=FG_TEXT, font=(FONT_NAME, 10, "bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        for col, (txt, val) in enumerate(
            [
                ("1. 全员手动选技", "1"),
                ("2. P1手动 + P2 AI", "2"),
                ("3. 全员AI自动推演", "3"),
            ],
            start=1,
        ):
            tk.Radiobutton(
                opts, text=txt, variable=self.mode_var, value=val,
                bg=BG_PANEL, fg=FG_TEXT, selectcolor=BG_CARD, activebackground=BG_PANEL,
                activeforeground=ACCENT_GOLD, font=(FONT_NAME, 10)
            ).grid(row=0, column=col, padx=8, sticky="w")

        tk.Checkbutton(
            opts, text="初始开启【自动投掷】", variable=self.auto_roll_var,
            bg=BG_PANEL, fg=ACCENT_GOLD, selectcolor=BG_CARD, activebackground=BG_PANEL,
            activeforeground=ACCENT_GOLD, font=(FONT_NAME, 10)
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        tk.Checkbutton(
            opts, text="启用 BGM 音效", variable=self.enable_bgm_var,
            bg=BG_PANEL, fg=ACCENT_PURPLE, selectcolor=BG_CARD, activebackground=BG_PANEL,
            activeforeground=ACCENT_PURPLE, font=(FONT_NAME, 10)
        ).grid(row=1, column=2, sticky="w", pady=(8, 0), padx=8)

        delay_box = tk.Frame(opts, bg=BG_PANEL)
        delay_box.grid(row=1, column=3, sticky="w", pady=(8, 0))
        tk.Label(delay_box, text="投掷延迟:", bg=BG_PANEL, fg=FG_MUTED, font=(FONT_NAME, 9)).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        tk.Scale(
            delay_box, from_=0.02, to=0.50, resolution=0.02, orient=tk.HORIZONTAL,
            variable=self.delay_var, bg=BG_PANEL, fg=FG_TEXT, troughcolor=BG_CARD,
            highlightthickness=0, length=120
        ).pack(side=tk.LEFT)

        tk.Button(
            footer, text="开始战斗 ▶", command=self._on_start_clicked,
            bg=ACCENT_GOLD, fg="#111", relief=tk.FLAT, padx=30, pady=12,
            font=(FONT_NAME, 13, "bold"), cursor="hand2"
        ).pack(side=tk.RIGHT)

        self._refresh_library()
        self._refresh_rosters()

    def _refresh_library(self) -> None:
        self.available_files = load_characters_from_dir(self.char_dir)
        self.lib_lb.delete(0, tk.END)
        for path, _ in self.available_files:
            self.lib_lb.insert(tk.END, path.name)
        if self.available_files:
            self.lib_lb.selection_set(0)

    def _refresh_rosters(self) -> None:
        self.p1_lb.delete(0, tk.END)
        for i, p in enumerate(self.p1_paths, 1):
            self.p1_lb.insert(tk.END, f"{i}. {p.name}")
        self.p2_lb.delete(0, tk.END)
        for i, p in enumerate(self.p2_paths, 1):
            self.p2_lb.insert(tk.END, f"{i}. {p.name}")

    def _add_char(self, team: str) -> None:
        sel = self.lib_lb.curselection()
        if not sel:
            return
        path = self.available_files[sel[0]][0]
        if team == "P1":
            self.p1_paths.append(path)
        else:
            self.p2_paths.append(path)
        self._refresh_rosters()

    def _browse_external(self) -> None:
        fpath = filedialog.askopenfilename(
            title="选择角色卡 (.py)",
            filetypes=[("Python 文件", "*.py"), ("所有文件", "*.*")],
        )
        if not fpath:
            return
        p = Path(fpath)
        if (p, p.stem) not in self.available_files:
            self.available_files.append((p, p.stem))
            self.lib_lb.insert(tk.END, f"{p.name} (外部)")
            self.lib_lb.selection_clear(0, tk.END)
            self.lib_lb.selection_set(tk.END)

    def _remove_char(self, team: str) -> None:
        lb = self.p1_lb if team == "P1" else self.p2_lb
        lst = self.p1_paths if team == "P1" else self.p2_paths
        sel = lb.curselection()
        if sel:
            lst.pop(sel[0])
            self._refresh_rosters()

    def _clear_team(self, team: str) -> None:
        if team == "P1":
            self.p1_paths.clear()
        else:
            self.p2_paths.clear()
        self._refresh_rosters()

    def _on_start_clicked(self) -> None:
        if not self.p1_paths or not self.p2_paths:
            messagebox.showwarning("阵容不完整", "请至少为 P1 和 P2 各加入 1 名角色！")
            return

        roster: List[Character] = []
        name_counts: Dict[str, int] = {}
        try:
            for team, paths in (("P1", self.p1_paths), ("P2", self.p2_paths)):
                for idx, path in enumerate(paths, start=1):
                    char = load_character_from_file(path, team=team, search_dir=self.char_dir)
                    char.char_id = f"{char.char_id}_{team}_{idx}"
                    name_counts[char.name] = name_counts.get(char.name, 0) + 1
                    roster.append(char)

            seen_counter: Dict[str, int] = {}
            for c in roster:
                if name_counts[c.name] > 1:
                    seen_counter[c.name] = seen_counter.get(c.name, 0) + 1
                    c.name = f"{c.name}#{seen_counter[c.name]}({c.team})"

            self._start_battle(roster)
        except Exception as exc:
            messagebox.showerror("启动战斗失败", f"{exc}\n\n{traceback.format_exc()}")

    # ==========================================================================
    # 页面 2：战斗主界面
    # ==========================================================================
    def build_battle_page(self) -> None:
        self._clear_container()

        # 1) 顶部栏
        top_bar = tk.Frame(self.container, bg=BG_PANEL, padx=16, pady=9)
        top_bar.pack(fill=tk.X)

        self.round_lbl = tk.Label(
            top_bar, text="正在初始化战斗...", bg=BG_PANEL, fg=ACCENT_GOLD,
            font=(FONT_NAME, 13, "bold")
        )
        self.round_lbl.pack(side=tk.LEFT)

        self.phase_lbl = tk.Label(
            top_bar, text="[状态: 准备中]", bg=BG_CARD, fg=ACCENT_BLUE,
            padx=10, pady=2, font=(FONT_NAME, 9, "bold")
        )
        self.phase_lbl.pack(side=tk.LEFT, padx=14)

        self.bgm_lbl = tk.Label(
            top_bar, text="♪ BGM: 无", bg=BG_PANEL, fg=ACCENT_PURPLE,
            font=(FONT_NAME, 10, "bold")
        )
        self.bgm_lbl.pack(side=tk.LEFT, padx=8)

        tk.Button(
            top_bar, text="返回配置", command=self._abort_and_return_setup,
            bg=BG_INPUT, fg=ACCENT_RED, relief=tk.FLAT, padx=12, pady=4,
            font=(FONT_NAME, 9, "bold"), cursor="hand2"
        ).pack(side=tk.RIGHT)

        self.auto_btn = tk.Button(
            top_bar, text="自动投掷: 关闭 [A]", command=self._on_toggle_auto,
            bg=BG_INPUT, fg=FG_TEXT, relief=tk.FLAT, padx=12, pady=4,
            font=(FONT_NAME, 9, "bold"), cursor="hand2"
        )
        self.auto_btn.pack(side=tk.RIGHT, padx=(0, 10))

        # 2) 主体纵向分割
        paned = tk.PanedWindow(
            self.container, orient=tk.VERTICAL, bg=BG_DARK, sashwidth=5, relief=tk.FLAT
        )
        paned.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        # ---- 区块 A：战场（左 P1 | 中 投掷舞台 | 右 P2） ----
        field_frame = tk.Frame(paned, bg=BG_DARK)
        paned.add(field_frame, minsize=250)
        field_frame.columnconfigure(0, weight=5)
        field_frame.columnconfigure(1, weight=4)
        field_frame.columnconfigure(2, weight=5)
        field_frame.rowconfigure(0, weight=1)

        self.p1_inner = self._create_scrollable_panel(field_frame, "P1 队伍", ACCENT_BLUE)
        self.p1_inner.outer_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        # 中间投掷舞台
        roll_card = tk.Frame(field_frame, bg=BG_PANEL, bd=1, relief=tk.SOLID, padx=14, pady=12)
        roll_card.grid(row=0, column=1, sticky="nsew", padx=4)

        tk.Label(
            roll_card, text="拼点与投掷舞台", bg=BG_PANEL, fg=FG_MUTED,
            font=(FONT_NAME, 9, "bold")
        ).pack(anchor="w")

        roll_top = tk.Frame(roll_card, bg=BG_PANEL, pady=6)
        roll_top.pack(fill=tk.X)
        self.roll_actor_lbl = tk.Label(
            roll_top, text="等待技能掷骰...", bg=BG_PANEL, fg=ACCENT_GOLD,
            font=(FONT_NAME, 11, "bold"), anchor="w", wraplength=260, justify=tk.LEFT
        )
        self.roll_actor_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.roll_score_lbl = tk.Label(
            roll_top, text="点数: -", bg=BG_PANEL, fg=FG_TEXT,
            font=(FONT_NAME, 13, "bold")
        )
        self.roll_score_lbl.pack(side=tk.RIGHT)

        self.coins_wrap = tk.Frame(roll_card, bg=BG_CARD, padx=10, pady=14)
        self.coins_wrap.pack(fill=tk.BOTH, expand=True, pady=(4, 10))

        self.reveal_btn = tk.Button(
            roll_card, text="揭晓下一枚硬币 [Space / Enter]",
            command=self._on_reveal_clicked,
            state=tk.DISABLED, bg=BG_INPUT, fg=FG_MUTED, relief=tk.FLAT,
            pady=8, font=(FONT_NAME, 10, "bold"), cursor="hand2"
        )
        self.reveal_btn.pack(fill=tk.X)

        self.p2_inner = self._create_scrollable_panel(field_frame, "P2 队伍", ACCENT_RED)
        self.p2_inner.outer_frame.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        # ---- 区块 B：独立宽幅选技操作台 ----
        dock_frame = tk.Frame(paned, bg=BG_PANEL, bd=1, relief=tk.SOLID, padx=14, pady=10)
        paned.add(dock_frame, minsize=210)

        dock_hdr = tk.Frame(dock_frame, bg=BG_PANEL)
        dock_hdr.pack(fill=tk.X)
        tk.Label(
            dock_hdr, text="【选技操作台】", bg=BG_PANEL, fg=ACCENT_GOLD,
            font=(FONT_NAME, 11, "bold")
        ).pack(side=tk.LEFT)
        self.action_title_lbl = tk.Label(
            dock_hdr, text="等待角色行动...", bg=BG_PANEL, fg=ACCENT_BLUE,
            font=(FONT_NAME, 11, "bold")
        )
        self.action_title_lbl.pack(side=tk.LEFT, padx=10)

        self.action_info_lbl = tk.Label(
            dock_frame, text="当轮到己方手动角色选技时，这里会显示可点击的技能卡片。",
            bg=BG_PANEL, fg=FG_MUTED, font=(FONT_NAME, 9), anchor="w", justify=tk.LEFT
        )
        self.action_info_lbl.pack(fill=tk.X, pady=(3, 8))

        dock_canvas_wrap = tk.Frame(dock_frame, bg=BG_PANEL)
        dock_canvas_wrap.pack(fill=tk.BOTH, expand=True)
        self.dock_canvas = tk.Canvas(dock_canvas_wrap, bg=BG_PANEL, highlightthickness=0)
        dock_scroll = tk.Scrollbar(dock_canvas_wrap, orient=tk.VERTICAL, command=self.dock_canvas.yview)
        self.dock_inner = tk.Frame(self.dock_canvas, bg=BG_PANEL)
        self.dock_inner.bind(
            "<Configure>",
            lambda e: self.dock_canvas.configure(scrollregion=self.dock_canvas.bbox("all")),
        )
        self._dock_win_id = self.dock_canvas.create_window((0, 0), window=self.dock_inner, anchor="nw")
        self.dock_canvas.bind(
            "<Configure>",
            lambda e: self.dock_canvas.itemconfigure(self._dock_win_id, width=e.width),
        )
        self.dock_canvas.configure(yscrollcommand=dock_scroll.set)
        self.dock_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        dock_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # ---- 区块 C：战斗日志 ----
        log_frame = tk.Frame(paned, bg=BG_PANEL, bd=1, relief=tk.SOLID, padx=10, pady=6)
        paned.add(log_frame, minsize=160)
        tk.Label(
            log_frame, text="战斗日志", bg=BG_PANEL, fg=FG_MUTED,
            font=(FONT_NAME, 9, "bold"), anchor="w"
        ).pack(fill=tk.X)

        log_box = tk.Frame(log_frame, bg="#0D0E13")
        log_box.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.log_text = tk.Text(
            log_box, bg="#0D0E13", fg=FG_TEXT, font=("Consolas", 10),
            wrap=tk.WORD, relief=tk.FLAT, state=tk.DISABLED, padx=8, pady=6
        )
        log_scroll = tk.Scrollbar(log_box, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.log_text.tag_configure("round", foreground=ACCENT_GOLD, font=(FONT_NAME, 10, "bold"))
        self.log_text.tag_configure("clash", foreground=ACCENT_BLUE)
        self.log_text.tag_configure("damage", foreground=ACCENT_RED)
        self.log_text.tag_configure("heal", foreground=ACCENT_GREEN)
        self.log_text.tag_configure("special", foreground=ACCENT_GOLD)
        self.log_text.tag_configure("domain", foreground=ACCENT_PURPLE)
        self.log_text.tag_configure("system", foreground="#6ED6D6")
        self.log_text.tag_configure("muted", foreground=FG_MUTED)

    def _create_scrollable_panel(self, parent: tk.Widget, title: str, color: str) -> Any:
        frame = tk.Frame(parent, bg=BG_PANEL, bd=1, relief=tk.SOLID)
        tk.Label(
            frame, text=title, bg=BG_PANEL, fg=color, font=(FONT_NAME, 11, "bold"), pady=7
        ).pack(fill=tk.X)

        canvas = tk.Canvas(frame, bg=BG_PANEL, highlightthickness=0)
        scroll = tk.Scrollbar(frame, orient=tk.VERTICAL, command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG_PANEL)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_id, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        inner.outer_frame = frame  # type: ignore[attr-defined]
        return inner

    # ==========================================================================
    # 启动后台战斗
    # ==========================================================================
    def _start_battle(self, roster: List[Character]) -> None:
        self._abort_flag = False
        self._waiting_coin = False
        self._recent_stdout_lines.clear()
        while not self.ui_queue.empty():
            try:
                self.ui_queue.get_nowait()
            except queue.Empty:
                break

        self.build_battle_page()

        roll_handler = GUIRollHandler(self, auto_delay=float(self.delay_var.get()))
        kwargs: Dict[str, Any] = {
            "characters": roster,
            "auto_roll": bool(self.auto_roll_var.get()),
            "roll_handler": roll_handler,
            "logger": lambda msg: self.post_event("log", str(msg)),
            "show_turn_board": False,
            "bgm_dir": self.bgm_dir,
        }
        if not self.enable_bgm_var.get() and NullAudioBackend is not None:
            kwargs["audio_backend"] = NullAudioBackend()

        sig = inspect.signature(CombatEngine.__init__)
        supported_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        engine = CombatEngine(**supported_kwargs)

        mode = self.mode_var.get()
        if mode == "1":
            engine.manual_selector = self._gui_manual_selector
        elif mode == "2":
            def p1_manual_p2_ai(
                eng: CombatEngine, char: Character, slot_idx: int, prev: List[Action]
            ) -> Action:
                if char.team == "P1":
                    return self._gui_manual_selector(eng, char, slot_idx, prev)
                return eng._default_ai_select(char, slot_idx, prev)

            engine.manual_selector = p1_manual_p2_ai
        else:
            engine.manual_selector = None

        event_names = (
            "GAME_START",
            "ROUND_START",
            "TURN_START",
            "TURN_END",
            "AFTER_SELECT_ACTION",
            "AFTER_TAKE_DAMAGE",
            "STATUS_APPLY",
            "STATUS_REMOVE",
            "CHARACTER_DEATH",
            "BGM_CHANGED",
            "AUTO_ROLL_TOGGLED",
            "PASSIVE_TRIGGERED",
        )
        for ename in event_names:
            et = getattr(EventType, ename, None)
            if et is not None:
                engine.events.subscribe(et, lambda ctx: self.post_event("refresh", None), priority=9999)

        self.engine = engine
        self._update_auto_btn(engine.auto_roll)
        self._refresh_board()

        def _worker() -> None:
            self._worker_ident = threading.get_ident()
            orig_stdout = sys.stdout
            orig_input = builtins.input
            interceptor = _ScopedStdout(
                orig_stdout=orig_stdout,
                worker_ident_fn=lambda: self._worker_ident,
                log_cb=lambda m: self.post_event("log", m),
                recent_lines=self._recent_stdout_lines,
            )
            sys.stdout = interceptor  # type: ignore[assignment]
            builtins.input = self._gui_input_bridge
            try:
                self.post_event(
                    "log",
                    f"[系统] 战斗启动（模式 {mode}，队伍人数 P1:{len(self.p1_paths)} vs P2:{len(self.p2_paths)}）",
                )
                winner = engine.run_game()
                self.post_event("game_end", winner)
            except BattleAborted:
                pass
            except Exception as exc:
                err_tb = traceback.format_exc()
                self.post_event("log", f"[引擎异常] {exc}\n{err_tb}")
                self.post_event("error", f"{exc}\n\n{err_tb}")
            finally:
                try:
                    interceptor.flush()
                except Exception:
                    pass
                sys.stdout = orig_stdout
                builtins.input = orig_input
                self._worker_ident = None
                try:
                    engine.bgm.stop_all()
                except Exception:
                    pass

        self.battle_thread = threading.Thread(target=_worker, daemon=True)
        self.battle_thread.start()

    # ==========================================================================
    # 线程通信
    # ==========================================================================
    def post_event(self, kind: str, payload: Any) -> None:
        self.ui_queue.put((kind, payload))

    def check_abort(self) -> None:
        if self._abort_flag:
            raise BattleAborted()

    def _gui_manual_selector(
        self,
        engine: CombatEngine,
        char: Character,
        slot_idx: int,
        previous_selections: List[Action],
    ) -> Action:
        self.check_abort()
        usable = [
            sk
            for sk in char.get_usable_skills(engine)
            if sk.is_aoe or sk.clash_aoe or engine.get_selectable_targets(char, sk, for_ai=False)
        ]
        if not usable:
            self.post_event("log", f"[选技] {char.name} 无可用技能（本回合只能挨打）")
            return Action(actor=char, skill=None, targets=[], consume_from_hand=False)

        self._action_result = None
        self._action_event.clear()

        prev_summary: List[str] = []
        for act in previous_selections:
            s_name = f"{act.skill.name}({act.skill.format_formula()})" if act.skill else "无技能"
            tgts = ",".join(
                f"{t.name}[队友]" if (t.team == act.actor.team and t is not act.actor) else t.name
                for t in act.targets
            ) or "-"
            pct = engine.format_hp_percent(act.actor) if hasattr(engine, "format_hp_percent") else ""
            itag = "[已被拦截]" if act.extra.get("intercepted_by") else ""
            prev_summary.append(f"[{act.actor.team}] {act.actor.name}({pct}): {s_name} → {tgts}{itag}")

        self.post_event(
            "request_action",
            {
                "char": char,
                "slot_idx": slot_idx,
                "usable": usable,
                "prev_summary": prev_summary,
                "prev_actions": list(previous_selections),
            },
        )
        self._action_event.wait()
        self.check_abort()
        assert self._action_result is not None
        return self._action_result

    def wait_for_coin(self, info: Dict[str, Any]) -> None:
        self.check_abort()
        self._coin_event.clear()
        self.post_event("wait_coin", info)
        self._coin_event.wait()
        self.check_abort()

    def _gui_input_bridge(self, prompt: str = "") -> str:
        self.check_abort()
        recent = list(self._recent_stdout_lines[-12:])
        self._input_result = "0"
        self._input_event.clear()
        self.post_event("request_input", {"prompt": prompt, "recent": recent})
        self._input_event.wait()
        self.check_abort()
        return self._input_result

    # ==========================================================================
    # UI 队列轮询与刷新（主线程）
    # ==========================================================================
    def _poll_ui_queue(self) -> None:
        try:
            processed = 0
            need_refresh = False
            while processed < 80:
                try:
                    kind, payload = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                processed += 1

                if kind == "log":
                    self._append_log(payload)
                elif kind == "refresh":
                    need_refresh = True
                elif kind == "roll_begin":
                    self._render_roll_begin(payload)
                    need_refresh = True
                elif kind == "wait_coin":
                    self._render_wait_coin(payload)
                elif kind == "coin_reveal":
                    self._render_coin_reveal(payload)
                elif kind == "roll_end":
                    self._render_roll_end(payload)
                    need_refresh = True
                elif kind == "request_action":
                    need_refresh = True
                    self._render_action_dock(payload)
                elif kind == "request_input":
                    self._open_prompt_dialog(payload)
                elif kind == "game_end":
                    need_refresh = True
                    self._on_game_end(payload)
                elif kind == "error":
                    messagebox.showerror("战斗异常", payload)

            if need_refresh:
                self._refresh_board()
        except Exception as exc:
            err = traceback.format_exc()
            print(err, file=sys.__stderr__)
            try:
                self._append_log(f"[UI异常] {exc}\n{err}")
            except Exception:
                pass
        finally:
            self.root.after(25, self._poll_ui_queue)

    def _append_log(self, msg: str) -> None:
        if not hasattr(self, "log_text"):
            return
        tag = None
        if "================" in msg or "--- 第" in msg or "=== 战斗" in msg:
            tag = "round"
        elif "[系统]" in msg:
            tag = "system"
        elif "[★" in msg or "[空间斩" in msg or "[也就是说" in msg or "[好不容易" in msg:
            tag = "special"
        elif "[伏魔御厨子]" in msg or "[领域" in msg or "[术式" in msg or "[当前BGM]" in msg:
            tag = "domain"
        elif "[伤害]" in msg or "[阵亡]" in msg or "[烧伤]" in msg or "[流血]" in msg or "[破裂]" in msg or "[腐蚀]" in msg:
            tag = "damage"
        elif "回复" in msg or "[反转术式" in msg:
            tag = "heal"
        elif "[拼点" in msg or "[拦截拼点" in msg or "[迎击" in msg or "[群攻" in msg or "[追击]" in msg:
            tag = "clash"
        elif msg.startswith("    "):
            tag = "muted"

        self.log_text.configure(state=tk.NORMAL)
        if tag:
            self.log_text.insert(tk.END, msg + "\n", tag)
        else:
            self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _refresh_board(self) -> None:
        eng = self.engine
        if not eng or not hasattr(self, "round_lbl"):
            return

        if eng.round_num <= 0:
            self.round_lbl.config(text="战斗初始化中...")
        else:
            self.round_lbl.config(
                text=f"第 {eng.round_num} 轮 · 第 {max(1, eng.turn_in_round)} 回合"
            )

        tr = getattr(eng.bgm, "current_track", None)
        if tr:
            owner_name = tr.owner.name if tr.owner else "场地"
            self.bgm_lbl.config(text=f"♪ BGM: 《{tr.title}》 ({owner_name})")
        else:
            self.bgm_lbl.config(text="♪ BGM: 无")

        self._update_auto_btn(eng.auto_roll)

        chars = list(eng.characters)
        p1_chars = [c for c in chars if c.team == "P1"]
        p2_chars = [c for c in chars if c.team != "P1"]
        self._render_team_cards(self.p1_inner, p1_chars, ACCENT_BLUE)
        self._render_team_cards(self.p2_inner, p2_chars, ACCENT_RED)

    def _render_team_cards(self, container: tk.Frame, chars: List[Character], team_color: str) -> None:
        for w in container.winfo_children():
            w.destroy()

        eng = self.engine
        if not eng:
            return

        for c in chars:
            bg = BG_CARD if c.alive else BG_CARD_DEAD
            card = tk.Frame(container, bg=bg, bd=1, relief=tk.SOLID, padx=10, pady=8)
            card.pack(fill=tk.X, pady=4, padx=2)

            hdr = tk.Frame(card, bg=bg)
            hdr.pack(fill=tk.X)
            name_txt = c.name if c.alive else f"{c.name} [阵亡]"
            name_fg = team_color if c.alive else FG_MUTED
            tk.Label(
                hdr, text=name_txt, bg=bg, fg=name_fg, font=(FONT_NAME, 11, "bold")
            ).pack(side=tk.LEFT)
            tk.Label(
                hdr, text=f"余牌: {len(c.available_skills)}", bg=bg, fg=FG_MUTED, font=(FONT_NAME, 9)
            ).pack(side=tk.RIGHT)

            hp_pct = max(0, min(100, int((c.hp / max(1, c.max_hp)) * 100)))
            hp_pct_str = eng.format_hp_percent(c) if hasattr(eng, "format_hp_percent") else f"{hp_pct}%"
            hp_row = tk.Frame(card, bg=bg)
            hp_row.pack(fill=tk.X, pady=(4, 2))
            pb = ttk.Progressbar(hp_row, style="HP.Horizontal.TProgressbar", maximum=100, value=hp_pct)
            pb.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
            tk.Label(
                hp_row, text=f"{c.hp}/{c.max_hp} ({hp_pct_str})",
                bg=bg, fg=FG_TEXT, font=("Consolas", 9, "bold")
            ).pack(side=tk.RIGHT)

            # 兼容宿傩资源与双嘴/四手部位显示
            for p in getattr(c, "passives", []):
                res_chips: List[Tuple[str, str]] = []
                if hasattr(p, "cursed_energy"):
                    res_chips.append((f"咒力:{getattr(p, 'cursed_energy')}", ACCENT_BLUE))
                if hasattr(p, "excitement"):
                    res_chips.append((f"兴致:{getattr(p, 'excitement')}", ACCENT_GOLD))
                if getattr(p, "domain_turns_left", 0) > 0 and not getattr(p, "burnout_active", False):
                    res_chips.append((f"领域:{getattr(p, 'domain_turns_left')}回合", ACCENT_PURPLE))
                if getattr(p, "burnout_active", False):
                    res_chips.append(("术式熔断", ACCENT_RED))
                if res_chips:
                    r_row = tk.Frame(card, bg=bg)
                    r_row.pack(fill=tk.X, pady=(2, 1))
                    for rtxt, rfg in res_chips:
                        tk.Label(
                            r_row, text=rtxt, bg="#171923", fg=rfg, padx=5, pady=1,
                            font=(FONT_NAME, 8, "bold")
                        ).pack(side=tk.LEFT, padx=(0, 4))

                if getattr(p, "true_form_triggered", False):
                    parts_row = tk.Frame(card, bg=bg)
                    parts_row.pack(fill=tk.X, pady=(2, 1))
                    for attr_name in ("mouth_part", "arms_part"):
                        part = getattr(p, attr_name, None)
                        if part is not None:
                            p_fg = ACCENT_GOLD if part.alive else FG_MUTED
                            p_txt = (
                                f"{part.name}:{part.hp}/{part.max_hp}"
                                if part.alive
                                else f"{part.name}:已破坏"
                            )
                            tk.Label(
                                parts_row, text=p_txt, bg="#171923", fg=p_fg,
                                padx=6, pady=1, font=(FONT_NAME, 8, "bold")
                            ).pack(side=tk.LEFT, padx=(0, 6))

            # 状态胶囊
            try:
                statuses = list(c.statuses.items())
            except RuntimeError:
                statuses = []
            if statuses:
                st_wrap = tk.Frame(card, bg=bg)
                st_wrap.pack(fill=tk.X, pady=(3, 0))
                row_f = tk.Frame(st_wrap, bg=bg)
                row_f.pack(fill=tk.X)
                for idx, (st_name, st_obj) in enumerate(statuses):
                    if st_obj.is_buff:
                        s_bg, s_fg = "#1C3B2B", ACCENT_GREEN
                    elif st_obj.is_debuff:
                        s_bg, s_fg = "#3E1E24", ACCENT_RED
                    else:
                        s_bg, s_fg = "#252B3B", ACCENT_BLUE
                    tk.Label(
                        row_f, text=f"{st_name}:{st_obj.stacks}",
                        bg=s_bg, fg=s_fg, padx=5, pady=1, font=(FONT_NAME, 8, "bold")
                    ).pack(side=tk.LEFT, padx=(0, 4), pady=1)
                    if (idx + 1) % 4 == 0:
                        row_f = tk.Frame(st_wrap, bg=bg)
                        row_f.pack(fill=tk.X)

            p_str = c.format_triggered_passives(eng)
            if p_str:
                tk.Label(
                    card, text=f"被动: {p_str}", bg=bg, fg=ACCENT_GOLD,
                    font=(FONT_NAME, 8), anchor="w", wraplength=280, justify=tk.LEFT
                ).pack(fill=tk.X, pady=(3, 0))

    # ==========================================================================
    # 投掷舞台
    # ==========================================================================
    def _render_roll_begin(self, data: Dict[str, Any]) -> None:
        self._waiting_coin = False
        self.phase_lbl.config(text="[状态: 拼点/掷骰中]", fg=ACCENT_GOLD)
        self.reveal_btn.config(state=tk.DISABLED, bg=BG_INPUT, fg=FG_MUTED, text="揭晓下一枚硬币 [Space / Enter]")
        self.roll_actor_lbl.config(text=f"{data['actor']} [{data['skill']}] ({data['formula']})")
        self.roll_score_lbl.config(text=f"基础: {data['base_power']}")

        for w in self.coins_wrap.winfo_children():
            w.destroy()
        self._coin_labels.clear()

        total_coins = data["total_coins"]
        if total_coins == 0:
            tk.Label(
                self.coins_wrap, text="无变动值硬币", bg=BG_CARD, fg=FG_MUTED, font=(FONT_NAME, 9)
            ).pack(side=tk.LEFT)
            return

        coins_meta = data.get("coins_meta", [])
        for i in range(total_coins):
            unb = coins_meta[i]["unbreakable"] if i < len(coins_meta) else False
            bd = ACCENT_RED if unb else BORDER_COLOR
            lbl = tk.Label(
                self.coins_wrap, text=" ? ", bg="#151822", fg=FG_MUTED,
                width=5, pady=6, font=("Consolas", 11, "bold"),
                highlightbackground=bd, highlightthickness=2 if unb else 1
            )
            lbl.pack(side=tk.LEFT, padx=3)
            self._coin_labels.append(lbl)

    def _render_wait_coin(self, data: Dict[str, Any]) -> None:
        self._waiting_coin = True
        self.phase_lbl.config(text="[状态: 等待揭晓硬币]", fg=ACCENT_GOLD)
        idx = data["coin_idx"] - 1
        if 0 <= idx < len(self._coin_labels):
            self._coin_labels[idx].config(bg="#313650", fg=ACCENT_GOLD, text=" ? ")
        self.reveal_btn.config(
            state=tk.NORMAL,
            bg=ACCENT_GOLD,
            fg="#111",
            text=f"揭晓第 {data['coin_idx']}/{data['total_coins']} 枚 ({data['value']:+d}, {int(data['probability']*100)}%) [Space]",
        )

    def _render_coin_reveal(self, data: Dict[str, Any]) -> None:
        self._waiting_coin = False
        self.reveal_btn.config(
            state=tk.DISABLED, bg=BG_INPUT, fg=FG_MUTED, text="揭晓下一枚硬币 [Space / Enter]"
        )
        idx = data["coin_idx"] - 1
        if 0 <= idx < len(self._coin_labels):
            if data["triggered"]:
                self._coin_labels[idx].config(bg="#3E3219", fg=ACCENT_GOLD, text=f"{data['added_val']:+d}")
            else:
                self._coin_labels[idx].config(bg="#181A22", fg=FG_MUTED, text=" +0 ")
        self.roll_score_lbl.config(text=f"当前: {data['running_total']}")

    def _render_roll_end(self, data: Dict[str, Any]) -> None:
        self._waiting_coin = False
        self.reveal_btn.config(state=tk.DISABLED, bg=BG_INPUT, fg=FG_MUTED)
        self.roll_score_lbl.config(text=f"最终: {data['final_total']}")

    def _on_reveal_clicked(self) -> None:
        if self._waiting_coin:
            self._waiting_coin = False
            self._coin_event.set()

    def _on_toggle_auto(self) -> None:
        if not self.engine:
            return
        state = self.engine.toggle_auto_roll()
        self._update_auto_btn(state)
        self._append_log(f"[系统] 自动投掷已{'开启' if state else '关闭'}！")
        if state and self._waiting_coin:
            self._waiting_coin = False
            self._coin_event.set()

    def _update_auto_btn(self, enabled: bool) -> None:
        if not hasattr(self, "auto_btn"):
            return
        if enabled:
            self.auto_btn.config(text="自动投掷: 开启 [A]", bg=ACCENT_GREEN, fg="#111")
        else:
            self.auto_btn.config(text="自动投掷: 关闭 [A]", bg=BG_INPUT, fg=FG_TEXT)

    def _on_space_key(self, event: tk.Event) -> None:
        if self._active_dialog is not None:
            return
        if self._waiting_coin:
            self._on_reveal_clicked()

    def _on_a_key(self, event: tk.Event) -> None:
        if self._active_dialog is not None or isinstance(event.widget, tk.Entry):
            return
        if self.engine:
            self._on_toggle_auto()

    # ==========================================================================
    # 独立宽幅【选技操作台】
    # ==========================================================================
    def _clear_dock(self) -> None:
        for w in self.dock_inner.winfo_children():
            w.destroy()

    def _render_action_dock(self, data: Dict[str, Any]) -> None:
        self._pending_action_ctx = data
        char: Character = data["char"]
        slot_idx: int = data["slot_idx"]
        prev_summary: List[str] = data["prev_summary"]

        eng = self.engine
        pct = eng.format_hp_percent(char) if (eng and hasattr(eng, "format_hp_percent")) else ""
        team_color = ACCENT_BLUE if char.team == "P1" else ACCENT_RED
        self.phase_lbl.config(text=f"[状态: 等待 {char.name} 选技]", fg=team_color)
        self.action_title_lbl.config(
            text=f"轮到 [{char.team}] {char.name} (速度:{pct}) 选择技能 (槽位 {slot_idx + 1})",
            fg=team_color,
        )
        if prev_summary:
            self.action_info_lbl.config(
                text="本回合已选情报: " + "  |  ".join(prev_summary),
                fg=FG_TEXT,
            )
        else:
            self.action_info_lbl.config(
                text="请点击下方任意技能卡片出招（1v1 / 群攻 / 自身技将直接确认或进入速度拦截选择）：",
                fg=FG_MUTED,
            )

        self._show_skill_cards()

    def _show_skill_cards(self) -> None:
        self._clear_dock()
        ctx = self._pending_action_ctx
        if not ctx:
            return
        usable: List[Skill] = ctx["usable"]

        cols = 3
        for c in range(cols):
            self.dock_inner.columnconfigure(c, weight=1)

        for idx, sk in enumerate(usable):
            r, c = divmod(idx, cols)
            flags: List[str] = []
            if sk.clash_aoe:
                flags.append("拼点群攻")
            elif sk.is_aoe:
                flags.append("群攻")
            if not sk.clashable:
                flags.append("不可拼点")
            if sk.has_unbreakable():
                flags.append("不可摧毁")
            flag_str = f" [{'·'.join(flags)}]" if flags else ""

            btn_text = f"{idx}: {sk.name}  [阶:{sk.tier}]\n公式: {sk.format_formula()}{flag_str}"
            btn = tk.Button(
                self.dock_inner,
                text=btn_text,
                command=lambda s=sk: self._on_skill_clicked(s),
                bg=BG_CARD_ALT, fg=FG_TEXT,
                activebackground=ACCENT_GOLD, activeforeground="#111",
                relief=tk.FLAT, padx=12, pady=10,
                font=(FONT_NAME, 10, "bold"),
                justify=tk.LEFT, anchor="w", cursor="hand2"
            )
            btn.grid(row=r, column=c, sticky="nsew", padx=5, pady=5)

    def _on_skill_clicked(self, skill: Skill) -> None:
        ctx = self._pending_action_ctx
        eng = self.engine
        if not ctx or not eng:
            return
        char: Character = ctx["char"]
        candidates = eng.get_selectable_targets(char, skill, for_ai=False)

        if skill.clash_aoe:
            if len(candidates) <= 1:
                if len(candidates) == 1:
                    self._on_target_chosen(char, skill, candidates[0], candidates)
                else:
                    self._commit_action(Action(actor=char, skill=skill, targets=[]))
                return
            hint = "无差别群攻" if skill.indiscriminate else "群攻"
            self.action_info_lbl.config(
                text=f"已选拼点群攻【{skill.name}】，请选择 1 名拼点对手（胜出后释放{hint}）：",
                fg=ACCENT_GOLD,
            )
            self._show_target_cards(char, skill, candidates)
        elif skill.is_aoe:
            self._commit_action(Action(actor=char, skill=skill, targets=list(candidates)))
        elif skill.target_type == "self":
            self._commit_action(Action(actor=char, skill=skill, targets=[char]))
        else:
            if len(candidates) <= 1:
                if len(candidates) == 1:
                    self._on_target_chosen(char, skill, candidates[0], candidates)
                else:
                    self._commit_action(Action(actor=char, skill=skill, targets=[]))
                return
            self.action_info_lbl.config(
                text=f"已选【{skill.name}({skill.format_formula()})】，请点击下方目标：",
                fg=ACCENT_GOLD,
            )
            self._show_target_cards(char, skill, candidates)

    def _show_target_cards(
        self, actor: Character, skill: Skill, candidates: List[Character]
    ) -> None:
        self._clear_dock()

        back_btn = tk.Button(
            self.dock_inner, text="← 重新选择技能", command=self._show_skill_cards,
            bg=BG_INPUT, fg=ACCENT_GOLD, relief=tk.FLAT, padx=12, pady=6,
            font=(FONT_NAME, 9, "bold"), cursor="hand2"
        )
        back_btn.grid(row=0, column=0, columnspan=3, sticky="w", padx=5, pady=(2, 8))

        cols = 3
        for c in range(cols):
            self.dock_inner.columnconfigure(c, weight=1)

        eng = self.engine
        for idx, tgt in enumerate(candidates):
            r, c = divmod(idx, cols)
            if tgt is actor:
                side, fg = "自身", ACCENT_GOLD
            elif tgt.team == actor.team:
                side, fg = "友方/队友", ACCENT_GREEN
            else:
                side, fg = "敌方", ACCENT_RED

            pct = eng.format_hp_percent(tgt) if (eng and hasattr(eng, "format_hp_percent")) else ""
            st_txt = ", ".join(f"{k}:{v.stacks}" for k, v in tgt.statuses.items())
            st_line = f"\n状态: {st_txt}" if st_txt else ""
            btn = tk.Button(
                self.dock_inner,
                text=f"[{side}] {tgt.name} (速度:{pct})\nHP: {tgt.hp}/{tgt.max_hp}{st_line}",
                command=lambda t=tgt: self._on_target_chosen(actor, skill, t, candidates),
                bg=BG_CARD_ALT, fg=fg,
                activebackground=ACCENT_BLUE, activeforeground="#FFF",
                relief=tk.FLAT, padx=12, pady=10,
                font=(FONT_NAME, 10, "bold"),
                justify=tk.LEFT, anchor="w", cursor="hand2"
            )
            btn.grid(row=r + 1, column=c, sticky="nsew", padx=5, pady=5)

    def _on_target_chosen(
        self,
        actor: Character,
        skill: Skill,
        target: Character,
        candidates: List[Character],
    ) -> None:
        ctx = self._pending_action_ctx
        eng = self.engine
        if not ctx or not eng:
            return
        prev_actions: List[Action] = ctx.get("prev_actions", [])
        interceptables = (
            eng.get_interceptable_actions(actor, skill, target, prev_actions)
            if hasattr(eng, "get_interceptable_actions")
            else []
        )
        if not interceptables:
            self._commit_action(Action(actor=actor, skill=skill, targets=[target]))
            return

        self._show_interception_cards(actor, skill, target, candidates, interceptables)

    def _show_interception_cards(
        self,
        actor: Character,
        skill: Skill,
        target: Character,
        candidates: List[Character],
        interceptables: List[Action],
    ) -> None:
        self._clear_dock()
        eng = self.engine
        assert eng is not None

        my_pct = eng.format_hp_percent(actor)
        tgt_pct = eng.format_hp_percent(target)
        self.action_info_lbl.config(
            text=f"[速度压制] {actor.name}({my_pct}) 比 {target.name}({tgt_pct}) 更快！请选择攻击方式：",
            fg=ACCENT_GOLD,
        )

        if len(candidates) > 1:
            back_cmd = lambda: self._show_target_cards(actor, skill, candidates)
            back_txt = "← 重新选择目标"
        else:
            back_cmd = self._show_skill_cards
            back_txt = "← 重新选择技能"

        tk.Button(
            self.dock_inner, text=back_txt, command=back_cmd,
            bg=BG_INPUT, fg=ACCENT_GOLD, relief=tk.FLAT, padx=12, pady=6,
            font=(FONT_NAME, 9, "bold"), cursor="hand2"
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=5, pady=(2, 8))

        cols = 3
        for c in range(cols):
            self.dock_inner.columnconfigure(c, weight=1)

        def _pick(intercept_act: Optional[Action]) -> None:
            act = Action(actor=actor, skill=skill, targets=[target])
            eng.configure_interception(act, intercept_act, interceptables)
            self._commit_action(act)

        tk.Button(
            self.dock_inner,
            text=f"0: 单方面攻击（不换攻击对象）\n不改变 {target.name} 原目标，直接单方面攻击",
            command=lambda: _pick(None),
            bg=BG_CARD_ALT, fg=ACCENT_BLUE,
            activebackground=ACCENT_BLUE, activeforeground="#FFF",
            relief=tk.FLAT, padx=12, pady=10,
            font=(FONT_NAME, 10, "bold"),
            justify=tk.LEFT, anchor="w", cursor="hand2"
        ).grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        for idx, s_act in enumerate(interceptables, start=1):
            r, c = divmod(idx, cols)
            orig_tgts = ",".join(t.name for t in s_act.targets) or "-"
            assert s_act.skill is not None
            tk.Button(
                self.dock_inner,
                text=(
                    f"{idx}: 拦截拼点 -> 【{s_act.skill.name}({s_act.skill.format_formula()})】\n"
                    f"原目标: {orig_tgts} → 强行拉入与你拼点"
                ),
                command=lambda sa=s_act: _pick(sa),
                bg=BG_CARD_ALT, fg=ACCENT_GOLD,
                activebackground=ACCENT_GOLD, activeforeground="#111",
                relief=tk.FLAT, padx=12, pady=10,
                font=(FONT_NAME, 10, "bold"),
                justify=tk.LEFT, anchor="w", cursor="hand2"
            ).grid(row=r + 1, column=c, sticky="nsew", padx=5, pady=5)

    def _commit_action(self, action: Action) -> None:
        self._pending_action_ctx = None
        self._clear_dock()
        self.phase_lbl.config(text="[状态: 战斗结算中]", fg=FG_MUTED)
        self.action_title_lbl.config(text="战斗推演中...", fg=FG_MUTED)
        self.action_info_lbl.config(text="等待下一位需要手动选技的角色或战斗结算...", fg=FG_MUTED)
        self._action_result = action
        self._action_event.set()

    # ==========================================================================
    # 外置角色卡交互弹窗（如宿傩原身启动后选择攻击部位）
    # ==========================================================================
    def _open_prompt_dialog(self, payload: Dict[str, Any]) -> None:
        prompt: str = payload["prompt"]
        recent: List[str] = payload["recent"]

        parsed_options: List[Tuple[str, str]] = []
        header_lines: List[str] = []
        for line in recent[-6:]:
            m = re.match(r"^\s*(\d+)\s*[:：]\s*(.+)$", line)
            if m:
                parsed_options.append((m.group(1), m.group(2).strip()))
            else:
                header_lines.append(line.strip())

        dlg = tk.Toplevel(self.root)
        dlg.title("战斗选择")
        dlg.configure(bg=BG_PANEL)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        self._active_dialog = dlg

        title_txt = header_lines[-1] if header_lines else (prompt.strip() or "请选择")
        tk.Label(
            dlg, text=title_txt, bg=BG_PANEL, fg=ACCENT_GOLD,
            font=(FONT_NAME, 11, "bold"), wraplength=420, justify=tk.LEFT, padx=18, pady=14
        ).pack(fill=tk.X)

        def _submit(val: str) -> None:
            self._input_result = val
            self._active_dialog = None
            dlg.destroy()
            self._input_event.set()

        if parsed_options:
            box = tk.Frame(dlg, bg=BG_PANEL, padx=18, pady=6)
            box.pack(fill=tk.X)
            for opt_key, opt_label in parsed_options:
                tk.Button(
                    box, text=f"{opt_key}: {opt_label}",
                    command=lambda k=opt_key: _submit(k),
                    bg=BG_CARD_ALT, fg=FG_TEXT,
                    activebackground=ACCENT_GOLD, activeforeground="#111",
                    relief=tk.FLAT, padx=14, pady=9, font=(FONT_NAME, 10, "bold"),
                    anchor="w", cursor="hand2"
                ).pack(fill=tk.X, pady=4)
        else:
            var = tk.StringVar()
            ent = tk.Entry(dlg, textvariable=var, bg=BG_INPUT, fg=FG_TEXT, font=(FONT_NAME, 11), relief=tk.FLAT)
            ent.pack(fill=tk.X, padx=18, pady=8)
            ent.focus_set()
            ent.bind("<Return>", lambda e: _submit(var.get()))
            tk.Button(
                dlg, text="确认", command=lambda: _submit(var.get()),
                bg=ACCENT_GOLD, fg="#111", relief=tk.FLAT, padx=16, pady=6, font=(FONT_NAME, 10, "bold")
            ).pack(pady=(4, 14))

        dlg.protocol("WM_DELETE_WINDOW", lambda: _submit(parsed_options[0][0] if parsed_options else "0"))
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

    # ==========================================================================
    # 结束与清理
    # ==========================================================================
    def _on_game_end(self, winner: Optional[str]) -> None:
        self._clear_dock()
        self.reveal_btn.config(state=tk.DISABLED, bg=BG_INPUT, fg=FG_MUTED)
        win_txt = "平局 (DRAW)" if winner == "DRAW" else f"胜者：{winner} 队伍！"
        self.phase_lbl.config(text="[状态: 战斗结束]", fg=ACCENT_GOLD)
        self.action_title_lbl.config(text=f"战斗结束 · {win_txt}", fg=ACCENT_GOLD)
        self.action_info_lbl.config(text="可以查阅下方日志，或点击按钮返回重新配置阵容。", fg=FG_MUTED)
        tk.Button(
            self.dock_inner, text="返回阵容配置", command=self._abort_and_return_setup,
            bg=ACCENT_GOLD, fg="#111", relief=tk.FLAT, padx=24, pady=10,
            font=(FONT_NAME, 11, "bold"), cursor="hand2"
        ).grid(row=0, column=0, sticky="w", padx=6, pady=6)

    def _release_waits(self) -> None:
        self._abort_flag = True
        self._waiting_coin = False
        self._action_event.set()
        self._coin_event.set()
        self._input_event.set()
        if self._active_dialog is not None:
            try:
                self._active_dialog.destroy()
            except Exception:
                pass
            self._active_dialog = None

    def _abort_and_return_setup(self) -> None:
        self._release_all_waits_and_stop()
        self.build_setup_page()

    def _release_all_waits_and_stop(self) -> None:
        self._release_waits()
        if self.engine:
            try:
                self.engine.bgm.stop_all()
            except Exception:
                pass
        if self.battle_thread and self.battle_thread.is_alive():
            self.battle_thread.join(timeout=0.5)
        self.engine = None

    def _on_close(self) -> None:
        self._release_waits()
        if self.engine:
            try:
                self.engine.bgm.stop_all()
            except Exception:
                pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    GameWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
