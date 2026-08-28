#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黑白棋位棋盘工具 — 纯标准库, Python 3.6 兼容, 无第三方依赖。

约定(与 chase 主项目一致):
  - 位置 pos = row*8 + col, row/col 均 0-7, pos=0 是 a1(左上)
  - 坐标字符串: 列 a-h(左→右), 行 1-8(上→下), 如 'd3' = row2,col3 = pos19
  - 位棋盘用 Python 大整数, 第 pos 位为 1 表示该格有子
  - (my, opp) 恒为"轮到走子的一方, 对方"
"""

FULL_MASK = 0xFFFFFFFFFFFFFFFF
FILE_A = 0x0101010101010101
FILE_H = 0x8080808080808080
NOT_FILE_A = FULL_MASK ^ FILE_A
NOT_FILE_H = FULL_MASK ^ FILE_H

# Python 3.6 没有 int.bit_count(), 用查表
_POPCNT8 = [bin(i).count('1') for i in range(256)]


def popcount(x):
    c = 0
    while x:
        c += _POPCNT8[x & 0xFF]
        x >>= 8
    return c


def set_bit(bb, row, col):
    return bb | (1 << (row * 8 + col))


def pos_to_mv(pos):
    """19 -> 'd3'"""
    return chr(97 + pos % 8) + str(pos // 8 + 1)


def mv_to_pos(mv):
    """'d3' -> 19; 非法格式返回 -1"""
    mv = mv.strip().lower()
    if len(mv) != 2 or not ('a' <= mv[0] <= 'h') or not ('1' <= mv[1] <= '8'):
        return -1
    return (int(mv[1]) - 1) * 8 + (ord(mv[0]) - 97)


def _sh_e(b):
    return (b & NOT_FILE_H) << 1 & FULL_MASK


def _sh_w(b):
    return (b & NOT_FILE_A) >> 1


def _sh_n(b):
    return b >> 8


def _sh_s(b):
    return (b << 8) & FULL_MASK


def _sh_ne(b):
    return (b & NOT_FILE_H) >> 7


def _sh_nw(b):
    return (b & NOT_FILE_A) >> 9


def _sh_se(b):
    return ((b & NOT_FILE_H) << 9) & FULL_MASK


def _sh_sw(b):
    return ((b & NOT_FILE_A) << 7) & FULL_MASK


_SHIFTS = (_sh_e, _sh_w, _sh_n, _sh_s, _sh_ne, _sh_nw, _sh_se, _sh_sw)


def legal_moves(my, opp):
    """返回 my 方全部合法着点的位棋盘"""
    empty = ~(my | opp) & FULL_MASK
    if empty == 0:
        return 0
    moves = 0
    for sh in _SHIFTS:
        x = sh(my) & opp
        acc = 0
        while x:
            nxt = sh(x)
            acc |= nxt
            x = nxt & opp
        moves |= acc & empty
    return moves


def apply_move(my, opp, pos):
    """my 在 pos 落子, 返回 (new_my, new_opp)。无翻子(非法)则原样返回。"""
    move_bit = 1 << pos
    if (my | opp) & move_bit:
        return my, opp
    flips = 0
    for sh in _SHIFTS:
        cur = sh(move_bit)
        line = 0
        while cur & opp:
            line |= cur
            cur = sh(cur)
        if cur & my:
            flips |= line
    if flips == 0:
        return my, opp
    return my | move_bit | flips, opp & ~flips


def moves_list(bb):
    """位棋盘 -> 位置列表"""
    out = []
    while bb:
        lsb = bb & -bb
        out.append(lsb.bit_length() - 1)
        bb ^= lsb
    return out


# --- 四重对称(黑白棋的对称群: 恒等 + 两条对角镜像 + 180°旋转) ---

def _mirror_up(bb):
    """反对角线镜像 (r,c)->(7-c,7-r)"""
    res = 0
    while bb:
        lsb = bb & -bb
        i = lsb.bit_length() - 1
        r, c = divmod(i, 8)
        res |= 1 << ((7 - c) * 8 + (7 - r))
        bb ^= lsb
    return res


def _mirror_down(bb):
    """主对角线镜像 (r,c)->(c,r)"""
    res = 0
    while bb:
        lsb = bb & -bb
        i = lsb.bit_length() - 1
        r, c = divmod(i, 8)
        res |= 1 << (c * 8 + r)
        bb ^= lsb
    return res


def _rot180(bb):
    """180°旋转 (r,c)->(7-r,7-c)"""
    res = 0
    while bb:
        lsb = bb & -bb
        i = lsb.bit_length() - 1
        res |= 1 << (63 - i)
        bb ^= lsb
    return res


SYMMETRIES = (('identity', lambda b: b), ('up', _mirror_up),
              ('down', _mirror_down), ('rot180', _rot180))


def canonical(black, white):
    """四重对称下的规范形, 用于开局枚举去重"""
    return min((fn(black), fn(white)) for _, fn in SYMMETRIES)


INIT_BLACK = set_bit(set_bit(0, 3, 4), 4, 3)   # e4, d5
INIT_WHITE = set_bit(set_bit(0, 3, 3), 4, 4)   # d4, e5
