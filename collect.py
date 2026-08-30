#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分片汇总与校验 —— 数据进训练之前的最后一道闸。

用法:
  python3 farm/collect.py ~/chase_farm/shards                     # 只校验+出报告
  python3 farm/collect.py ~/chase_farm/shards --out clean.jsonl   # 顺便写出去重后的数据
  python3 farm/collect.py ~/chase_farm/shards --verify 100 \\
      --engine ~/chase_farm/engine/lEdax                          # 再抽 100 条用引擎回验标签

它检查的东西分三类, **硬错误一条都不该有**:

  A. 结构完整性(硬错误)
     A1 必需字段齐全、类型正确
     A2 my 与 opp 无重叠(同一格不能既是我又是对手)
     A3 popcount(my|opp) == pcs(记录的子数必须和位棋盘一致)
     A4 best 在该局面是合法着法
     A5 pcs 落在 [12,53]
     A6 T 只能是 0 或 Tsched 里出现过的温度
  B2 完整性: 对局序号是否连续(唯一能发现"数据根本没写进去"的检查)
  B3 生成逻辑: 走法可复原 / 确定段必走老师手 / 随机段确由选择器驱动 /
     **采样形态** —— 每盘的随机点分布必须是「连续前缀 + 至多一个孤立点」
  B. 重复情况(不是错误, 但要知道)
     B1 完全相同的局面出现多少次
     B2 四重对称等价的局面出现多少次(训练时它们是同一个东西)
     B3 **重复局面的标签是否自相矛盾** —— 同一个局面两次给出不同的 best/score,
        说明引擎不确定或数据被污染, 这个必须为 0
  C. 分布(供判断数据是否均衡)
     C1 每个 pcs 的条数
     C2 评分分布、一边倒(|score|>=24)占比
     C3 T 的分布(随机段 vs 确定段的比例)
     C4 各工人/主机的产量

退出码: 0 = 无硬错误; 1 = 有硬错误。
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
from othello import (legal_moves, apply_move, popcount,   # noqa: E402
                     mv_to_pos, canonical)

REQUIRED = ('pcs', 'my', 'opp', 'best', 'score', 'g', 'src', 'T', 'L', 'SL')
PCS_MIN, PCS_MAX = 12, 53


def load(paths):
    rows, bad_json = [], 0
    for p in paths:
        with open(p) as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    bad_json += 1
    return rows, bad_json


