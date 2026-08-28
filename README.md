# chase-farm — 黑白棋训练数据生成农场

用 Egaroucid 在机房批量生成带标签的黑白棋局面, 供 chase 项目的监督训练使用。
**纯标准库 Python 3.6+, 无需 pip 安装任何东西。**

---

## 快速开始(机房)

```bash
# 第一次: 拉代码
cd ~ && git clone https://github.com/joewang0430/chase-farm.git
cd chase-farm

# 自检(不起工人) —— 第一台机器会自动编译引擎, 约几分钟
./bootstrap.sh --check

# 起工人(默认 核数-4 个)
./bootstrap.sh

# 看进度(任意机器上都能看全场)
./bootstrap.sh --status

# 全场收工(所有机器的工人下一局结束后干净退出)
./bootstrap.sh --stop
```

因为家目录是 NFS 共享的, `~/chase_farm/` 对全机房所有机器是**同一个目录**:
引擎只需第一台编译一次, 开局池只生成一次, 所有机器的分片都落到同一处, STOP 文件全场可见。

## 一键铺开到多台

在中枢机(如 p107)上:

```bash
# 先配一次免密(NFS 共享 ~/.ssh, 配一次全场生效)
ssh-keygen -t ed25519 -N ''
cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# 起全场
for i in $(seq 97 132); do
  ssh -o StrictHostKeyChecking=no p$i \
    'cd ~/chase-farm && ./bootstrap.sh' </dev/null &
done
wait

# 收工(一条命令全场生效, 不需要挨台登录)
./bootstrap.sh --stop
```

## 目录结构(运行时)

```
~/chase_farm/                共享根目录
  engine/bin/                Egaroucid 二进制 + resources(全场共用)
  openings8.jsonl            8手开局池, 67k 条(全场共用)
  shards/                    数据分片 <主机>_<pid>_<序号>.jsonl
  logs/                      每个工人的 stderr
  STOP                       此文件存在 = 全场收工信号
```

## 数据格式

每行一个局面:

```json
{"pcs":24,"my":"1234567","opp":"8901234","best":"f5","score":-2,"g":17,"src":"p107_12345"}
```

| 字段 | 含义 |
|---|---|
| `pcs` | 盘上子数(只记录 12~53, 与训练分桶对齐) |
| `my` / `opp` | 位棋盘, **十进制字符串**(避免 JSON 大整数精度问题) |
| `best` | 老师(Egaroucid L15)认定的最优着法 |
| `score` | 老师的评分, 净胜子, **轮到方视角** |
| `g` | 该工人的第几盘 |
| `src` | 主机_进程号, 用于溯源与去重 |

## 生成方案

1. **前 8 手**: 从对称去重的**全枚举**开局池(67,361 条)随机取一条 —— 开局空间 100% 覆盖, 零采样偏差。
2. **随机段**: 从 `{12,16,...,56}` 抽一个终点手数, 段内双方**纯随机**走。
   随机段长度本身是随机的 —— 短段产出接近正常对局的局面, 长段产出畸形残局, 整个可达空间被均匀采到。
3. **随机段之后**: 双方**全程走老师的最优着法**直到终局 —— 保证每盘的后半段都是高质量对局。
4. **标签**: 每个局面都记老师的最优着法 + 评分, **与实际走了哪一步无关**。
   走偏只是为了**到达**更多样的局面, 教材永远是正解。

这套方案参考 Egaroucid 作者自己生成训练数据的做法(开局全枚举 + 多个随机手数 + 之后自对弈)。

## 老师档位: L15

900 局面实测(对照 Othello Sensei 的标注):

| 档位 | policy top-1 | value 符号 | 残局真值区 | 相对耗时 |
|---|---|---|---|---|
| L21 | 92.9% | 95.6% | 34/34 | ×4 |
| **L15** | **91.0%** | **95.3%** | **34/34** | **×1** |
| L10 | 84.7% | 93.8% | 34/34 | ×0.2 |

L15 相比 L21 只掉 1.9 个 policy 点, value 侧几乎无损, 但快 4 倍 ——
对"数据量是瓶颈"的我们, 这个交换划算。(Egaroucid 作者自己生成中盘数据用的是 L11。)

## 排障

| 症状 | 处理 |
|---|---|
| `--check` 说编译失败 | 看有没有 cmake/git; 手动 `cd ~/chase_farm/Egaroucid_src && cmake --build build` 看报错 |
| 工人起不来说"已有工人在跑" | `pkill -u $USER -f farm/worker.py` 后重试 |
| 某台机器分片不增长 | `tail ~/chase_farm/logs/<主机>_w1.log`; 引擎失联时工人会自动重启引擎 |
| 想临时加/减工人 | `./bootstrap.sh --workers N`(需先 stop 现有的) |

**注意**: 工人以 `nice 10` 运行, 只吃空闲 CPU, 别人登录用机时会自动让路;
默认每台留 4 核不占。
