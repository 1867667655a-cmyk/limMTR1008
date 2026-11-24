from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, List, Optional

from .models import Character


def resolve_character_path(
    file_input: str | Path, search_dir: Optional[str | Path] = None
) -> Path:
    """
    智能解析角色卡文件名：
    支持传入 'flame_knight'、'flame_knight.py' 或完整/相对路径。
    """
    raw = str(file_input).strip().strip('"').strip("'")
    if not raw:
        raise FileNotFoundError("文件名为空")

    candidates: List[Path] = []
    p = Path(raw)
    candidates.append(p)
    if p.suffix != ".py":
        candidates.append(Path(f"{raw}.py"))

    if search_dir is not None:
        sdir = Path(search_dir)
        candidates.append(sdir / raw)
        if p.suffix != ".py":
            candidates.append(sdir / f"{raw}.py")

    for c in candidates:
        if c.exists() and c.is_file():
            return c.resolve()

    raise FileNotFoundError(f"找不到角色卡文件: {file_input}")


def load_character_from_file(
    file_path: str | Path,
    team: str = "P1",
    search_dir: Optional[str | Path] = None,
    **kwargs: Any,
) -> Character:
    """
    从外部 .py 文件动态加载角色卡。
    外部 .py 文件只需提供以下两种规范之一即可：
      1) 定义工厂函数 `create_character(team: str, **kwargs) -> Character`
      2) 定义一个 `Character` 子类（接受 team 参数）
    """
    path = resolve_character_path(file_path, search_dir=search_dir)

    module_name = f"ext_char_{path.stem}_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载角色模块: {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if hasattr(module, "create_character") and callable(module.create_character):
        char = module.create_character(team=team, **kwargs)
        if not isinstance(char, Character):
            raise TypeError(f"{path} 的 create_character 必须返回 Character 实例")
        return char

    for attr in vars(module).values():
        if isinstance(attr, type) and issubclass(attr, Character) and attr is not Character:
            return attr(team=team, **kwargs)

    raise ValueError(f"{path} 中未找到 create_character 函数或 Character 子类")


def load_characters_from_dir(dir_path: str | Path) -> List[tuple[Path, str]]:
    """扫描目录下所有合法外置角色 .py 文件"""
    p = Path(dir_path)
    if not p.exists():
        return []
    files = sorted(f for f in p.glob("*.py") if not f.name.startswith("_"))
    return [(f, f.stem) for f in files]
