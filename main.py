import re
import traceback
from pathlib import Path
from typing import Dict, List, Tuple

from engine import (
    Action,
    Character,
    CombatEngine,
    InteractiveRollHandler,
    load_character_from_file,
    load_characters_from_dir,
)


def parse_battle_size(raw: str) -> Tuple[int, int]:
    s = raw.strip()
    if s.isdigit():
        n = int(s)
        if n > 0:
            return n, n
    m = re.fullmatch(r"(\d+)\s*[vVxX×:\s]\s*(\d+)", s)
    if m:
        n1, n2 = int(m.group(1)), int(m.group(2))
        if n1 > 0 and n2 > 0:
            return n1, n2
    raise ValueError("格式无效，请输入正整数（如 1、3）或双方人数（如 2v3）")


def prompt_battle_size() -> Tuple[int, int]:
    while True:
        raw = input("请输入几人作战（如输入 1 表示 1v1，3 表示 3v3，或 2v3 表示 P1 2人 vs P2 3人）: ")
        try:
            n1, n2 = parse_battle_size(raw)
            print(f"已确认作战规模：P1({n1}人) VS P2({n2}人)\n")
            return n1, n2
        except ValueError as e:
            print(f"  [错误] {e}")


def prompt_roster(n1: int, n2: int, char_dir: Path) -> List[Character]:
    available = load_characters_from_dir(char_dir)
    print(f"当前目录 ({char_dir.name}/) 扫描到的角色卡文件:")
    if available:
        for path, _ in available:
            print(f"  - {path.name}")
    else:
        print("  （暂无文件，你也可以直接输入其他路径的 .py 文件）")
    print()

    roster: List[Character] = []
    name_counts: Dict[str, int] = {}

    def _input_team(team: str, count: int) -> None:
        for idx in range(1, count + 1):
            while True:
                fname = input(f"请输入 {team} 第 {idx} 位入场角色卡文件名: ").strip()
                if not fname:
                    continue
                try:
                    char = load_character_from_file(fname, team=team, search_dir=char_dir)
                    char.char_id = f"{char.char_id}_{team}_{idx}"
                    base_name = char.name
                    name_counts[base_name] = name_counts.get(base_name, 0) + 1
                    roster.append(char)
                    print(f"  -> 已入场: [{team}] {char.name} (HP: {char.hp}/{char.max_hp})")
                    break
                except Exception as e:
                    print(f"  [加载失败] {e}")
                    traceback.print_exc(limit=1)

    _input_team("P1", n1)
    _input_team("P2", n2)

    seen_counter: Dict[str, int] = {}
    for c in roster:
        if name_counts[c.name] > 1:
            seen_counter[c.name] = seen_counter.get(c.name, 0) + 1
            c.name = f"{c.name}#{seen_counter[c.name]}({c.team})"

    return roster


