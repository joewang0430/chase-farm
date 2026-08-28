#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chase 标注工人 — 用 Egaroucid 生成带标签的黑白棋训练局面。

纯标准库, Python 3.6 兼容, 无 pip 依赖。

生成方案(design/selfplay_policy_design.md 与 2026-08-28 讨论定稿):
  1. 前 8 手: 从对称去重后的全枚举开局池随机取一条(覆盖整个开局空间)
  2. 随机段: 从 RANDOM_LENGTHS 抽一个终点手数, 段内双方纯随机走(合法着法均匀)
  3. 随机段之后: 双方全程走老师的最优着法, 直到终局
  4. 标签: 每个局面都记老师的最优着法 + 评分(轮到方视角), 与实际走法无关

引擎交互的四个已知坑(全部在此处理, 勿擅自"简化"):
  a. 启动横幅会先于任何响应到达 -> 启动后必须排空
  b. 高档位 hint 表的深度列形如 "21@74%" 而非纯数字 -> 正则须放宽
  c. 发令前队列可能有上条命令的残留 -> 发令前排空
  d. setboard 的响应分多屏陆续到达, 用文本标志判断"结束"会提前截断
     -> 统一改用"静默超时"判定响应到齐
"""

import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from othello import (INIT_BLACK, INIT_WHITE, legal_moves, apply_move,   # noqa: E402
                     popcount, moves_list, pos_to_mv)

# ---------------- 配置 ----------------
LEVEL = 15                  # 老师档位: 900题实测 policy 91.0% / value符号 95.3% / 残局满分, 比L21快4倍
RANDOM_LENGTHS = [12, 16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56]
PCS_MIN = 12                # 只记录 pcs>=12 的局面(与训练分桶 12-25/26-39/40-53 对齐)
PCS_MAX = 53                # pcs>=54 即 empties<=10, 交给残局求解器, 网络不碰
GAMES_PER_SHARD = 200       # 每个分片文件写多少盘后换新文件
HINT_TIMEOUT = 180.0


class Egaroucid(object):
    """Egaroucid 控制台驱动。每次 hint 都是无状态的 setboard + hint。"""

    def __init__(self, exe, level=LEVEL):
        self.exe = exe
        self.level = level
        self.proc = None
        self.q = None
        self._start()

    def _start(self):
        self.proc = subprocess.Popen(
            [self.exe, '-nobook', '-t', '1', '-l', str(self.level), '-mode', '3'],
            cwd=os.path.dirname(self.exe),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, universal_newlines=True, bufsize=1)
        self.q = queue.Queue()
        t = threading.Thread(target=self._reader)
        t.daemon = True
        t.start()
        self._drain(quiet=1.0, hard=15.0)      # 坑a: 排空启动横幅

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self.q.put(line)
        except Exception:
            pass

    def _drain(self, quiet=0.35, hard=8.0):
        """坑c/d: 读到静默 quiet 秒为止, 视为上一条命令的响应已到齐。"""
        t0 = time.time()
        last = time.time()
        while time.time() - t0 < hard:
            try:
                self.q.get(timeout=0.1)
                last = time.time()
            except queue.Empty:
                if time.time() - last > quiet:
                    return

    def _send(self, cmd):
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def hint(self, my, opp):
        """返回 (最优着法pos, 评分) — 均为 my 方(轮到方)视角。失败返回 (None, None)。"""
        board = ['-'] * 64
        for i in range(64):
            if (my >> i) & 1:
                board[i] = 'B'
            elif (opp >> i) & 1:
                board[i] = 'W'
        self._send("setboard " + ''.join(board) + " B")
        self._drain()
        self._send("hint 1")
        t0 = time.time()
        while time.time() - t0 < HINT_TIMEOUT:
            try:
                line = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if not line.strip().startswith('|'):
                continue
            f = [x.strip() for x in line.split('|')]
            # 坑b: f[1]=level, f[2]=深度(可能是 "21@74%"), f[3]=着法, f[4]=评分
            if len(f) > 5 and re.match(r'^[a-h][1-8]$', f[3]) and re.match(r'^[+-]\d+$', f[4]):
                self._drain(quiet=0.2, hard=3.0)
                return (int(f[3][1]) - 1) * 8 + (ord(f[3][0]) - 97), int(f[4])
        return None, None

    def restart(self):
        self.close()
        self._start()

    def close(self):
        try:
            self._send("quit")
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def play_game(engine, rng, openings):
    """下一整盘, 返回 [(pcs, my, opp, best_pos, score), ...](只含 PCS_MIN..PCS_MAX)。
    引擎异常时返回 None, 由调用方重启引擎。"""
    seq = openings[rng.randrange(len(openings))]
    black, white, side = INIT_BLACK, INIT_WHITE, 1
    for pos in seq:
        my, opp = (black, white) if side == 1 else (white, black)
        if not ((legal_moves(my, opp) >> pos) & 1):
            return []                      # 开局池里的坏条目, 跳过这盘
        my, opp = apply_move(my, opp, pos)
        black, white = (my, opp) if side == 1 else (opp, my)
        side = -side

    rand_until = RANDOM_LENGTHS[rng.randrange(len(RANDOM_LENGTHS))]
    ply = len(seq)
    passes = 0
    out = []
    while passes < 2 and popcount(black | white) < 64:
        my, opp = (black, white) if side == 1 else (white, black)
        legal = legal_moves(my, opp)
        if legal == 0:
            passes += 1
            side = -side
            continue
        passes = 0
        pcs = popcount(black | white)
        best, score = engine.hint(my, opp)
        if best is None:
            return None                    # 引擎失联
        if PCS_MIN <= pcs <= PCS_MAX:
            out.append((pcs, my, opp, best, score))
        # 随机段内纯随机, 之后走最优 —— 但标签始终记 best
        mv = moves_list(legal)[rng.randrange(popcount(legal))] if ply < rand_until else best
        my, opp = apply_move(my, opp, mv)
        black, white = (my, opp) if side == 1 else (opp, my)
        side = -side
        ply += 1
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--engine', required=True, help='Egaroucid_for_Console.out 路径')
    ap.add_argument('--openings', required=True, help='8手开局池 jsonl')
    ap.add_argument('--out-dir', required=True, help='分片输出目录(NFS共享)')
    ap.add_argument('--stop-file', default=None, help='此文件存在则收工(默认 out-dir/../STOP)')
    ap.add_argument('--games', type=int, default=0, help='0=无限, 直到 STOP')
    ap.add_argument('--seed', type=int, default=None)
    args = ap.parse_args()

    stop_file = args.stop_file or os.path.join(os.path.dirname(args.out_dir.rstrip('/')), 'STOP')
    host = os.uname()[1].split('.')[0]
    pid = os.getpid()
    tag = "%s_%d" % (host, pid)
    seed = args.seed if args.seed is not None else (hash(tag) ^ int(time.time())) & 0x7FFFFFFF
    rng = random.Random(seed)

    openings = []
    with open(args.openings) as f:
        for line in f:
            line = line.strip()
            if line:
                openings.append(json.loads(line)["moves"])

    if not os.path.isdir(args.out_dir):
        os.makedirs(args.out_dir)

    engine = Egaroucid(args.engine)
    t_start = time.time()
    games = positions = shard_idx = shard_games = 0
    fh = None

    def open_shard():
        path = os.path.join(args.out_dir, "%s_%04d.jsonl" % (tag, shard_idx))
        return open(path, 'w')

    fh = open_shard()
    sys.stderr.write("[%s] 开工 seed=%d 开局池=%d\n" % (tag, seed, len(openings)))
    sys.stderr.flush()

    try:
        while True:
            if os.path.exists(stop_file):
                sys.stderr.write("[%s] 检测到 STOP, 收工\n" % tag)
                break
            if args.games and games >= args.games:
                break
            rec = play_game(engine, rng, openings)
            if rec is None:
                sys.stderr.write("[%s] 引擎失联, 重启\n" % tag)
                sys.stderr.flush()
                engine.restart()
                continue
            for pcs, my, opp, best, score in rec:
                fh.write(json.dumps({
                    "pcs": pcs, "my": str(my), "opp": str(opp),
                    "best": pos_to_mv(best), "score": score,
                    "g": games, "src": tag,
                }, separators=(',', ':')) + "\n")
            fh.flush()
            games += 1
            shard_games += 1
            positions += len(rec)
            if shard_games >= GAMES_PER_SHARD:
                fh.close()
                shard_idx += 1
                shard_games = 0
                fh = open_shard()
            if games % 20 == 0:
                el = time.time() - t_start
                sys.stderr.write("[%s] %d盘 %d局面 %.1f分 (%.1f秒/盘)\n"
                                 % (tag, games, positions, el / 60, el / games))
                sys.stderr.flush()
    except KeyboardInterrupt:
        sys.stderr.write("[%s] 中断\n" % tag)
    finally:
        if fh:
            fh.close()
        engine.close()
        el = time.time() - t_start
        sys.stderr.write("[%s] 结束: %d盘 %d局面 %.1f分\n" % (tag, games, positions, el / 60))


if __name__ == '__main__':
    main()
