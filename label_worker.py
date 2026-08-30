#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标注工人 —— 给一堆现成的局面用 Edax L21 打标签。

用法:
  python3 farm/label_worker.py --engine <lEdax> --tasks <任务目录> --out <输出目录> \\
      --machine 0 --machines 4 [--workers 24]

和 worker.py 的区别: 这里**没有对局逻辑、没有选择器、没有开局池**。
输入是现成的局面列表, 输出是同样的局面加上 best 与 score 两个字段。

分片与断点续跑:
  任务目录里是 part_0000.jsonl ... part_0199.jsonl。
  第 i 台机器(共 N 台)只处理 编号 % N == i 的那些片。
  每片单独输出 part_XXXX.done.jsonl, **输出已存在的片直接跳过** ——
  云上抢占式实例随时可能被回收, 重启后接着跑即可, 不会重复劳动。

进程内并发:
  一个进程一个 Edax(常驻约 55MB), --workers 决定起多少个子进程。
  子进程之间再按 编号 % workers 分片, 与机器级分片正交。
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from worker import Edax, TEACHER_HINT, TEACHER_LEVEL   # noqa: E402
from othello import pos_to_mv                          # noqa: E402


def label_shard(engine, src, dst, level, log):
    """标注一个分片。先写临时文件再改名 —— 半截文件不会被当成已完成。"""
    rows = [json.loads(l) for l in open(src) if l.strip()]
    tmp = dst + ".tmp"
    t0 = time.time()
    last_log = t0
    fails = 0
    with open(tmp, 'w') as f:
        for i, r in enumerate(rows):
            my, opp = int(r['my']), int(r['opp'])
            got = engine.hint(my, opp, TEACHER_HINT)
            if not got:
                # hint 内部已就地重试 3 次; 仍失败就重启引擎再试一次
                engine.restart()
                got = engine.hint(my, opp, TEACHER_HINT)
                if not got:
                    fails += 1
                    continue
            pos, sc = got[0]
            r['best'] = pos_to_mv(pos)
            r['score'] = sc
            r['L'] = level
            f.write(json.dumps(r, separators=(',', ':')) + "\n")
            # 按**时间**打进度而不是按条数: 条数间隔在少核机器上可能几十分钟不出声,
            # 看着像死机(实测在 1 核机器上踩过)。
            now = time.time()
            if now - last_log >= 60:
                last_log = now
                el = now - t0
                log("    %s: %d/%d  %.1f分  %.3f秒/局面"
                    % (os.path.basename(src), i + 1, len(rows), el / 60, el / (i + 1)))
    os.rename(tmp, dst)
    el = time.time() - t0
    log("  完成 %s: %d 局面, %.1f 分, %.3f 秒/局面%s"
        % (os.path.basename(src), len(rows) - fails, el / 60, el / max(1, len(rows)),
           (", **失败 %d 条**" % fails) if fails else ""))
    return len(rows) - fails, fails


def run_one(args, sub):
    """一个子进程: 处理 (编号 % machines == machine) 且 (片序 % workers == sub) 的分片。"""
    def log(msg):
        sys.stderr.write("[w%d] %s\n" % (sub, msg))
        sys.stderr.flush()

    parts = sorted(glob.glob(os.path.join(args.tasks, "part_*.jsonl")))

    def part_idx(path):
        return int(os.path.basename(path).split('_')[1].split('.')[0])

    # 两级分片, 互相正交: 先按机器(编号 % machines), 再按本机的子进程(名次 % workers)
    this_machine = [p for p in parts if part_idx(p) % args.machines == args.machine]
    mine = [p for k, p in enumerate(this_machine) if k % args.workers == sub]

    todo = []
    for p in mine:
        idx = os.path.basename(p).replace('.jsonl', '')
        dst = os.path.join(args.out, idx + ".done.jsonl")
        if os.path.exists(dst):
            continue                        # 断点续跑: 已完成的片直接跳过
        todo.append((p, dst))
    log("分到 %d 片, 待做 %d 片" % (len(mine), len(todo)))
    if not todo:
        return
    engine = Edax(args.engine, level=args.level)
    n_ok = n_bad = 0
    for src, dst in todo:
        a, b = label_shard(engine, src, dst, args.level, log)
        n_ok += a; n_bad += b
    engine.close()
    log("收工: 标注 %d 局面, 失败 %d" % (n_ok, n_bad))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--engine', required=True)
    ap.add_argument('--tasks', required=True, help='存放 part_*.jsonl 的目录')
    ap.add_argument('--out', required=True)
    ap.add_argument('--machine', type=int, default=0, help='本机是第几台(0 起)')
    ap.add_argument('--machines', type=int, default=1, help='共几台')
    ap.add_argument('--workers', type=int, default=0, help='本机起几个进程(0=核数-2)')
    ap.add_argument('--level', type=int, default=TEACHER_LEVEL)
    ap.add_argument('--sub', type=int, default=-1, help='内部用: 子进程编号')
    args = ap.parse_args()

    if args.workers == 0:
        try:
            args.workers = max(1, os.cpu_count() - 2)
        except Exception:
            args.workers = 4
    if not os.path.isdir(args.out):
        os.makedirs(args.out)

    if args.sub >= 0:
        run_one(args, args.sub)
        return 0

    # 父进程: 起 workers 个子进程, 各自认领一部分分片
    sys.stderr.write("本机 %d/%d, 起 %d 个进程, 老师 L%d\n"
                     % (args.machine, args.machines, args.workers, args.level))
    procs = []
    for s in range(args.workers):
        procs.append(subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             '--engine', args.engine, '--tasks', args.tasks, '--out', args.out,
             '--machine', str(args.machine), '--machines', str(args.machines),
             '--workers', str(args.workers), '--level', str(args.level),
             '--sub', str(s)]))
    rc = 0
    for p in procs:
        rc |= p.wait()
    done = len(glob.glob(os.path.join(args.out, "*.done.jsonl")))
    sys.stderr.write("本机结束, 已完成分片 %d 个\n" % done)
    return rc


if __name__ == '__main__':
    sys.exit(main())