def cli_manual_selector(
    engine: CombatEngine,
    char: Character,
    slot_idx: int,
    previous_selections: List[Action],
) -> Action:
    """文本交互选技：任意输入阶段均可通过输入 a/auto 切换自动投掷；支持选择攻击队友"""
    usable = [
        sk
        for sk in char.get_usable_skills(engine)
        if sk.is_aoe or sk.clash_aoe or engine.get_selectable_targets(char, sk, for_ai=False)
    ]

    print(
        f"\n>>> 轮到 [{char.team}] {char.name} (HP: {char.hp}/{char.max_hp}) 选择技能 "
        f"(槽位 {slot_idx + 1} | 自动投掷: {'开启' if engine.auto_roll else '关闭'}):"
    )
    if previous_selections:
        print("  [本回合已选情报]")
        for act in previous_selections:
            s_name = f"{act.skill.name}({act.skill.format_formula()})" if act.skill else "无技能(挨打)"
            tgts = ",".join(
                f"{t.name}[队友]" if (t.team == act.actor.team and t is not act.actor) else t.name
                for t in act.targets
            ) or "-"
            print(f"    - [{act.actor.team}] {act.actor.name}: {s_name} -> {tgts}")

    if char.statuses:
        st_str = ", ".join(f"{k}:{v.stacks}" for k, v in char.statuses.items())
        print(f"  [自身状态] {st_str}")

    if not usable:
        print("  当前无可用技能，本回合只能挨打。")
        return Action(actor=char, skill=None, targets=[], consume_from_hand=False)

    print("  [可用技能]")
    for idx, sk in enumerate(usable):
        print(f"    {idx}: {sk.name} [阶:{sk.tier}] ({sk.format_formula()})")

    while True:
        raw = input("  请输入技能编号 (输入 a 可随时切换自动投掷): ").strip().lower()
        if raw in ("a", "auto", "t"):
            state = engine.toggle_auto_roll()
            print(f"  [系统] 自动投掷已{'开启' if state else '关闭'}！")
            continue
        if raw.isdigit() and 0 <= int(raw) < len(usable):
            chosen_skill = usable[int(raw)]
            break
        print("  输入无效，请重新输入。")

    candidates = engine.get_selectable_targets(char, chosen_skill, for_ai=False)

    def _pick_single_target(prompt_title: str, pool: List[Character]) -> List[Character]:
        if not pool:
            return []
        if len(pool) == 1:
            return [pool[0]]
        print(f"  [{prompt_title}]")
        for idx, tgt in enumerate(pool):
            if tgt is char:
                side = "自身"
            elif tgt.team == char.team:
                side = "友方/队友"
            else:
                side = "敌方"
            st = ", ".join(f"{k}:{v.stacks}" for k, v in tgt.statuses.items())
            st_info = f" | 状态: {st}" if st else ""
            print(f"    {idx}: [{side}] {tgt.name} (HP:{tgt.hp}/{tgt.max_hp}{st_info})")
        while True:
            traw = input("  请输入目标编号 (输入 a 可切换自动投掷): ").strip().lower()
            if traw in ("a", "auto", "t"):
                state = engine.toggle_auto_roll()
                print(f"  [系统] 自动投掷已{'开启' if state else '关闭'}！")
                continue
            if traw.isdigit() and 0 <= int(traw) < len(pool):
                return [pool[int(traw)]]
            print("  输入无效，请重新输入。")

    if chosen_skill.clash_aoe:
        hint = "无差别群攻（敌人+队友）" if chosen_skill.indiscriminate else "群攻（全体敌人）"
        print(f"  [提示] 该技能为拼点群攻，请选择 1 名目标进行拼点，胜出后将直接释放{hint}！")
        targets = _pick_single_target("选择拼点目标", candidates)
    elif chosen_skill.is_aoe:
        targets = list(candidates)
    elif chosen_skill.target_type == "self":
        targets = [char]
    else:
        targets = _pick_single_target("选择目标", candidates)

    return Action(actor=char, skill=chosen_skill, targets=targets)


def main() -> None:
    print("=== 回合制拼点对战引擎 ===")
    char_dir = Path(__file__).parent / "characters"

    n1, n2 = prompt_battle_size()
    roster = prompt_roster(n1, n2, char_dir)

    print("\n选择操控方式:")
    print("  1: 全员手动输入（P1 与 P2 均由控制台手动选技）")
    print("  2: P1 手动，P2 自动AI")
    print("  3: 全员自动AI推演")
    mode = input("请输入模式编号 (默认 1): ").strip() or "1"

    auto_choice = input("是否开启【自动投掷】？(y 开启 / n 关闭手动逐枚按空格揭晓，默认 n): ").strip().lower()
    initial_auto = auto_choice in ("y", "yes", "1", "是")
    print("提示：战斗中随时可按 [A]（投掷时）或输入 [a]（选技时）开启/关闭自动投掷；选目标时可直接选择 [友方/队友] 进行攻击！\n")

    roll_handler = InteractiveRollHandler(auto_delay=0.18)
    engine = CombatEngine(
        characters=roster,
        auto_roll=initial_auto,
        roll_handler=roll_handler,
    )

    if mode == "1":
        engine.manual_selector = cli_manual_selector
    elif mode == "2":
        def p1_manual_p2_ai(eng, char, slot_idx, prev):
            if char.team == "P1":
                return cli_manual_selector(eng, char, slot_idx, prev)
            return eng._default_ai_select(char, slot_idx, prev)
        engine.manual_selector = p1_manual_p2_ai

    engine.run_game()


if __name__ == "__main__":
    main()
