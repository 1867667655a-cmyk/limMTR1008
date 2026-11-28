# ALND的工作日志
2026/11/24 19:53
engine\statuses.py", line 200, in _on_take_damage
f"  [破裂] {self.owner.name} 触发破裂造成 {dmg} 点伤害（层数减半为 {new_stacks}）"
AttributeError: 'NoneType' object has no attribute 'name'
engine/statuses.py修复


2026/11/24 20:27
更新不可摧毁硬币engine/combat.py、engine/models.py、engine/events.py
可给技能硬币额外定义“unbreakable=True”以调用，例：
coins=[Coin(3), Coin(3), Coin(3, unbreakable=True)]



2026/11/25 23:39
fw0+7做的群攻有bug,现已修复,更新了engine/combat.py、main.py


2026/11/26 00:03
新增通用更多buff和追加伤害方法，更新了engine/statuses.py、engine/models.py、engine/combat.py、engine/__init__.py

2026/11/26 22:46
更新BGM播放,新增文件夹bgm,更改了engine\combat.py、engine\bgm.py
可在角色内定义bgm = BGMTrack(
        track_id="",
        title="",
以调用
