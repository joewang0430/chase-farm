#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分片汇总与校验 —— 数据进训练之前的最后一道闸。

用法:
  python3 farm/collect.py ~/chase_farm/shards                      # 目录
  python3 farm/collect.py data.jsonl.gz                            # 单文件, 支持 .gz
  python3 farm/collect.py data.jsonl.gz --sample 20000             # B/B3 只抽 2 万盘
  python3 farm/collect.py data.jsonl.gz --verify 2000 --engine <edax>
  python3 farm/collect.py data.jsonl.gz --out clean.jsonl          # 写出对称去重后的数据

**流式读取**: 2033 万条记录若一次性读进内存约需 14GB, 会把 16GB 的机器打爆(实测踩过)。
所以改成两遍扫描, 常驻内存只有汇总量:
  第一遍 —— A(逐条) + B2(逐条) + C(计数器), 只记统计量, 不留记录
  第二遍 —— 只把抽中的那些**整盘**读进内存, 做 B(重复) 与 B3(生成逻辑)

检查项:
  A. 结构完整性(硬错误, **全量**)
     A1 必需字段齐全、类型正确
     A2 my 与 opp 无重叠(同一格不能既是我又是对手)
     A3 popcount(my|opp) == pcs
     A4 best 在该局面是合法着法
     A5 pcs 落在 [12,53]
     A6 T 只能是 0 或 Tsched 里出现过的温度
  B2. 完整性(**全量**): 对局序号是否连续 —— 唯一能发现"数据根本没写进去"的检查;
      以及每盘的局面数(>42 不可能, 说明有重复写入)
  B.  重复情况(可抽样): 完全相同 / 四重对称等价 / 按 pcs 拆开 / 标签是否自相矛盾
  B3. 生成逻辑(可抽样): 走法可复原 / 确定段必走老师手 / 随机段确由选择器驱动 /
      **采样形态** —— 每盘的随机点必须是「连续前缀 + 至多一个孤立点」
  C.  分布(**全量**): pcs 覆盖 / 评分 / 温度 / 各工人产量
  D.  抽样回验(可选): 用独立新起的引擎重问, best 与 score 必须逐字段相等