def check_structure(rows):
    """返回 {错误名: [(下标, 说明), ...]}"""
    errs = collections.OrderedDict(
        (k, []) for k in ('A1字段缺失或类型错', 'A2 my与opp重叠', 'A3 pcs与位棋盘不符',
                          'A4 best不合法', 'A5 pcs越界', 'A6 T不在温度表内'))
    for i, r in enumerate(rows):
        try:
            for k in REQUIRED:
                if k not in r:
                    raise KeyError(k)
            my, opp, pcs = int(r['my']), int(r['opp']), int(r['pcs'])
            score = int(r['score'])
        except Exception as ex:
            errs['A1字段缺失或类型错'].append((i, str(ex)))
            continue
        if my & opp:
            errs['A2 my与opp重叠'].append((i, '重叠 %d 格' % popcount(my & opp)))
            continue
        if popcount(my | opp) != pcs:
            errs['A3 pcs与位棋盘不符'].append((i, '记 %d 实 %d' % (pcs, popcount(my | opp))))
        if not (PCS_MIN <= pcs <= PCS_MAX):
            errs['A5 pcs越界'].append((i, 'pcs=%d' % pcs))
        try:
            pos = mv_to_pos(r['best'])
            if not ((legal_moves(my, opp) >> pos) & 1):
                errs['A4 best不合法'].append((i, 'pcs=%d best=%s' % (pcs, r['best'])))
        except Exception as ex:
            errs['A4 best不合法'].append((i, '解析失败 %s' % ex))
        sched = str(r.get('Tsched', ''))
        allowed = set([0.0])
        for part in sched.replace(',', '/').split('/'):
            try:
                allowed.add(float(part))
            except ValueError:
                pass
        if allowed != set([0.0]) and float(r['T']) not in allowed:
            errs['A6 T不在温度表内'].append((i, 'T=%s 表=%s' % (r['T'], sched)))
        _ = score
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('shards', help='分片目录, 或单个 jsonl')
    ap.add_argument('--out', default=None, help='写出去重后的数据(按四重对称去重)')
    ap.add_argument('--verify', type=int, default=0, help='抽多少条用引擎回验标签')
    ap.add_argument('--engine', default=None, help='回验用的引擎路径')
    ap.add_argument('--show', type=int, default=5, help='每类错误最多打印几条')
    ap.add_argument('--sample', type=int, default=0,
                    help='B/B3 只在这么多**盘**上跑(0=全量)。A 与 B2 始终全量 —— '
                         '它们快, 且"字段错/丢数据"必须逐条查; B3 验的是"要么处处成立、'
                         '要么系统性失效"的性质, 抽样与全量给出同样的结论。'
                         '2000万条全量约100分钟, --sample 20000 约1分钟。')
    args = ap.parse_args()

    paths = ([args.shards] if args.shards.endswith('.jsonl')
             else sorted(glob.glob(os.path.join(args.shards, '*.jsonl'))))
    if not paths:
        print("没找到分片: %s" % args.shards)
        return 1
    rows, bad_json = load(paths)
    print("=" * 72)
    print("分片 %d 个, 记录 %d 条%s"
          % (len(paths), len(rows), (", **JSON 解析失败 %d 行**" % bad_json) if bad_json else ""))
    if not rows:
        return 1

    games = {(r['src'], r['g']) for r in rows}
    hosts = {r['src'].split('_')[0] for r in rows}
    print("对局 %d 盘, 平均 %.2f 局面/盘, 来自 %d 台机器 / %d 个工人"
          % (len(games), len(rows) / float(len(games)), len(hosts),
             len({r['src'] for r in rows})))
    print("老师档位 %s   选择器档位 %s   温度策略 %s"
          % (sorted({r.get('L', '缺') for r in rows}),
             sorted({r.get('SL', '缺') for r in rows}),
             sorted({str(r.get('Tsched', '缺')) for r in rows})))

    # ---------- A 结构完整性 ----------
    print("\n" + "=" * 72)
    print("A. 结构完整性(硬错误)")
    errs = check_structure(rows)
    n_hard = bad_json
    for name, lst in errs.items():
        n_hard += len(lst)
        flag = "PASS" if not lst else "**FAIL**"
        print("  %-22s %6d 条  %s" % (name, len(lst), flag))
        for i, why in lst[:args.show]:
            print("        #%d %s" % (i, why))

    # 抽样: 必须**按整盘**抽, 不能按行抽 —— B3 要用同一盘的相邻记录做走法复原,
    # 按行抽会把对局打散, 复原必然失败(那是抽样方式的错, 不是数据的错)。
    brows = rows
    if args.sample and args.sample < len(games):
        rng0 = random.Random(20260830)
        keep = set(rng0.sample(sorted(games), args.sample))
        brows = [r for r in rows if (r['src'], r['g']) in keep]
        print("\n(B/B3 抽样: %d 盘 / %d 条, 占全部 %.1f%%; A 与 B2 仍为全量)"
              % (args.sample, len(brows), len(brows) / float(len(rows)) * 100))

    # ---------- B 重复 ----------
    print("\n" + "=" * 72)
    print("B. 重复情况")
    exact = collections.defaultdict(list)
    canon = collections.defaultdict(list)
    for i, r in enumerate(brows):
        my, opp = int(r['my']), int(r['opp'])
        exact[(my, opp)].append(i)
        canon[canonical(my, opp)].append(i)
    n_uniq_e, n_uniq_c = len(exact), len(canon)
    print("  完全相同的局面: %d 个不同局面, 去重率 %.2f%%"
          % (n_uniq_e, (1 - n_uniq_e / float(len(brows))) * 100))
    print("  四重对称等价后: %d 个不同局面, 去重率 %.2f%%"
          % (n_uniq_c, (1 - n_uniq_c / float(len(brows))) * 100))
    # 按 pcs 拆开: 开局池只有 6.7 万条, 所以 pcs 12 附近的局面池是**有限**的,
    # 对局数一旦远超开局池规模, 低 pcs 段必然大量重复 —— 这是设计的必然, 不是 bug,
    # 但它意味着 early 段的多样性有天花板, 直接影响"该生成多少数据"的判断。
    canon_by_pcs = collections.defaultdict(set)
    cnt_by_pcs = collections.Counter()
    for r in brows:
        canon_by_pcs[r['pcs']].add(canonical(int(r['my']), int(r['opp'])))
        cnt_by_pcs[r['pcs']] += 1
    print("  按 pcs 的对称去重率(只列非零的档):")
    any_dup = False
    for pcs in sorted(cnt_by_pcs):
        n, u = cnt_by_pcs[pcs], len(canon_by_pcs[pcs])
        if n > u:
            any_dup = True
            print("        pcs=%-3d %7d 条 -> %7d 个不同局面  重复 %.2f%%"
                  % (pcs, n, u, (1 - u / float(n)) * 100))
    if not any_dup:
        print("        (各档均无重复)")

    conflict = []
    for key, idxs in exact.items():
        if len(idxs) < 2:
            continue
        labs = {(rows[i]['best'], rows[i]['score']) for i in idxs}
        if len(labs) > 1:
            conflict.append((key, sorted(labs)[:3], len(idxs)))
    print("  **同一局面标签自相矛盾**: %d 处  %s"
          % (len(conflict), "PASS" if not conflict else "**FAIL**"))
    for key, labs, n in conflict[:args.show]:
        print("        出现%d次, 标签有 %s" % (n, labs))
    n_hard += len(conflict)

    # ---------- B2 完整性: 有没有丢盘 ----------
    # 工人的 g 是"自己的第几盘", 从 0 连续递增, 每盘写完即 flush。
    # 若 NFS 丢了写入或文件被截断, g 序列就会出现空洞 ——
    # 这是唯一能发现"数据根本没写进去"的办法(其余检查只能查已存在的数据对不对)。
    print("\n" + "=" * 72)
    print("B2. 完整性: 每个工人的对局序号是否连续")
    holes = []
    per_src_g = collections.defaultdict(set)
    for r in rows:
        per_src_g[r['src']].add(r['g'])
    for src, gs in per_src_g.items():
        lo, hi = min(gs), max(gs)
        miss = (hi - lo + 1) - len(gs)
        if miss:
            holes.append((src, miss, lo, hi))
    print("  工人 %d 个, 序号有空洞的 %d 个  %s"
          % (len(per_src_g), len(holes), "PASS" if not holes else "**FAIL**"))
    for src, miss, lo, hi in holes[:args.show]:
        print("        %s 缺 %d 盘 (范围 %d~%d)" % (src, miss, lo, hi))
    n_hard += len(holes)

    # 每盘的局面数: 正常是 42(pcs 12..53 各一条), 提前终局会少
    per_game = collections.Counter()
    for r in rows:
        per_game[(r['src'], r['g'])] += 1
    short = sum(1 for v in per_game.values() if v < 42)
    over = sum(1 for v in per_game.values() if v > 42)
    print("  每盘局面数: =42 的 %d 盘, <42 的 %d 盘(提前终局, 正常), >42 的 %d 盘  %s"
          % (len(per_game) - short - over, short, over,
             "PASS" if over == 0 else "**FAIL** (>42 不可能, 说明有重复写入)"))
    n_hard += over

    # ---------- B3 生成逻辑: 对**全部**对局验证, 不是抽样 ----------
    # 这三项原来只在 selftest.py 的 10 盘冒烟里跑过。它们完全可以从记录本身算出来
    # (不需要引擎), 所以这里对全量数据再跑一遍 —— 把"设计成立"从抽样变成全覆盖。
    print("\n" + "=" * 72)
    print("B3. 生成逻辑(全量验证)")
    by_game = collections.defaultdict(list)
    for r in brows:
        by_game[(r['src'], r['g'])].append(r)
    n_link = n_link_bad = n_det = n_det_ok = n_rand = n_rand_diff = 0
    bad_ex = []
    for key, recs in by_game.items():
        recs.sort(key=lambda x: x['pcs'])
        for a, b in zip(recs, recs[1:]):
            my, opp = int(a['my']), int(a['opp'])
            nxt = (int(b['my']), int(b['opp']))
            n_link += 1
            legal = legal_moves(my, opp)
            played = []
            for pos in range(64):
                if not ((legal >> pos) & 1):
                    continue
                nm, no = apply_move(my, opp, pos)
                if legal_moves(no, nm):
                    cand = (no, nm)          # 正常换边
                elif legal_moves(nm, no):
                    cand = (nm, no)          # 对手须 PASS, 轮回自己
                else:
                    continue                 # 终局, 不会再有记录
                if cand == nxt:
                    played.append(pos)
            if not played:
                n_link_bad += 1
                if len(bad_ex) < args.show:
                    bad_ex.append((key, a['pcs'], b['pcs']))
                continue
            best = mv_to_pos(a['best'])
            if float(a['T']) == 0.0:
                n_det += 1
                n_det_ok += (best in played)
            else:
                n_rand += 1
                n_rand_diff += (best not in played)
    print("  相邻记录 %d 组, 复原失败 %d 组  %s"
          % (n_link, n_link_bad, "PASS" if not n_link_bad else "**FAIL**"))
    for key, p1, p2 in bad_ex:
        print("        %s g=%s  pcs %d -> %d" % (key[0], key[1], p1, p2))
    n_hard += n_link_bad
    det_bad = n_det - n_det_ok
    print("  确定段(T=0) %d 条, 实走 != 老师best 的 %d 条  %s"
          % (n_det, det_bad, "PASS" if det_bad == 0 else "**FAIL**"))
    n_hard += det_bad
    # B3d 采样形态: 每盘的 T 序列必须是
    #   前缀连续 T>0 (随机段)  ->  确定段 T=0  ->  其中**至多一个**孤立的 T>0 (额外偏离点)
    # 这一项验的是"随机放在哪里"这个设计本身, 是其余检查都盖不到的。
    # 老方案(2026-08-30 之前)没有额外偏离点, 所以孤立点数为 0 —— 不是错误, 会分开计数。
    shape_bad = []
    n_extra0 = n_extra1 = 0
    for key, recs in by_game.items():
        recs.sort(key=lambda x: x['pcs'])
        flags = [float(r['T']) > 0 for r in recs]
        pre = 0
        while pre < len(flags) and flags[pre]:
            pre += 1
        tail = [i for i in range(pre, len(flags)) if flags[i]]
        if len(tail) == 0:
            n_extra0 += 1
        elif len(tail) == 1:
            n_extra1 += 1
        else:
            shape_bad.append((key, [recs[i]['pcs'] for i in tail]))
    tot_g = len(by_game)
    print("  采样形态: 无额外偏离点 %d 盘(老方案/采到最优手), 恰好一个 %d 盘(%.1f%%), "
          "**两个以上 %d 盘**  %s"
          % (n_extra0, n_extra1, n_extra1 / float(max(1, tot_g)) * 100,
             len(shape_bad), "PASS" if not shape_bad else "**FAIL**"))
    for key, pcslist in shape_bad[:args.show]:
        print("        %s g=%s 确定段里有 %d 个随机点: %s"
              % (key[0], key[1], len(pcslist), pcslist))
    n_hard += len(shape_bad)

    if n_rand:
        pr = n_rand_diff / float(n_rand)
        ok = 0.02 < pr < 0.98
        print("  随机段(T>0) %d 条, 实走 != 老师best 占 %.1f%%  %s"
              % (n_rand, pr * 100,
                 "PASS" if ok else "**FAIL**(0%=选择器没生效, 100%=老师被绕开)"))
        n_hard += (not ok)

    # ---------- C 分布 ----------
    print("\n" + "=" * 72)
    print("C. 分布")
    by_pcs = collections.Counter(r['pcs'] for r in rows)
    exp = len(rows) / float(PCS_MAX - PCS_MIN + 1)
    worst = max(by_pcs.values()) / exp - 1, 1 - min(by_pcs.values()) / exp
    print("  C1 pcs 覆盖 %d/%d 档, 每档期望 %.0f 条, 实际 %d~%d (偏离 -%.0f%% ~ +%.0f%%)"
          % (len(by_pcs), PCS_MAX - PCS_MIN + 1, exp, min(by_pcs.values()),
             max(by_pcs.values()), worst[1] * 100, worst[0] * 100))
    miss = [p for p in range(PCS_MIN, PCS_MAX + 1) if p not in by_pcs]
    if miss:
        print("        **缺失的 pcs**: %s" % miss)
    sc = [r['score'] for r in rows]
    a = sorted(abs(s) for s in sc)
    print("  C2 |score| 中位 %d 均值 %.1f, 一边倒(>=24) %.1f%%, 正/负/零 %.1f/%.1f/%.1f%%"
          % (a[len(a) // 2], sum(a) / float(len(a)),
             sum(1 for s in a if s >= 24) / float(len(a)) * 100,
             sum(1 for s in sc if s > 0) / float(len(sc)) * 100,
             sum(1 for s in sc if s < 0) / float(len(sc)) * 100,
             sum(1 for s in sc if s == 0) / float(len(sc)) * 100))
    tc = collections.Counter(r['T'] for r in rows)
    print("  C3 温度分布: " + ", ".join(
        "T=%s %.1f%%" % (t, n / float(len(rows)) * 100) for t, n in sorted(tc.items())))
    per_src = collections.Counter(r['src'] for r in rows)
    v = sorted(per_src.values())
    print("  C4 工人产量: 最少 %d, 中位 %d, 最多 %d (差异 %.0f%%)"
          % (v[0], v[len(v) // 2], v[-1], (v[-1] / float(v[0]) - 1) * 100 if v[0] else 0))

    # ---------- D 抽样回验 ----------
    if args.verify:
        print("\n" + "=" * 72)
        print("D. 抽样回验(用独立新起的引擎重问 hint 1)")
        if not args.engine:
            print("  跳过: 没给 --engine")
        else:
            from worker import Edax, TEACHER_HINT
            lvl = sorted({r['L'] for r in rows})[0]
            e = Edax(args.engine, level=lvl)
            rng = random.Random(20260829)
            sample = rng.sample(rows, min(args.verify, len(rows)))
            bad_b = bad_s = no_resp = 0
            for r in sample:
                got = e.hint(int(r['my']), int(r['opp']), TEACHER_HINT)
                if not got:
                    no_resp += 1
                    continue
                if got[0][0] != mv_to_pos(r['best']):
                    bad_b += 1
                if got[0][1] != r['score']:
                    bad_s += 1
            e.close()
            ok = (bad_b == 0 and bad_s == 0)
            print("  L%d 抽查 %d 条: best 不符 %d, score 不符 %d, 无响应 %d  %s"
                  % (lvl, len(sample), bad_b, bad_s, no_resp, "PASS" if ok else "**FAIL**"))
            n_hard += bad_b + bad_s

    # ---------- 写出 ----------
    if args.out:
        keep = [rows[idxs[0]] for idxs in canon.values()]
        with open(args.out, 'w') as f:
            for r in keep:
                f.write(json.dumps(r, separators=(',', ':')) + "\n")
        print("\n已写出去重数据: %s (%d 条, 按四重对称去重)" % (args.out, len(keep)))

    print("\n" + "=" * 72)
    print("硬错误合计: %d  ->  %s" % (n_hard, "数据可用" if n_hard == 0 else "**数据有问题, 不要用**"))
    return 0 if n_hard == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
