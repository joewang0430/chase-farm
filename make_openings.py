#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""枚举全部 8 手开局(四重对称去重), 写成工人用的开局池。

对照 Egaroucid 作者公布的官方数字验收:
  手数  1   2   3    4    5     6      7       8
  局面  1   3   14   60   322   1773   10649   67245

我们的枚举会比官方多约 0.1% —— 差异来自"中途已终局的局面": 官方不计入后续层数,
我们会剔除掉双方都无子可走的局面, 但仍可能有细微差异(以官方数字为准做告警, 不阻断)。
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