退出码: 0 = 无硬错误; 1 = 有硬错误。
"""

import argparse
import collections
import glob
import gzip
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
ERR_NAMES = ('A1字段缺失或类型错', 'A2 my与opp重叠', 'A3 pcs与位棋盘不符',
             'A4 best不合法', 'A5 pcs越界', 'A6 T不在温度表内')


def open_any(path):
    return gzip.open(path, 'rt') if path.endswith('.gz') else open(path)


def iter_rows(paths):
    """逐行产出记录; 解析失败的行以 None 产出(由调用方计数)。全程不驻留。"""
    for p in paths:
        with open_any(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    yield None


def check_one(r, errs, i, show):
    """对单条记录做 A 类检查, 把错误累进 errs(每类只留 show 条示例)。返回是否有硬错误。"""
    bad = 0

    def rec(name, why):
        errs[name][0] += 1
        if len(errs[name][1]) < show:
            errs[name][1].append((i, why))

    try:
        for k in REQUIRED:
            if k not in r:
                raise KeyError(k)
        my, opp, pcs = int(r['my']), int(r['opp']), int(r['pcs'])
        int(r['score'])
    except Exception as ex:
        rec('A1字段缺失或类型错', str(ex))
        return 1
    if my & opp:
        rec('A2 my与opp重叠', '重叠 %d 格' % popcount(my & opp))
        return 1
    if popcount(my | opp) != pcs:
        rec('A3 pcs与位棋盘不符', '记 %d 实 %d' % (pcs, popcount(my | opp))); bad += 1
    if not (PCS_MIN <= pcs <= PCS_MAX):
        rec('A5 pcs越界', 'pcs=%d' % pcs); bad += 1
    try:
        pos = mv_to_pos(r['best'])
        if not ((legal_moves(my, opp) >> pos) & 1):
            rec('A4 best不合法', 'pcs=%d best=%s' % (pcs, r['best'])); bad += 1
    except Exception as ex:
        rec('A4 best不合法', '解析失败 %s' % ex); bad += 1
    sched = str(r.get('Tsched', ''))
    allowed = set([0.0])
    for part in sched.replace(',', '/').split('/'):
        try:
            allowed.add(float(part))
        except ValueError:
            pass
    if allowed != set([0.0]) and float(r['T']) not in allowed:
        rec('A6 T不在温度表内', 'T=%s 表=%s' % (r['T'], sched)); bad += 1
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('shards', help='分片目录, 或单个 jsonl / jsonl.gz')
    ap.add_argument('--out', default=None, help='写出对称去重后的数据(需配合 --sample 控制内存)')
    ap.add_argument('--verify', type=int, default=0, help='抽多少条用引擎回验标签')
    ap.add_argument('--engine', default=None, help='回验用的引擎路径')
    ap.add_argument('--show', type=int, default=5, help='每类错误最多打印几条')
    ap.add_argument('--sample', type=int, default=0,
                    help='B/B3 只在这么多**盘**上跑(0=全量, 但 2000 万条全量会吃掉十几GB内存)。'
                         'A/B2/C 始终全量 —— 它们是流式的, 不占内存, 而"字段错/丢数据"必须逐条查; '
                         'B3 验的是"要么处处成立、要么系统性失效"的性质, 抽样与全量结论相同。')
    args = ap.parse_args()

    if os.path.isdir(args.shards):
        paths = sorted(glob.glob(os.path.join(args.shards, '*.jsonl')))
    else:
        paths = [args.shards]
    if not paths:
        print("没找到分片: %s" % args.shards)
        return 1

    # ================= 第一遍: A / B2 / C, 全量流式 =================
    print("=" * 72)
    print("第一遍扫描(全量, 流式)...")
    sys.stdout.flush()
    errs = collections.OrderedDict((k, [0, []]) for k in ERR_NAMES)
    bad_json = n_rows = 0
    per_src_g = collections.defaultdict(set)      # 工人 -> 该工人的对局序号集合
    per_game = collections.Counter()              # (工人, 盘) -> 局面数
    by_pcs = collections.Counter()
    abs_hist = collections.Counter()              # |score| -> 条数
    sign = collections.Counter()
    tcnt = collections.Counter()
    per_src = collections.Counter()
    meta_L, meta_SL, meta_T = set(), set(), set()

    for i, r in enumerate(iter_rows(paths)):
        if r is None:
            bad_json += 1
            continue
        n_rows += 1
        check_one(r, errs, i, args.show)
        try:
            key = (r['src'], r['g'])
            per_src_g[r['src']].add(r['g'])
            per_game[key] += 1
            by_pcs[r['pcs']] += 1
            s = int(r['score'])
            abs_hist[abs(s)] += 1
            sign[(s > 0) - (s < 0)] += 1
            tcnt[r['T']] += 1
            per_src[r['src']] += 1
            meta_L.add(r.get('L', '缺')); meta_SL.add(r.get('SL', '缺'))
            meta_T.add(str(r.get('Tsched', '缺')))
        except Exception:
            pass
        if n_rows % 2000000 == 0:
            print("    已扫 %s 条..." % "{:,}".format(n_rows)); sys.stdout.flush()

    if not n_rows:
        print("没有可用记录")
        return 1
    n_games = len(per_game)
    hosts = {s.split('_')[0] for s in per_src}
    print("记录 %s 条%s" % ("{:,}".format(n_rows),
                          (", **JSON 解析失败 %d 行**" % bad_json) if bad_json else ""))
    print("对局 %s 盘, 平均 %.2f 局面/盘, 来自 %d 台机器 / %d 个工人"
          % ("{:,}".format(n_games), n_rows / float(n_games), len(hosts), len(per_src)))
    print("老师档位 %s   选择器档位 %s   温度策略 %s"
          % (sorted(meta_L), sorted(meta_SL), sorted(meta_T)))

    n_hard = bad_json

    # ---------- A ----------
    print("\n" + "=" * 72)
    print("A. 结构完整性(全量, 硬错误)")
    for name in ERR_NAMES:
        cnt, ex = errs[name]
        n_hard += cnt
        print("  %-22s %8d 条  %s" % (name, cnt, "PASS" if not cnt else "**FAIL**"))
        for i, why in ex:
            print("        #%d %s" % (i, why))

    # ---------- B2 ----------
    # 工人的 g 从 0 连续递增, 每盘写完即 flush。若 NFS 丢了写入, g 序列会出现空洞 ——
    # 这是唯一能发现"数据根本没写进去"的检查, 其余检查只能查已存在的数据对不对。
    print("\n" + "=" * 72)
    print("B2. 完整性(全量): 每个工人的对局序号是否连续")
    holes = []
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
    short = sum(1 for v in per_game.values() if v < 42)
    over = sum(1 for v in per_game.values() if v > 42)
    print("  每盘局面数: =42 的 %s 盘, <42 的 %d 盘(提前终局, 正常), >42 的 %d 盘  %s"
          % ("{:,}".format(n_games - short - over), short, over,
             "PASS" if over == 0 else "**FAIL** (>42 不可能, 说明有重复写入)"))
    n_hard += over

    # ================= 选样, 第二遍只读这些盘 =================
    keep = None
    if args.sample and args.sample < n_games:
        rng0 = random.Random(20260830)
        keep = set(rng0.sample(sorted(per_game), args.sample))
        print("\n(B/B3 抽样: %s 盘 / 共 %s 盘; A、B2、C 已全量完成)"
              % ("{:,}".format(args.sample), "{:,}".format(n_games)))
    print("\n第二遍扫描(取抽中的对局)...")
    sys.stdout.flush()
    by_game = collections.defaultdict(list)
    for r in iter_rows(paths):
        if r is None:
            continue
        k = (r['src'], r['g'])
        if keep is None or k in keep:
            by_game[k].append(r)
    brows_n = sum(len(v) for v in by_game.values())

    # ---------- B 重复 ----------
    print("\n" + "=" * 72)
    print("B. 重复情况(%s 条)" % "{:,}".format(brows_n))
    exact = collections.defaultdict(list)
    canon_ct = collections.Counter()
    canon_by_pcs = collections.defaultdict(set)
    cnt_by_pcs = collections.Counter()
    for recs in by_game.values():
        for r in recs:
            my, opp = int(r['my']), int(r['opp'])
            exact[(my, opp)].append((r['best'], r['score']))
            c = canonical(my, opp)
            canon_ct[c] += 1
            canon_by_pcs[r['pcs']].add(c)
            cnt_by_pcs[r['pcs']] += 1
    print("  完全相同的局面: %s 个不同局面, 去重率 %.2f%%"
          % ("{:,}".format(len(exact)), (1 - len(exact) / float(brows_n)) * 100))
    print("  四重对称等价后: %s 个不同局面, 去重率 %.2f%%"
          % ("{:,}".format(len(canon_ct)), (1 - len(canon_ct) / float(brows_n)) * 100))
    # 按 pcs 拆开: 开局池只有 6.7 万条, 所以 pcs 12 附近的局面池是**有限**的 ——
    # 对局数远超开局池规模时, 低 pcs 段必然大量重复。这是设计的必然, 不是 bug,
    # 但它意味着 early 段的多样性有天花板。
    print("  按 pcs 的对称去重率(只列非零的档):")
    any_dup = False
    for pcs in sorted(cnt_by_pcs):
        n, u = cnt_by_pcs[pcs], len(canon_by_pcs[pcs])
        if n > u:
            any_dup = True
            print("        pcs=%-3d %8d 条 -> %8d 个不同局面  重复 %.2f%%"
                  % (pcs, n, u, (1 - u / float(n)) * 100))
    if not any_dup:
        print("        (各档均无重复)")
    conflict = [(k, sorted(set(v))[:3], len(v))
                for k, v in exact.items() if len(set(v)) > 1]
    print("  **同一局面标签自相矛盾**: %d 处  %s"
          % (len(conflict), "PASS" if not conflict else "**FAIL**"))
    for k, labs, n in conflict[:args.show]:
        print("        出现%d次, 标签有 %s" % (n, labs))
    n_hard += len(conflict)

    # ---------- B3 生成逻辑 ----------
    print("\n" + "=" * 72)
    print("B3. 生成逻辑(%s 盘)" % "{:,}".format(len(by_game)))
    n_link = n_link_bad = n_det = n_det_ok = n_rand = n_rand_diff = 0
    bad_ex = []
    shape_bad = []
    n_extra0 = n_extra1 = 0
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
        # 采样形态: T 序列必须是「连续前缀 T>0」+「确定段 T=0」+「至多一个孤立 T>0」。
        # 老方案(2026-08-30 之前)没有那个孤立点, 孤立点数为 0 —— 不是错误, 分开计数。
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

    print("  相邻记录 %s 组, 复原失败 %d 组  %s"
          % ("{:,}".format(n_link), n_link_bad, "PASS" if not n_link_bad else "**FAIL**"))
    for key, p1, p2 in bad_ex:
        print("        %s g=%s  pcs %d -> %d" % (key[0], key[1], p1, p2))
    n_hard += n_link_bad
    det_bad = n_det - n_det_ok
    print("  确定段(T=0) %s 条, 实走 != 老师best 的 %d 条  %s"
          % ("{:,}".format(n_det), det_bad, "PASS" if det_bad == 0 else "**FAIL**"))
    n_hard += det_bad
    tot_g = max(1, len(by_game))
    print("  采样形态: 无额外偏离点 %s 盘(老方案/采到最优手), 恰好一个 %s 盘(%.1f%%), "
          "**两个以上 %d 盘**  %s"
          % ("{:,}".format(n_extra0), "{:,}".format(n_extra1), n_extra1 / float(tot_g) * 100,
             len(shape_bad), "PASS" if not shape_bad else "**FAIL**"))
    for key, pcslist in shape_bad[:args.show]:
        print("        %s g=%s 确定段里有 %d 个随机点: %s"
              % (key[0], key[1], len(pcslist), pcslist))
    n_hard += len(shape_bad)
    if n_rand:
        pr = n_rand_diff / float(n_rand)
        ok = 0.02 < pr < 0.98
        print("  随机段(T>0) %s 条, 实走 != 老师best 占 %.1f%%  %s"
              % ("{:,}".format(n_rand), pr * 100,
                 "PASS" if ok else "**FAIL**(0%=选择器没生效, 100%=老师被绕开)"))
        n_hard += (not ok)

    # ---------- C 分布(全量) ----------
    print("\n" + "=" * 72)
    print("C. 分布(全量)")
    exp = n_rows / float(PCS_MAX - PCS_MIN + 1)
    lo_c, hi_c = min(by_pcs.values()), max(by_pcs.values())
    print("  C1 pcs 覆盖 %d/%d 档, 每档期望 %.0f 条, 实际 %s~%s (偏离 -%.1f%% ~ +%.1f%%)"
          % (len(by_pcs), PCS_MAX - PCS_MIN + 1, exp, "{:,}".format(lo_c),
             "{:,}".format(hi_c), (1 - lo_c / exp) * 100, (hi_c / exp - 1) * 100))
    miss = [p for p in range(PCS_MIN, PCS_MAX + 1) if p not in by_pcs]
    if miss:
        print("        **缺失的 pcs**: %s" % miss)
    tot = sum(abs_hist.values())
    acc, med = 0, 0
    for v in sorted(abs_hist):
        acc += abs_hist[v]
        if acc >= tot // 2:
            med = v
            break
    mean = sum(v * c for v, c in abs_hist.items()) / float(tot)
    blow = sum(c for v, c in abs_hist.items() if v >= 24) / float(tot) * 100
    print("  C2 |score| 中位 %d 均值 %.1f, 一边倒(>=24) %.1f%%, 正/负/零 %.1f/%.1f/%.1f%%"
          % (med, mean, blow, sign[1] / float(tot) * 100,
             sign[-1] / float(tot) * 100, sign[0] / float(tot) * 100))
    print("  C3 温度分布: " + ", ".join(
        "T=%s %.1f%%" % (t, c / float(n_rows) * 100) for t, c in sorted(tcnt.items())))
    v = sorted(per_src.values())
    print("  C4 工人产量: 最少 %d, 中位 %d, 最多 %d" % (v[0], v[len(v) // 2], v[-1]))

    # ---------- D 抽样回验 ----------
    if args.verify:
        print("\n" + "=" * 72)
        print("D. 抽样回验(用独立新起的引擎重问 hint 1)")
        if not args.engine:
            print("  跳过: 没给 --engine")
        else:
            from worker import Edax, TEACHER_HINT
            pool = [r for recs in by_game.values() for r in recs]
            lvl = sorted({r['L'] for r in pool})[0]
            e = Edax(args.engine, level=lvl)
            rng = random.Random(20260829)
            sample = rng.sample(pool, min(args.verify, len(pool)))
            bad_b = bad_s = no_resp = 0
            for j, r in enumerate(sample):
                got = e.hint(int(r['my']), int(r['opp']), TEACHER_HINT)
                if not got:
                    no_resp += 1
                    continue
                if got[0][0] != mv_to_pos(r['best']):
                    bad_b += 1
                if got[0][1] != r['score']:
                    bad_s += 1
                if (j + 1) % 200 == 0:
                    print("    已验 %d/%d ..." % (j + 1, len(sample))); sys.stdout.flush()
            e.close()
            ok = (bad_b == 0 and bad_s == 0)
            print("  L%d 抽查 %d 条: best 不符 %d, score 不符 %d, 无响应 %d  %s"
                  % (lvl, len(sample), bad_b, bad_s, no_resp, "PASS" if ok else "**FAIL**"))
            if ok:
                print("     -> 标签错误率的 95%% 置信上界 %.2f%% (三倍法则)"
                      % (3.0 / len(sample) * 100))
            n_hard += bad_b + bad_s

    # ---------- 写出 ----------
    if args.out:
        seen = set()
        n_out = 0
        with open(args.out, 'w') as f:
            for recs in by_game.values():
                for r in recs:
                    c = canonical(int(r['my']), int(r['opp']))
                    if c in seen:
                        continue
                    seen.add(c)
                    f.write(json.dumps(r, separators=(',', ':')) + "\n")
                    n_out += 1
        print("\n已写出对称去重数据: %s (%s 条)" % (args.out, "{:,}".format(n_out)))

    print("\n" + "=" * 72)
    print("硬错误合计: %d  ->  %s" % (n_hard, "数据可用" if n_hard == 0 else "**数据有问题, 不要用**"))
    return 0 if n_hard == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
