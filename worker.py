#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chase 标注工人 — 用 Egaroucid 生成带标签的黑白棋训练局面。

纯标准库, Python 3.6 兼容, 无 pip 依赖。

生成方案(design/selfplay_policy_design.md 与 2026-08-28 讨论定稿):
  1. 前 8 手: 从对称去重后的全枚举开局池随机取一条(走完 pcs=12, 覆盖整个开局空间)
  2. 随机段: 从 RANDOM_UNTIL_PCS(12..53) 抽一个终点, 到该 pcs 之前双方按温度抽样落子
     —— 抽到 12 就是"枚举开局后立刻转最优", 抽到 53 就是"几乎整盘随机"
  3. 随机段之后: 双方全程走老师的最优着法(top-1)
  4. 标签: 每个局面都记老师的最优着法 + 评分(轮到方视角), 与实际走法无关
  5. 记完 pcs=53 即收工: pcs>=54 的局面不入库, 继续下只是白烧引擎

引擎交互的四个已知坑(全部在此处理, 勿擅自"简化"):
  a. 启动横幅会先于任何响应到达 -> 启动后必须排空
  b. 高档位 hint 表的深度列形如 "21@74%" 而非纯数字 -> 正则须放宽
  c/d/e. 读取响应的三条实测事实(每条都曾让我写错, 勿凭直觉"简化"):
     1) 响应一次性吐出(行间零间隙), 等待全在"发命令→第一行"之间(可达数秒);
     2) 棋盘重绘次数不固定(setboard 首次 2 张、之后 1 张) -> 不能数张数;
     3) **hint 的棋盘先到、评分表后到** -> 一见棋盘就返回会把整张表丢掉。
     解法: 棋盘内容对齐 + hint 额外要求"表格已出现"(_sync 的 need_table)。
     (注: Egaroucid 对未知命令不报错、只重画棋盘, 所以"哨兵回声"那套在这里不成立)
