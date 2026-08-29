#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chase 标注工人 — 用 Edax 生成带标签的黑白棋训练局面。

纯标准库, Python 3.6 兼容, 无 pip 依赖。

生成方案(design/selfplay_policy_design.md, 2026-08-29 定稿):
  1. 前 8 手: 从对称去重后的全枚举开局池随机取一条(走完 pcs=12, 覆盖整个开局空间)
  2. 随机段: 从 RANDOM_UNTIL_PCS(12..53) 抽一个终点, 到该 pcs 之前双方按温度抽样落子
     —— 抽到 12 就是"枚举开局后立刻转最优", 抽到 53 就是"几乎整盘随机"
  3. 随机段之后: 双方全程走老师的最优着法(top-1)
  4. 标签: 每个局面都记老师的最优着法 + 评分(轮到方视角), 与实际走法无关
  5. 记完 pcs=53 即收工: pcs>=54 的局面不入库, 继续下只是白烧引擎

**双引擎**(2026-08-29 定案, 见配置块):
  老师 L15 + hint 1  -> 标签(每个局面都问)
  选择器 L5 + hint 30 -> 只决定随机段走向, 评分绝不进标签
正确性由 farm/selftest.py 自动检验(标签溯源 / 走法可复原 / 确定段必走老师手)。

