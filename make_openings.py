#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""枚举全部 8 手开局(四重对称去重), 写成工人用的开局池。

对照 Egaroucid 作者公布的官方数字验收:
  手数  1   2   3    4    5     6      7       8
  局面  1   3   14   60   322   1773   10649   67245

我们的结果: 前 6 层**逐个精确吻合**; 第 7 层 10658(+9), 第 8 层 67361(+116), 即多 0.17%。

**这 0.17% 的成因未查明。** 已逐一实测排除的假设:
  - 含"已终局"的线            -> 0 个(黑白棋最少 9 手才可能终局)
  - 含"一方被全歼"的线        -> 0 个
  - 含"轮到方必须 PASS"的线   -> 深度8 只有 6 个, 不是 116
  - 我们池内有重复            -> 0 个(规范形两两不同)
  - PASS 按"换边不计手"处理   -> 结果完全相同
  - 改用 8 重对称(含黑白交换) -> 差 -6298, 方向都不对
要查明大概需要读作者的枚举代码。

**对本项目的影响**: 池中每一条都经重放验证为合法、互不重复、走完恰好 12 子、轮到黑方,
作为开局采样池完全可用; 0.17% 的额外条目不构成风险。故不作剔除。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from othello import (INIT_BLACK, INIT_WHITE, legal_moves, apply_move,   # noqa: E402
                     canonical, moves_list)

OFFICIAL = {1: 1, 2: 3, 3: 14, 4: 60, 5: 322, 6: 1773, 7: 10649, 8: 67245}


def enumerate_openings(depth):
    """返回 {规范形key: 着法序列}, 深度为 depth 手。"""
    frontier = {canonical(INIT_BLACK, INIT_WHITE) + (1,): (INIT_BLACK, INIT_WHITE, 1, [])}
    for d in range(1, depth + 1):
        nxt = {}
        for _, (black, white, side, seq) in frontier.items():
            my, opp = (black, white) if side == 1 else (white, black)
            legal = legal_moves(my, opp)
            if legal == 0:
                continue                    # 该方无子可走: 此处直接剪掉(含已终局的线)
            for pos in moves_list(legal):
                nm, no = apply_move(my, opp, pos)
                nb, nw = (nm, no) if side == 1 else (no, nm)
                key = canonical(nb, nw) + (-side,)
                if key not in nxt:
                    nxt[key] = (nb, nw, -side, seq + [pos])
        frontier = nxt
        n = len(frontier)
        exp = OFFICIAL.get(d)
        flag = ""
        if exp:
            diff = n - exp
            flag = " (官方 %d, 差 %+d)" % (exp, diff) if diff else " (与官方一致)"
        sys.stderr.write("  深度 %d: %d 个局面%s\n" % (d, n, flag))
        sys.stderr.flush()
    return frontier


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--depth', type=int, default=8)
    args = ap.parse_args()

    sys.stderr.write("枚举 %d 手开局(四重对称去重)...\n" % args.depth)
    frontier = enumerate_openings(args.depth)
    tmp = args.out + ".tmp"
    with open(tmp, 'w') as f:
        for _, (_, _, _, seq) in frontier.items():
            f.write(json.dumps({"moves": seq}, separators=(',', ':')) + "\n")
    os.rename(tmp, args.out)
    sys.stderr.write("已写出 %d 条 -> %s\n" % (len(frontier), args.out))


if __name__ == '__main__':
    main()
