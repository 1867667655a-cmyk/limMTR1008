# ALND的工作日志
2026/11/24 19:53
engine\statuses.py", line 200, in _on_take_damage
f"  [破裂] {self.owner.name} 触发破裂造成 {dmg} 点伤害（层数减半为 {new_stacks}）"
AttributeError: 'NoneType' object has no attribute 'name'

修复