引擎交互的坑见 class Edax 的文档字符串(四条, 每条都实测踩过)。
"""

import collections
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
# 双引擎分工(2026-08-29 定案):
#   老师 teacher  = Edax L15 + hint 1  —— 只负责出标签(最优着法 + 评分), 每个局面都问
#   选择器 selector = Edax L5 + hint 30 —— 只负责随机段"下一步走哪", 不进标签
# 为什么不合成一个: hint 30 比 hint 1 贵得多, 而温度采样只需要"一张大致靠谱的评分表",
# 标签始终来自老师, 选择器只决定"往哪走", 故低档位不影响数据质量。
#
# 为什么是 Edax 不是 Egaroucid(2026-08-29 实测定案):
#   质量 — 900题 Edax L15 top1 一致 90.2%(early 86.0 / mid 84.7 / late 100.0),
#          符号一致 91.9~97.2%; Egaroucid L15 在另一批900题上是 89.2%, 同一水平。
#   速度 — 在**农场真正要标的随机段局面**上(同一批28个局面逐格对照):
#          整局 Edax 8.7 秒 vs Egaroucid 351 秒, 快约 40 倍。
#          Egaroucid 在畸形局面上退化严重(正常局面 0.27s/局面 -> 随机段 8.4s/局面),
#          Edax 几乎不受影响(0.43 -> 0.21s/局面)。成因未查明, 但选型结论明确。
# 老师档位 L21(2026-08-29 定案, 从 L15 上调):
#   质量 — 900题实测按段对齐: early 81.3%->88.0%(+6.7), mid 80.3%->87.8%(+7.5),
#          评分符号 early 93.3%->95.9%, mid 93.9%->97.1%。late 段两者都是 100%(精确求解, 档位无关)。
#          (注: 历史日志里"L21 合计 87.9% vs L15 87.2%"是口径陷阱 —— L21 那次 late 段
#           300 题全部解析失败、没进分母, 等于拿 L21 的 early+mid 去比 L15 的三段全含。)
#   成本 — 同一批随机段局面实测, 整局 L15 12.2 秒 / L21 46.8 秒, 约 3.8 倍。
#          机房 24 工人实测 L15 约 26 万局面/小时, 换 L21 约 6.8 万/小时, 仍远超需求。
#   风险 — 历史上 L21 在 late 段曾 300/300 解析失败; 本版驱动专项复测 0/140 失败, 已排除。
TEACHER_LEVEL = 21
TEACHER_HINT = 1            # 老师只要 top-1(标签), 多要一个都是浪费
SELECTOR_LEVEL = 5          # 选择器档位: 只影响随机段走向, 不进标签
SELECTOR_HINT = 30          # 选择器要整张表, 温度采样需要全部合法着法的评分
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

# 分段温度: 三段网络各自管辖的 pcs 区间可以用不同的温度。
# 键是区间上界(含), 值是该区间用的温度; 查表时取第一个 pcs <= 上界的档。
# 单一温度用 --temperature 6, 分段用 --temperature 6/5/4(early/mid/late)。
STAGE_BOUNDS = (25, 39, 53)          # early 12-25 / mid 26-39 / late 40-53


def stage_temp(pcs, temps):
    """按 pcs 取该阶段的温度。temps 为 (T_early, T_mid, T_late) 或单元素元组。"""
    if len(temps) == 1:
        return temps[0]
    for bound, t in zip(STAGE_BOUNDS, temps):
        if pcs <= bound:
            return t
    return temps[-1]


def parse_temps(text):
    """'6' -> (6.0,); '6/5/4' -> (6.0,5.0,4.0)。也接受 inf。"""
    parts = [float(x) for x in text.replace(',', '/').split('/')]
    if len(parts) not in (1, 3):
        raise ValueError('温度须是 1 个或 3 个值(early/mid/late), 收到: %s' % text)
    return tuple(parts)


# Edax 棋盘行形如 "3 - - * * - . - - 3    ..."  (*=轮到方 O=对手 -=空 .=可落子点)
_BOARD_ROW_RE = re.compile(r'^([1-8]) ((?:[-*O.] ){8})')
# Edax hint 结果行形如 "15@73%  -03        0:04.143        713680     172262 f6 D6 e7 ..."
#   列依次是 深度 | 评分 | 时间 | 节点数 | N/s | PV, 着法是 PV 的第一个词
_ROW_RE = re.compile(r'^\s*\d+(@\d+%)?\s+([+-]\d+)\s+\d+:\d[\d.]*\s')
_MV_RE = re.compile(r'^[a-h][1-8]$')

GAMES_PER_SHARD = 200       # 每个分片文件写多少盘后换新文件
MAX_ENGINE_FAILS = 5        # 连续这么多盘引擎失联就退出(防引擎坏掉时720个工人一起空转)
HINT_TIMEOUT = 180.0
SETBOARD_TIMEOUT = 30.0
HINT_TRIES = 3              # 单次 hint 的就地重试次数。失败的代价应是"多问一次",
                            # 不是"整盘作废 + 退避 + 重启双引擎"。
ENGINE_START_TIMEOUT = 180.0   # 只用于启动握手: 加载引擎的耗时不该记在每条命令头上


class Prof(object):
    """分部门计时。开销极小(每次两个 time.time()), 常开也无妨; --profile 时打印。
    目的: 回答"时间到底花在哪" —— 引擎调用 / 读取 / 解析 / 对局逻辑 / 落盘 各占多少。"""
    t = collections.OrderedDict()
    n = collections.OrderedDict()
    fails = collections.OrderedDict()

    @classmethod
    def add(cls, key, dt):
        cls.t[key] = cls.t.get(key, 0.0) + dt
        cls.n[key] = cls.n.get(key, 0) + 1

    by_pcs = collections.OrderedDict()

    @classmethod
    def pcs(cls, p, dt):
        a = cls.by_pcs.setdefault(p, [0, 0.0])
        a[0] += 1; a[1] += dt

    @classmethod
    def pcs_report(cls):
        out = ["", "===== 老师 hint 按 pcs 的耗时 =====",
               "%-5s %6s %6s %9s %9s" % ("pcs", "空格", "次数", "合计秒", "均值秒")]
        tot = sum(v[1] for v in cls.by_pcs.values())
        for p in sorted(cls.by_pcs):
            c, t = cls.by_pcs[p]
            out.append("%-5d %6d %6d %9.2f %9.2f  %s"
                       % (p, 64 - p, c, t, t / c, '#' * int(round(t / max(tot, 1e-9) * 120))))
        return "\n".join(out)

    @classmethod
    def fail(cls, key):
        cls.fails[key] = cls.fails.get(key, 0) + 1

    @classmethod
    def report(cls, wall, games):
        out = ["", "===== 分部门耗时 (%d盘, 墙钟 %.1f秒, %.1f秒/盘) =====" % (games, wall, wall / max(1, games)),
               "%-26s %8s %10s %9s %9s" % ("部门", "次数", "合计秒", "占墙钟", "均值毫秒")]
        for k in cls.t:
            out.append("%-26s %8d %10.1f %8.1f%% %9.1f"
                       % (k, cls.n[k], cls.t[k], cls.t[k] / wall * 100, cls.t[k] / cls.n[k] * 1000))
        # 顶层部门 = hint合计(已含各自的 setboard 与解析) + 对局 + 落盘。
        # hint 的子项(引擎在算/我们在读/解析)是 hint合计 的**细分**, 不能再加一遍, 否则重复计数。
        top = [k for k in cls.t if k.endswith('.hint合计') or k.startswith('对局') or k.startswith('落盘')]
        acc = sum(cls.t[k] for k in top)
        out.append("-" * 66)
        out.append("%-26s %8s %10.1f %8.1f%%" % ("顶层合计(不含引擎启动)", "", acc, acc / wall * 100))
        out.append("%-26s %8s %10.1f %8.1f%%  <- 引擎启动/握手等"
                   % ("(未计入的其余部分)", "", wall - acc, (wall - acc) / wall * 100))
        if cls.fails:
            out.append("失败计数: " + ", ".join("%s=%d" % (k, v) for k, v in cls.fails.items()))
        return "\n".join(out)


class HintFail(Exception):
    """一次 hint 没拿到可用结果。reason 区分四种成因, 便于定位。"""
    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


class Edax(object):
    """Edax 控制台驱动。每次 hint 都是无状态的 setboard + hint。

    引擎交互的四个坑(全部实测踩过, 勿"简化"):
      1. 启动横幅/棋盘可能晚于固定 sleep 到达 -> 建连时必须排空到静默, 并做一次就绪握手
      2. setboard 会画**两张**棋盘 -> 不能"见到一张棋盘"就认为响应结束
      3. hint 的结果行在棋盘**之前** -> 只等棋盘会读到上一条命令的回显(整整错位一格),
         故 hint 必须额外要求"已见到结果行"(need_row)
      4. 结果行是 深度|评分|时间|节点数|N/s|PV, **着法在 PV 首位, 不是第 4 列**
    安全网: hint 返回前校验响应里的棋盘内容 == 所请求的局面, 不符即判失败。
    注: 这里**没有**逐次 _drain —— 实测每局面凭空多花 1.0 秒(1.324s vs 0.315s / 40局面),
        而 need_row + 棋盘校验已足够。**不要加回来。**
    """

    def __init__(self, exe, level, tag=""):
        self.exe = exe
        self.level = level
        self.tag = tag or ("L%d" % level)
        self.proc = None
        self.q = None
        self.last_lines = []
        self._start()

    def _start(self):
        self.proc = subprocess.Popen(
            [self.exe, '-n', '1', '-l', str(self.level), '-book-usage', 'off'],
            cwd=os.path.dirname(os.path.abspath(self.exe)),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, universal_newlines=True, bufsize=1)
        self.q = queue.Queue()
        t = threading.Thread(target=self._reader)
        t.daemon = True
        t.start()
        self._drain(1.5)                       # 坑1: 排空启动横幅
        # 就绪握手: 把"加载引擎"的耗时从后续每条命令的超时预算里摘出去
        if self._cmd("setboard " + '-' * 27 + 'OX------XO' + '-' * 27 + " X",
                     timeout=ENGINE_START_TIMEOUT) is None:
            dead = self.proc.poll()
            tail = "".join(self.last_lines).strip()
            raise RuntimeError("引擎启动握手失败(进程%s)%s"
                               % ("已退出码 %s" % dead if dead is not None else "仍存活但无响应",
                                  ("; 最后输出: " + tail) if tail else ""))

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self.q.put(line)
        except Exception:
            pass

    def _drain(self, quiet):
        last = time.time()
        while time.time() - last < quiet:
            try:
                self.q.get(timeout=0.1)
                last = time.time()
            except queue.Empty:
                pass

    def _cmd(self, cmd, need_row=False, timeout=HINT_TIMEOUT):
        """发一条命令, 读到本命令的收尾棋盘为止。need_row: 必须先见到结果行(见坑3)。"""
        tag = self.tag
        stale = self.q.qsize()             # 发命令前队列里已有的行 = 上一条命令的尾巴
        t0 = time.time()
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()
        out = []
        seen = False
        first = None
        # 首行时刻只在收到**本命令的实质内容**时才记。
        # 上一条命令的收尾(空行 + "> " 提示符)会立刻被读到, 若把它当首行,
        # 首行时刻会被钉死在 0 秒, 真正的等待全被算进"读余下" —— 实测踩过这个坑。
        while time.time() - t0 < timeout:
            try:
                line = self.q.get(timeout=0.5)
            except queue.Empty:
                # 引擎进程已死就别空等到超时了 —— 例如权重装不下时 Edax 会打印
                # "FATAL ERROR ... cannot allocate the hash table" 后直接退出。
                # 不检测的话这里会白等满 timeout(实测 180 秒), 报错还含糊。
                if self.proc.poll() is not None:
                    self.last_lines = out[-6:]
                    return None
                continue
            if stale > 0:
                stale -= 1                 # 属于上一条命令的残留, 不计入本命令
                out.append(line)
                if _ROW_RE.match(line):
                    seen = True
                continue
            if first is None and line.strip() and not line.strip().startswith('>'):
                first = time.time()
            out.append(line)
            if _ROW_RE.match(line):
                seen = True
            if "WHITE" in line and "A B C D E F G H" in line and (seen or not need_row):
                now = time.time()
                first = first or now
                Prof.add("引擎%s.%s.引擎在算" % (tag, cmd.split()[0]), first - t0)
                Prof.add("引擎%s.%s.我们在读" % (tag, cmd.split()[0]), now - first)
                return out
        Prof.add("引擎%s.%s.超时" % (tag, cmd.split()[0]), time.time() - t0)
        return None

    @staticmethod
    def _board_of(lines):
        """抠出响应里的棋盘, 返回 64 字符(X=轮到方 O=对手 -=空)。取不到返回 None。"""
        rows = {}
        for line in lines:
            m = _BOARD_ROW_RE.match(line)
            if m:
                rows[int(m.group(1))] = m.group(2).replace(' ', '')
        if len(rows) != 8:
            return None
        return ''.join(rows[i] for i in range(1, 9)).replace('*', 'X').replace('.', '-')

    def _hint_once(self, my, opp, n):
        """问一次。失败抛 HintFail(reason), reason 区分四种成因。"""
        cells = ['-'] * 64
        for i in range(64):
            if (my >> i) & 1:
                cells[i] = 'X'
            elif (opp >> i) & 1:
                cells[i] = 'O'
        want = ''.join(cells)
        if self._cmd("setboard " + want + " X", timeout=SETBOARD_TIMEOUT) is None:
            raise HintFail("setboard超时")
        out = self._cmd("hint %d" % n, need_row=True)
        if out is None:
            raise HintFail("hint超时")
        t0 = time.time()
        if self._board_of(out) != want:        # 安全网: 算的必须是所问的局面
            raise HintFail("棋盘不符")
        res = []
        for line in out:
            m = _ROW_RE.match(line)
            if not m:
                continue
            for tok in line[m.end():].split():
                if _MV_RE.match(tok.lower()):
                    mv = tok.lower()
                    res.append(((int(mv[1]) - 1) * 8 + (ord(mv[0]) - 97), int(m.group(2))))
                    break
        Prof.add("引擎%s.解析+校验" % self.tag, time.time() - t0)
        if not res:
            raise HintFail("解析为空")
        return res

    def hint(self, my, opp, n, tries=HINT_TRIES):
        """返回 [(pos, score), ...] 按评分从高到低 —— 均为 my 方(轮到方)视角。
        第 0 项就是最优着法(标签)。全部重试用尽仍失败才返回 []。
        n 必填: 老师用 1、选择器用 30, 默认值会让"用错引擎"变成静默错误。

        **恢复是分层的**(2026-08-29 改): 单次调用失败先就地重试, 棋局不作废。
        一次读取错位或偶发超时的代价是"多问一次", 而不是"整盘作废+退避+重启双引擎"。
        只有连续 tries 次都失败, 才向上报错让调用方去重启引擎。"""
        for k in range(tries):
            try:
                return self._hint_once(my, opp, n)
            except HintFail as e:
                Prof.fail("%s:%s" % (self.tag, e.reason))
                if k + 1 < tries:
                    t0 = time.time()
                    self._drain(0.25)          # 只在失败后排空: 把错位的残留冲掉
                    Prof.add("引擎%s.失败后重同步" % self.tag, time.time() - t0)
        return []

    def restart(self):
        self.close()
        self._start()

    def close(self):
        try:
            self.proc.stdin.write("quit\n")
            self.proc.stdin.flush()
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
    if T == float('inf'):
        return rows[rng.randrange(len(rows))][0]      # T->无穷 = 在合法着法上均匀随机
    top = rows[0][1]
    w = [math.exp((s - top) / T) for _, s in rows]   # 减去最大值防溢出, 不改变分布
    r = rng.random() * sum(w)
    acc = 0.0
    for (pos, _), wi in zip(rows, w):
        acc += wi
        if r <= acc:
            return pos
    return rows[-1][0]


def play_game(teacher, selector, rng, openings, temps=None):
    """下一整盘, 返回 [(pcs, my, opp, best_pos, score, T), ...](只含 PCS_MIN..PCS_MAX)。

    双引擎分工(不可混用, 混用即数据污染):
      teacher  (L15+hint1)  -> best/score, 即写进数据集的标签。每个局面都问。
      selector (L5+hint30)  -> 只用来决定随机段"下一步走哪"。**其评分绝不进标签。**
    随机段结束后不再调用 selector: 那一段直接走 teacher 的 top-1。

    T = 该局面实际用于抽样的温度; 0 表示此处已走出随机段, 走的是确定性 top-1。
    任一引擎失联即返回 None(整盘作废), 由调用方重启两个引擎。
    绝不"退化成确定性走法"继续 —— 那会静默改变数据分布, 事后查不出来。"""
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
        _t = time.time()
        my, opp = (black, white) if side == 1 else (white, black)
        legal = legal_moves(my, opp)
        Prof.add("对局.合法着法", time.time() - _t)
        if legal == 0:
            passes += 1
            side = -side
            continue
        passes = 0
        pcs = popcount(black | white)

        # --- 标签: 只认老师 ---
        _t = time.time()
        trows = teacher.hint(my, opp, TEACHER_HINT)
        _dt = time.time() - _t
        Prof.add("引擎老师.hint合计", _dt)
        Prof.pcs(pcs, _dt)
        if not trows:
            return None                    # 老师失联
        best, score = trows[0]             # 标签: 永远是老师的 top-1

        in_random = pcs < rand_until_pcs
        t_here = stage_temp(pcs, temps or (TEMPERATURE,)) if in_random else 0.0
        if PCS_MIN <= pcs <= PCS_MAX:
            out.append((pcs, my, opp, best, score, t_here))
        if pcs >= PCS_MAX:
            break     # 记完 pcs=53 即收工: 之后的局面不入库, 再下就是白烧引擎(省约20%调用)

        # --- 走子: 随机段问选择器, 之后走老师的 top-1 ---
        if in_random:
            _t = time.time()
            srows = selector.hint(my, opp, SELECTOR_HINT)
            Prof.add("引擎选择器.hint合计", time.time() - _t)
            if not srows:
                return None                # 选择器失联 —— 整盘作废, 不降级
            _t = time.time()
            mv = sample_move(srows, rng, t_here)
            Prof.add("对局.温度抽样", time.time() - _t)
        else:
            mv = best
        _t = time.time()
        my, opp = apply_move(my, opp, mv)
        black, white = (my, opp) if side == 1 else (opp, my)
        side = -side
        Prof.add("对局.落子", time.time() - _t)
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--engine', required=True, help='Edax 可执行文件(mEdax)路径')
    ap.add_argument('--openings', required=True, help='8手开局池 jsonl')
    ap.add_argument('--out-dir', required=True, help='分片输出目录(NFS共享)')
    ap.add_argument('--stop-file', default=None, help='此文件存在则收工(默认 out-dir/../STOP)')
    ap.add_argument('--games', type=int, default=0, help='0=无限, 直到 STOP')
    ap.add_argument('--seed', type=int, default=None)
    ap.add_argument('--level', type=int, default=TEACHER_LEVEL,
                    help='老师档位(决定标签质量), 默认 %d' % TEACHER_LEVEL)
    ap.add_argument('--sel-level', type=int, default=SELECTOR_LEVEL,
                    help='选择器档位(只影响随机段走向, 不进标签), 默认 %d' % SELECTOR_LEVEL)
    ap.add_argument('--temperature', type=str, default=str(TEMPERATURE),
                    help='随机段落子温度(净胜子)。单值如 6, 或分段如 6/5/4'
                         '(early12-25/mid26-39/late40-53); 默认 %.1f' % TEMPERATURE)
    ap.add_argument('--profile', action='store_true', help='结束时打印分部门耗时')
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

    teacher = Edax(args.engine, level=args.level, tag='老师')          # 出标签
    selector = Edax(args.engine, level=args.sel_level, tag='选择器')      # 出随机段走向
    t_start = time.time()
    games = positions = shard_idx = shard_games = fails = 0
    fh = None

    def open_shard():
        path = os.path.join(args.out_dir, "%s_%04d.jsonl" % (tag, shard_idx))
        return open(path, 'w')

    fh = open_shard()
    temps = parse_temps(args.temperature)
    sys.stderr.write("[%s] 开工 seed=%d 老师=L%d(hint%d) 选择器=L%d(hint%d) T=%s 开局池=%d\n"
                     % (tag, seed, args.level, TEACHER_HINT, args.sel_level,
                        SELECTOR_HINT, args.temperature, len(openings)))
    sys.stderr.flush()

    try:
        while True:
            if os.path.exists(stop_file):
                sys.stderr.write("[%s] 检测到 STOP, 收工\n" % tag)
                break
            if args.games and games >= args.games:
                break
            rec = play_game(teacher, selector, rng, openings, temps)
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
                teacher.restart()                  # 不知道是哪个坏了, 两个都重启
                selector.restart()
                continue
            fails = 0                              # 成功一盘即清零(只拦连续失败)
            _t = time.time()
            for pcs, my, opp, best, score, t_here in rec:
                fh.write(json.dumps({
                    "pcs": pcs, "my": str(my), "opp": str(opp),
                    "best": pos_to_mv(best), "score": score,
                    "g": games, "src": tag, "T": t_here, "Tsched": args.temperature,
                    "L": args.level, "SL": args.sel_level,
                }, separators=(',', ':')) + "\n")
            fh.flush()
            Prof.add("落盘.写分片", time.time() - _t)
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
        teacher.close()
        selector.close()
        el = time.time() - t_start
        sys.stderr.write("[%s] 结束: %d盘 %d局面 %.1f分\n" % (tag, games, positions, el / 60))
        if args.profile:
            sys.stderr.write(Prof.report(el, games) + "\n")
            sys.stderr.write(Prof.pcs_report() + "\n")


if __name__ == '__main__':
    main()
