#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一盘棋逐手画出来 —— 给人眼核对用, 不是给机器判卷用。

用法:
  python3 farm/replay.py ~/chase_farm/shards                       # 随机抽一盘
  python3 farm/replay.py ~/chase_farm/shards --seed 7              # 换一盘
  python3 farm/replay.py ~/chase_farm/shards --src p107_..._301883 --game 3
  python3 farm/replay.py ~/chase_farm/shards --openings ~/chase_farm/openings8.jsonl
      # 顺带反查出未记录的开局 8 手, 得到完整棋谱(可直接在 Sensei 里重放)
  python3 farm/replay.py ~/chase_farm/shards --pcs-from 30 --pcs-to 40    # 只看一段

每一手会给出:
  - 棋盘(绝对黑白, 不是"我方/对手"), 标出合法点、老师的最优手、实际走的那一手
  - 轮到谁(黑/白)
  - 老师(Edax L21)认为的最优手与评分, **评分是轮到方视角**
  - 实际走了哪一手, 以及这一手在随机段(T>0)还是确定段(T=0)
  - 可直接粘进 Sensei / Edax 的局面串

**颜色是推导出来的, 不是记录里的**: 数据只存"轮到方/对手"。开局池的每条线都无 PASS,
走完 8 手恰好轮到黑方, 所以 pcs=12 一定是黑先; 之后按"是否发生 PASS"逐手推进即可。
"""

import argparse
import collections
import glob
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from othello import (INIT_BLACK, INIT_WHITE, legal_moves, apply_move,   # noqa: E402
                     popcount, pos_to_mv, mv_to_pos, moves_list)

BLACK, WHITE = 1, -1
NAME = {BLACK: "● 黑(X)", WHITE: "○ 白(O)"}


def draw(black, white, legal=0, best=-1, played=-1):
    """画棋盘。X=黑 O=白 .=空; + 合法点; # 老师最优手; @ 实际走的; * 两者相同。"""
    out = ["    a b c d e f g h"]
    for row in range(8):
        cells = []
        for col in range(8):
            i = row * 8 + col
            if (black >> i) & 1:
                c = 'X'
            elif (white >> i) & 1:
                c = 'O'
            elif i == best and i == played:
                c = '*'
            elif i == best:
                c = '#'
            elif i == played:
                c = '@'
            elif (legal >> i) & 1:
                c = '+'
            else:
                c = '.'
            cells.append(c)
        out.append(" %d  %s  %d" % (row + 1, ' '.join(cells), row + 1))
    out.append("    a b c d e f g h")
    return "\n".join(out)


def board_str(black, white, side):
    """65 字符局面串: 64 格(X=黑 O=白 -=空) + 轮到方。Sensei / Edax setboard 都吃这个。"""
    s = []
    for i in range(64):
        s.append('X' if (black >> i) & 1 else ('O' if (white >> i) & 1 else '-'))
    return ''.join(s) + (' X' if side == BLACK else ' O')


def recover_move(my, opp, nxt_my, nxt_opp):
    """从相邻两个局面反推走了哪一步。返回 (pos, 对手是否PASS); 找不到返回 (None, None)。"""
    legal = legal_moves(my, opp)
    hits = []
    for pos in moves_list(legal):
        nm, no = apply_move(my, opp, pos)
        if legal_moves(no, nm) and (no, nm) == (nxt_my, nxt_opp):
            hits.append((pos, False))          # 正常换边
        elif legal_moves(nm, no) and (nm, no) == (nxt_my, nxt_opp):
            hits.append((pos, True))           # 对手无子可走, PASS 回来
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return hits[0]                         # 多步殊途同归, 取其一(极罕见)
    return (None, None)


