#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker.py 双引擎设计的自动检验。

用法: python3 farm/selftest.py --engine <mEdax 路径> --openings <池> [--games 6]

它不"读代码看对不对", 而是**从产出的数据反推**, 检查五件只有实现正确才可能成立的事:

  检验1 走法可复原: 同一盘相邻两条记录的盘面之间, 必须恰好隔一步合法着法(允许其后有 PASS)。
        任何盘面/视角/换边的错误都会让复原失败。
  检验2 确定段必走老师手: 凡 T=0 的记录(已走出随机段), 复原出来的那一步**必须等于** best。
        若误把选择器的着法用在确定段, 此检验必炸。
  检验3 随机段确实由选择器驱动: T>0 的记录里, 实走 != best 的比例须显著大于 0
        (若为 0, 说明选择器没生效或被老师覆盖)且显著小于 1(若接近均匀, 说明温度没生效)。
  检验4 标签溯源: 随机抽若干记录, 用**独立新起的 Edax L15 引擎**重问 hint 1,
        best 与 score 必须逐字段相等。若标签被选择器(L5)污染, 此检验必炸。
  检验5 字段自洽: pcs 落在 [12,53]; T 与该 pcs 所属阶段的温度一致; L/SL 与命令行一致。

任何一项失败即以非零码退出。
"""

import argparse
import collections
import json
import os
import random
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from othello import legal_moves, apply_move, popcount, mv_to_pos   # noqa: E402
from worker import (Edax, stage_temp, parse_temps,            # noqa: E402
                    TEACHER_HINT, PCS_MIN, PCS_MAX)


def successors(my, opp):
    """返回 {下一条记录的(my,opp): 走的那一步}。含"对手须 PASS 则轮回自己"的分支。"""
    out = {}
    for pos in range(64):
        if not ((legal_moves(my, opp) >> pos) & 1):
            continue
        nm, no = apply_move(my, opp, pos)
        if legal_moves(no, nm):
            nxt = (no, nm)          # 正常换边
        elif legal_moves(nm, no):
            nxt = (nm, no)          # 对手无子可走, PASS 回来
        else:
            continue                # 双方都走不了 = 终局, 不会再有记录
        out.setdefault(nxt, []).append(pos)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--engine', required=True)
    ap.add_argument('--openings', required=True)
    ap.add_argument('--games', type=int, default=6)
    ap.add_argument('--level', type=int, default=15)
    ap.add_argument('--sel-level', type=int, default=5)
    ap.add_argument('--temperature', default='6')
    ap.add_argument('--relabel', type=int, default=25, help='检验4 抽查多少条')
    args = ap.parse_args()

    out_dir = tempfile.mkdtemp(prefix='selftest_')
    cmd = [sys.executable, os.path.join(HERE, 'worker.py'),
           '--engine', args.engine, '--openings', args.openings,
           '--out-dir', out_dir, '--games', str(args.games), '--seed', '20260829',
           '--level', str(args.level), '--sel-level', str(args.sel_level),
           '--temperature', args.temperature]
    sys.stderr.write("生成 %d 盘用于检验...\n" % args.games)
    subprocess.check_call(cmd)

    rows = []
    for fn in sorted(os.listdir(out_dir)):
        if fn.endswith('.jsonl'):
            with open(os.path.join(out_dir, fn)) as f:
                rows += [json.loads(l) for l in f if l.strip()]
    if not rows:
        print("**失败**: 没有产出任何记录"); return 1
    print("拿到 %d 条记录 / %d 盘\n" % (rows and len(rows), len({r['g'] for r in rows})))

    temps = parse_temps(args.temperature)
    games = collections.defaultdict(list)
    for r in rows:
        games[r['g']].append(r)

    fail = 0

    # ---------- 检验1 + 2 + 3 ----------
    n_link = n_det = n_det_ok = n_rand = n_rand_diff = 0
    link_bad = []
    for g, recs in games.items():
        recs.sort(key=lambda r: r['pcs'])
        for a, b in zip(recs, recs[1:]):
            my, opp = int(a['my']), int(a['opp'])
            nxt = (int(b['my']), int(b['opp']))
            succ = successors(my, opp)
            n_link += 1
            if nxt not in succ:
                link_bad.append((g, a['pcs'], b['pcs']))
                continue
            played = succ[nxt]
            best = mv_to_pos(a['best'])
            if a['T'] == 0.0:
                n_det += 1
                # 确定段: 走的必须是老师的 best。若有多步通向同一盘面, best 在其中即可
                n_det_ok += (best in played)
            else:
                n_rand += 1
                n_rand_diff += (best not in played)

    print("=== 检验1 走法可复原 ===")
    print("  相邻记录对 %d 组, 复原失败 %d 组 -> %s"
          % (n_link, len(link_bad), "PASS" if not link_bad else "**FAIL** " + str(link_bad[:5])))
    fail += bool(link_bad)

    print("\n=== 检验2 确定段(T=0)必走老师的 best ===")
    if n_det == 0:
        print("  样本为 0(本次抽到的随机终点都很靠后), **无法判定** —— 请加大 --games 重跑")
        fail += 1
    else:
        print("  T=0 记录 %d 条, 实走==best 的 %d 条 -> %s"
              % (n_det, n_det_ok, "PASS" if n_det_ok == n_det else "**FAIL**"))
        fail += (n_det_ok != n_det)

    print("\n=== 检验3 随机段确由选择器驱动 ===")
    if n_rand == 0:
        print("  样本为 0, **无法判定**"); fail += 1
    else:
        p = n_rand_diff / float(n_rand)
        ok = 0.02 < p < 0.98
        print("  T>0 记录 %d 条, 实走 != 老师best 的占 %.1f%% -> %s"
              % (n_rand, p * 100, "PASS" if ok else "**FAIL**(0%=选择器没生效, 100%=老师被绕开)"))
        fail += (not ok)

    # ---------- 检验4 标签溯源 ----------
    print("\n=== 检验4 标签溯源: 用独立新起的 L%d 引擎重问 hint %d ===" % (args.level, TEACHER_HINT))
    rng = random.Random(7)
    sample = rng.sample(rows, min(args.relabel, len(rows)))
    eng = Edax(args.engine, level=args.level)
    bad_best = bad_score = 0
    for r in sample:
        got = eng.hint(int(r['my']), int(r['opp']), TEACHER_HINT)
        if not got:
            print("  引擎无响应, 该条跳过"); continue
        pos, sc = got[0]
        if pos != mv_to_pos(r['best']):
            bad_best += 1
        if sc != r['score']:
            bad_score += 1
    eng.close()
    print("  抽查 %d 条: best 不符 %d 条, score 不符 %d 条 -> %s"
          % (len(sample), bad_best, bad_score,
             "PASS" if bad_best == 0 and bad_score == 0 else "**FAIL**"))
    fail += (bad_best or bad_score)

    # ---------- 检验5 字段自洽 ----------
    print("\n=== 检验5 字段自洽 ===")
    e_pcs = [r for r in rows if not (PCS_MIN <= r['pcs'] <= PCS_MAX)]
    e_t = [r for r in rows if r['T'] not in (0.0, stage_temp(r['pcs'], temps))]
    e_l = [r for r in rows if r['L'] != args.level or r['SL'] != args.sel_level]
    for name, bad in (("pcs 越界", e_pcs), ("T 与阶段不符", e_t), ("L/SL 与命令行不符", e_l)):
        print("  %-16s %d 条 -> %s" % (name, len(bad), "PASS" if not bad else "**FAIL**"))
        fail += bool(bad)

    print("\n" + ("全部检验通过" if not fail else "**有 %d 项失败**" % fail))
    return 0 if not fail else 1


if __name__ == '__main__':
    sys.exit(main())
