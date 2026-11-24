# limMTR1008
limMTR
怎么把技能变成红币：
单枚红币（混合白币+红币）：给对应硬币加 unbreakable=True，例如 [Coin(4), Coin(5, unbreakable=True)]。拼输后白币碎掉，只保留红币并强制变为 +1。
全技能红币（所有硬币都不会完全碎掉）：给技能本身加 unbreakable=True。拼输后所有硬币保留并强制变为 +1。
怎么叠加群攻：
普通敌方群攻：target_type="all_enemies"
无差别群攻：target_type="all_others"（或 indiscriminate=True）
拼点群攻：clash_aoe=True（若要同时无差别，再加 indiscriminate=True）
改了events
改了models
改了combot