def find_opening(black, white, openings_path):
    """拿 pcs=12 的盘面反查开局池, 找回未记录的前 8 手。找不到返回 None。"""
    if not openings_path or not os.path.exists(openings_path):
        return None
    for line in open(openings_path):
        line = line.strip()
        if not line:
            continue
        seq = json.loads(line)["moves"]
        b, w, side = INIT_BLACK, INIT_WHITE, BLACK
        ok = True
        for pos in seq:
            my, opp = (b, w) if side == BLACK else (w, b)
            if not ((legal_moves(my, opp) >> pos) & 1):
                ok = False
                break
            my, opp = apply_move(my, opp, pos)
            b, w = (my, opp) if side == BLACK else (opp, my)
            side = -side
        if ok and b == black and w == white:
            return seq
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('shards')
    ap.add_argument('--src', default=None, help='指定工人(记录里的 src)')
    ap.add_argument('--game', type=int, default=None, help='指定该工人的第几盘(记录里的 g)')
    ap.add_argument('--seed', type=int, default=0, help='随机抽一盘时的种子')
    ap.add_argument('--openings', default=None, help='开局池, 用于反查未记录的前8手')
    ap.add_argument('--pcs-from', type=int, default=0)
    ap.add_argument('--pcs-to', type=int, default=99)
    args = ap.parse_args()

    paths = ([args.shards] if args.shards.endswith('.jsonl')
             else sorted(glob.glob(os.path.join(args.shards, '*.jsonl'))))
    if not paths:
        print("没找到分片: %s" % args.shards)
        return 1

    # 只挑出目标那一盘, 避免把几十万条全读进内存
    want_src, want_g = args.src, args.game
    if want_src is None or want_g is None:
        rng = random.Random(args.seed)
        p = rng.choice(paths)
        keys = set()
        for line in open(p):
            if line.strip():
                r = json.loads(line)
                keys.add((r['src'], r['g']))
        want_src, want_g = rng.choice(sorted(keys))
        paths = [p]

    recs = []
    for p in paths:
        for line in open(p):
            if not line.strip():
                continue
            r = json.loads(line)
            if r['src'] == want_src and r['g'] == want_g:
                recs.append(r)
        if recs:
            break
    if not recs:
        print("没找到这盘: src=%s g=%s" % (want_src, want_g))
        return 1
    recs.sort(key=lambda x: x['pcs'])

    print("=" * 62)
    print("对局来源: %s  第 %d 盘" % (want_src, want_g))
    print("记录 %d 手 (pcs %d ~ %d)   老师 L%s + hint1   选择器 L%s   温度策略 %s"
          % (len(recs), recs[0]['pcs'], recs[-1]['pcs'],
             recs[0]['L'], recs[0]['SL'], recs[0].get('Tsched', '?')))
    print("=" * 62)

    # 颜色推导: pcs=12 一定是黑先(开局池每条线都无 PASS, 走完 8 手轮到黑)
    side = BLACK
    if recs[0]['pcs'] != 12:
        print("注意: 首条记录 pcs=%d 而非 12, 无法确定颜色, 以下按黑先推导, 可能整体反色。"
              % recs[0]['pcs'])

    first_my, first_opp = int(recs[0]['my']), int(recs[0]['opp'])
    if args.openings:
        seq = find_opening(first_my, first_opp, args.openings)
        if seq:
            print("\n开局(未记录在数据里, 由盘面反查开局池得到)")
            print("  前 8 手: %s" % ' '.join(pos_to_mv(p).upper() for p in seq))
        else:
            print("\n开局: 在开局池里没找到匹配的线(不影响后续复盘)")

    total = len(recs)
    for i, r in enumerate(recs):
        pcs = r['pcs']
        my, opp = int(r['my']), int(r['opp'])
        black, white = (my, opp) if side == BLACK else (opp, my)
        best = mv_to_pos(r['best'])
        played, opp_passed = (None, None)
        if i + 1 < total:
            played, opp_passed = recover_move(my, opp, int(recs[i + 1]['my']),
                                              int(recs[i + 1]['opp']))
        if not (args.pcs_from <= pcs <= args.pcs_to):
            # 不打印, 但仍要推进颜色
            if played is not None:
                side = side if opp_passed else -side
            continue

        legal = legal_moves(my, opp)
        print("\n" + "-" * 62)
        print("第 %d 手   盘上 %d 子 (空 %d)   轮到 %s"
              % (i + 1, pcs, 64 - pcs, NAME[side]))
        print(draw(black, white, legal, best, played if played is not None else -1))
        print("  图例: X黑 O白 . 空 + 合法点 # 老师最优 @ 实际走的 * 两者相同")
        print("  合法着法(%d): %s"
              % (popcount(legal), ' '.join(pos_to_mv(p) for p in moves_list(legal))))
        print("  老师(L%s) 最优: %-3s   评分 %+d 子  (%s 视角)"
              % (r['L'], r['best'], r['score'], NAME[side]))
        if played is None:
            print("  实际走的: (本盘记录到此为止)")
        else:
            seg = ("随机段, T=%.1f" % float(r['T'])) if float(r['T']) > 0 else "确定段, 走老师最优"
            same = "与老师一致" if played == best else "**与老师不同**"
            print("  实际走的: %-3s   %s   [%s]" % (pos_to_mv(played), same, seg))
            if opp_passed:
                print("  注: 走完之后对手无子可走, PASS, 又轮回同一方")
        print("  局面串(可粘进 Sensei / Edax setboard):")
        print("    %s" % board_str(black, white, side))

        if played is not None:
            side = side if opp_passed else -side

    print("\n" + "=" * 62)
    print("复盘结束。核对要点:")
    print("  1. 每张图里 # 所在的格子, 是否确实是个像样的好手")
    print("  2. 确定段(T=0)的手, @ 和 # 必须重合成 *; 随机段允许不同")
    print("  3. 评分符号: 正数表示**轮到方**领先, 换手之后应当基本反号")
    print("  4. 把局面串粘进 Sensei, 看它给的最优手与 # 是否一致")
    return 0


if __name__ == '__main__':
    sys.exit(main())