"""

import json
import math
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
PCS_MIN = 12                # 只记录 pcs>=12 的局面(与训练分桶 12-25/26-39/40-53 对齐)
PCS_MAX = 53                # pcs>=54 即 empties<=10, 交给残局求解器, 网络不碰

# 随机段终止于哪个 pcs(含)。每盘从中随机抽一个:
#   12 = 只有枚举开局是"人为"的, 之后全程最优 —— 最干净的一档
#   53 = 几乎整盘随机, 只有最后几手是最优 —— 最畸形的一档(极端残局的来源)
# 用 pcs 而非手数(ply)判断: 出现 PASS 时手数会涨而子数不涨, 只有 pcs 忠实反映盘面进展。
# 连续取值(不跳档): 避免"某些 pcs 永远不会是转折点"的周期性痕迹。
RANDOM_UNTIL_PCS = list(range(PCS_MIN, PCS_MAX + 1))   # 12..53, 共 42 档

# 随机段内的落子温度(单位: 净胜子)。按 softmax(score/T) 对**全部**合法着法分配概率,
# top-1 也在池中且权重最高 —— 温度只压低劣手权重, 不排除任何着法。
# T=6 实测(三个真实局面): top-1 概率约为均匀分布的 2 倍, 最劣手从 7~17% 掉到 0.3~2.7%;
# 而评分相近的着法仍近似平分 —— "接近的照抽, 悬殊的枪毙"。
# 纯随机(T=∞)会让一方迅速崩盘, 使 24+ 子的极端局面主导数据集, 故收紧至 T=6。
TEMPERATURE = 6.0
HINT_MOVES = 30             # 一次问回全部着法的评分; 实测与 hint 1 同价(引擎本就全评)

# 棋盘行形如 "3 . . . X O . . .   ply 9 52 empties"; 用它把引擎画的盘面抠出来做内容对齐
_BOARD_ROW_RE = re.compile(r'^([1-8]) ((?:[.XO] ){8})')

GAMES_PER_SHARD = 200       # 每个分片文件写多少盘后换新文件
MAX_ENGINE_FAILS = 5        # 连续这么多盘引擎失联就退出(防引擎坏掉时720个工人一起空转)
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
        time.sleep(0.5)                        # 坑a: 丢掉启动横幅(非交互模式下通常为空)
        while not self.q.empty():
            self.q.get()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self.q.put(line)
        except Exception:
            pass

    def _send(self, cmd):
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def _sync(self, cmd, want, timeout, need_table=False):
        """发一条命令并取回完整响应。返回非棋盘行(hint 表格在其中), 失败返回 None。

        实测得到的三个事实(每一条都曾让我写错):
          1. 响应是一次性吐出的(行间零间隙), 等待全在"发命令 → 第一行"之间;
          2. 棋盘重绘次数不固定(setboard 首次 2 张、之后 1 张) —— 不能数张数;
          3. **hint 的棋盘先到、评分表后到** —— 一见棋盘就返回会把整张表丢掉。
        故: 用棋盘内容对齐, 且 hint 必须等到表格出现之后的那张棋盘(need_table=True)。
        """
        self._send(cmd)
        others = []
        rows = []
        seen_table = False
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                line = self.q.get(timeout=0.5)
            except queue.Empty:
                continue                 # 引擎在算(首次响应可达数秒), 继续等到 timeout
            m = _BOARD_ROW_RE.match(line)
            if not m:
                others.append(line)
                if line.strip().startswith('|'):
                    seen_table = True
                continue
            rows.append((int(m.group(1)), m.group(2).replace(' ', '')))
            if len(rows) == 8:
                rows.sort()
                if ''.join(r[1] for r in rows) == want and (seen_table or not need_table):
                    return others        # 内容对齐(且表格已到) = 本命令响应到齐
                rows = []                # 表格还没来, 或是旧盘 —— 继续读
        return None

    def hint(self, my, opp, n=HINT_MOVES):
        """返回 [(pos, score), ...] 按评分从高到低 —— 均为 my 方(轮到方)视角。
        第 0 项就是最优着法(标签)。失败返回 []。"""
        cells = ['.'] * 64
        for i in range(64):
            if (my >> i) & 1:
                cells[i] = 'X'          # 引擎画盘时: X=黑(轮到方), O=白
            elif (opp >> i) & 1:
                cells[i] = 'O'
        want = ''.join(cells)
        board_cmd = ''.join('B' if c == 'X' else ('W' if c == 'O' else '-') for c in cells)
        if self._sync("setboard " + board_cmd + " B", want, 30.0) is None:
            return []
        out = self._sync("hint %d" % n, want, HINT_TIMEOUT, need_table=True)
        if out is None:
            return []
        rows = []
        for line in out:
            if not line.strip().startswith('|'):
                continue
            f = [x.strip() for x in line.split('|')]
            # 坑b: f[1]=level, f[2]=深度(可能是 "21@74%"), f[3]=着法, f[4]=评分
            if len(f) > 5 and re.match(r'^[a-h][1-8]$', f[3]) and re.match(r'^[+-]\d+$', f[4]):
                rows.append(((int(f[3][1]) - 1) * 8 + (ord(f[3][0]) - 97), int(f[4])))
        return rows

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


def sample_move(rows, rng, temperature=None):
    """按 softmax(score/T) 从全部合法着法中抽一手。rows 为 [(pos, score), ...]。
    top-1 权重最高但不独占; 最劣手概率被压低却不为零 —— 覆盖不丢, 灾难手不再泛滥。"""
    T = TEMPERATURE if temperature is None else temperature
    top = rows[0][1]
    w = [math.exp((s - top) / T) for _, s in rows]   # 减去最大值防溢出, 不改变分布
    r = rng.random() * sum(w)
    acc = 0.0
    for (pos, _), wi in zip(rows, w):
        acc += wi
        if r <= acc:
            return pos
    return rows[-1][0]


def play_game(engine, rng, openings, temperature=None):
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

    rand_until_pcs = RANDOM_UNTIL_PCS[rng.randrange(len(RANDOM_UNTIL_PCS))]
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
        rows = engine.hint(my, opp)
        if not rows:
            return None                    # 引擎失联
        best, score = rows[0]              # 标签: 永远是 top-1
        if PCS_MIN <= pcs <= PCS_MAX:
            out.append((pcs, my, opp, best, score))
        if pcs >= PCS_MAX:
            break     # 记完 pcs=53 即收工: 之后的局面不入库, 再下就是白烧引擎(省约20%调用)
        # 随机段内按温度抽样(top-1 在池中且权重最高), 之后走 top-1
        mv = sample_move(rows, rng, temperature) if pcs < rand_until_pcs else best
        my, opp = apply_move(my, opp, mv)
        black, white = (my, opp) if side == 1 else (opp, my)
        side = -side
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
    ap.add_argument('--temperature', type=float, default=TEMPERATURE,
                    help='随机段落子温度(净胜子); 默认 %.1f' % TEMPERATURE)
    args = ap.parse_args()

    stop_file = args.stop_file or os.path.join(os.path.dirname(args.out_dir.rstrip('/')), 'STOP')
    host = os.uname()[1].split('.')[0]
    pid = os.getpid()
    # 分片名带启动时间戳: 机器重启后 pid 可能被复用, 只用 host_pid 会静默覆盖旧数据
    tag = "%s_%s_%d" % (host, time.strftime("%m%d%H%M%S"), pid)
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
    games = positions = shard_idx = shard_games = fails = 0
    fh = None

    def open_shard():
        path = os.path.join(args.out_dir, "%s_%04d.jsonl" % (tag, shard_idx))
        return open(path, 'w')

    fh = open_shard()
    sys.stderr.write("[%s] 开工 seed=%d T=%.1f 开局池=%d\n" % (tag, seed, args.temperature, len(openings)))
    sys.stderr.flush()

    try:
        while True:
            if os.path.exists(stop_file):
                sys.stderr.write("[%s] 检测到 STOP, 收工\n" % tag)
                break
            if args.games and games >= args.games:
                break
            rec = play_game(engine, rng, openings, args.temperature)
            if rec is None:
                fails += 1
                sys.stderr.write("[%s] 引擎失联(第%d次), 重启\n" % (tag, fails))
                sys.stderr.flush()
                if fails >= MAX_ENGINE_FAILS:
                    # 不能无限重启: 若引擎彻底坏了(权重损坏等), 720个工人会一起空转烧机时
                    sys.stderr.write("[%s] 连续失败 %d 次, 判定引擎不可用, 退出\n"
                                     % (tag, MAX_ENGINE_FAILS))
                    break
                time.sleep(min(30, 2 ** fails))    # 退避, 避免疯狂重启
                engine.restart()
                continue
            fails = 0                              # 成功一盘即清零(只拦连续失败)
            for pcs, my, opp, best, score in rec:
                fh.write(json.dumps({
                    "pcs": pcs, "my": str(my), "opp": str(opp),
                    "best": pos_to_mv(best), "score": score,
                    "g": games, "src": tag, "T": args.temperature,
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
