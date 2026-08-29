#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎验收: 拿标准答案向量核对一个 Edax 二进制。

用法: python3 farm/verify_engine.py <mEdax/lEdax 路径> [--vectors farm/known_answers.jsonl]

为什么需要它: 机房是自己从源码编译 Edax, 编译成功**不等于**答案正确 ——
eval.dat 版本不符、编译参数出错、CPU 指令集不匹配, 都可能让引擎跑得起来但答错。
那样产出的数据是坏的, 而且事后极难发现。
标准答案向量是本机用已验证的参考引擎(Edax 4.6 + sha256 已知的 eval.dat)生成的:
best 与 score 必须**逐字段完全相等**, 有一条不符就判定不可用。

退出码 0 = 通过, 非 0 = 不可用。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from worker import Edax, TEACHER_HINT   # noqa: E402
from othello import mv_to_pos           # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('engine')
    ap.add_argument('--vectors', default=os.path.join(HERE, 'known_answers.jsonl'))
    ap.add_argument('--level', type=int, default=15)
    args = ap.parse_args()

    if not os.path.exists(args.vectors):
        sys.stderr.write("找不到标准答案向量: %s\n" % args.vectors)
        return 2
    vecs = [json.loads(l) for l in open(args.vectors) if l.strip()]
    if not vecs:
        sys.stderr.write("标准答案向量为空\n")
        return 2

    try:
        e = Edax(args.engine, level=args.level)
    except Exception as ex:
        sys.stderr.write("引擎起不来: %s\n" % ex)
        return 3

    bad = []
    for v in vecs:
        got = e.hint(int(v['my']), int(v['opp']), TEACHER_HINT)
        if not got:
            bad.append((v['pcs'], '无响应', ''))
            continue
        pos, sc = got[0]
        if pos != mv_to_pos(v['best']) or sc != v['score']:
            bad.append((v['pcs'], "期望 %s(%+d)" % (v['best'], v['score']),
                        "实得 pos=%d(%+d)" % (pos, sc)))
    e.close()

    if bad:
        sys.stderr.write("**引擎验收失败**: %d/%d 条不符\n" % (len(bad), len(vecs)))
        for row in bad[:5]:
            sys.stderr.write("   pcs=%s  %s  %s\n" % row)
        sys.stderr.write("可能原因: eval.dat 版本不符 / 编译参数不当 / CPU 指令集不匹配。\n"
                         "**不要用这个引擎生成数据。**\n")
        return 1
    sys.stdout.write("引擎验收通过: %d/%d 条标准答案完全一致 (L%d)\n"
                     % (len(vecs), len(vecs), args.level))
    return 0


if __name__ == '__main__':
    sys.exit(main())
