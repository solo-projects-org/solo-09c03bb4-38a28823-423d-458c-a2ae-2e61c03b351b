"""名录对照的链式换算。

对照表每行 (起版 v, 旧学名 old, 下一版学名 new) 表示：
在 v -> v+1 这一次换版中，old 变为 new。
- 同一 (v, old) 出现多行  = 拆种（一个旧名对应多个下一版学名）；
- 多个不同 old 指向同一 new = 并种；
- 某版本没有列出的名称   = 沿用到下一版。
版号为连续整数，逐级换算即可把任意版本的学名追到最新版。
"""


def build_index(rows):
    """rows: 可迭代的 (from_version, old_name, next_name)，返回查询索引。"""
    idx = {}
    for from_version, old_name, next_name in rows:
        idx.setdefault((from_version, old_name), set()).add(next_name)
    return idx


def trace(names, from_version, to_version, idx):
    """把一组学名从 from_version 版换算到 to_version 版，返回可达学名集合。

    拆种时一个名字会扩成多个；并种时多个名字会收敛。
    若 from_version >= to_version，原样返回。
    """
    current = set(names)
    for version in range(from_version, to_version):
        stepped = set()
        for name in current:
            mapped = idx.get((version, name))
            if mapped:
                stepped |= mapped
            else:
                stepped.add(name)  # 未列出的名称沿用
        current = stepped
    return current


def audit_trace(names, from_version, to_version, idx, checklists):
    """逐版核对正式名录的换算，返回 (可达学名集合, 缺名条目列表)。

    checklists: {版号: 该版正式学名集合}。
    - 某版没有正式名单（版号不在 checklists 中）：该版不做收录核对，
      沿用原换算规则（对照未列出的名称原样带到下一版）。
    - 某版有正式名单：换算到该版的每个学名都必须被该版收录；
      任一可达学名未收录就记录一条 (版号, 缺名)。即使之后经并种回到
      有效学名，缺名条目仍保留，整条记录交待核处理。
    核对从换算起点版（from_version）开始，逐版到 to_version 为止。
    """
    current = set(names)
    missing = []
    seen = set()
    for version in range(from_version, to_version + 1):
        official = checklists.get(version)
        if official is not None:
            for name in sorted(current):
                if name not in official and (version, name) not in seen:
                    missing.append({"version": version, "name": name})
                    seen.add((version, name))
        if version < to_version:
            stepped = set()
            for name in current:
                mapped = idx.get((version, name))
                if mapped:
                    stepped |= mapped
                else:
                    stepped.add(name)  # 缺正式名单的版：未列出名称沿用
            current = stepped
    return current, missing
