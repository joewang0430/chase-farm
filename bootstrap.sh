#!/bin/bash
# chase-farm 一键启动 —— 在机房任意一台机器上跑这一条命令即可。
#
#   ./bootstrap.sh              起工人(自动检测/编译引擎, 按核数决定并发)
#   ./bootstrap.sh --check      只做环境自检 + 引擎验收, 不起工人
#   ./bootstrap.sh --workers 8  指定工人数
#   ./bootstrap.sh --stop       全场收工(建 STOP 文件, 所有机器的工人下一局结束后退出)
#   ./bootstrap.sh --resume     撤销 STOP
#   ./bootstrap.sh --status     看本机工人数与全场产量
#
# **标注模式**(给现成局面打标签, 用于云端跑 WTHOR 任务, 不需要共享文件系统):
#   ./bootstrap.sh --label --tasks <任务目录> --out <输出目录> [--machine i --machines N]
#   前面装引擎、校验、验收的步骤**完全复用**, 只是最后起 label_worker.py 而不是 worker.py。
#   不生成开局池(标注用不上)。
#
# 约定: 家目录是 NFS 共享的, 所有机器共用 $FARM_ROOT ——
#       引擎只需第一台编译一次, 开局池只生成一次, 分片全落到同一处, STOP 全场可见。
#
# 引擎 = Edax 4.6, 从源码编译。**不用官方 Makefile**, 理由:
#   Makefile 的 gcc 优化档写死了 -flto=auto(需 gcc>=10) 和 ARCH=x86-64-v3(需 gcc>=11),
#   而机房是 gcc 8.5, 两者都不认。Edax 提供 all.c 单文件合并版, 一条 gcc 命令即可编出
#   等价的优化版本(本机实测 8 秒, 与参考引擎 40/40 逐字段一致)。
set -u

FARM_ROOT="${FARM_ROOT:-$HOME/chase_farm}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE_DIR="$FARM_ROOT/engine"
ENGINE_BIN="$ENGINE_DIR/lEdax"
EDAX_SRC="$FARM_ROOT/edax-reversi"
# 钉死在**实测验证过**的 commit, 不用 v4.6 tag ——
# v4.6 tag 的源码有 bug: 启动即 "hash_init: cannot allocate the hash table" 后退出(本机实测)。
# 这个 commit 编出来的引擎与参考引擎 40/40 逐字段一致。
EDAX_COMMIT="14f048c05ddfa385b6bf954a9c2905bbe677e9d3"
OPENINGS="$FARM_ROOT/openings8.jsonl"
SHARDS="$FARM_ROOT/shards"
STOP_FILE="$FARM_ROOT/STOP"
LOGS="$FARM_ROOT/logs"
RESERVE_CORES=4          # 给系统和其他用户留的核数
TMUX_SESSION="farm"

HOST=$(hostname -s)

log() { echo "[$(date +%H:%M:%S)] $*"; }
die() { echo "错误: $*" >&2; exit 1; }

# ---------------- 子命令 ----------------
case "${1:-}" in
  --stop)
    mkdir -p "$FARM_ROOT"; touch "$STOP_FILE"
    log "已建 STOP —— 全场工人将在各自当前这盘结束后退出"
    exit 0 ;;
  --resume)
    rm -f "$STOP_FILE"; log "已撤销 STOP(需重新起工人)"; exit 0 ;;
  --status)
    n=$(pgrep -u "$USER" -f "worker.py" 2>/dev/null | wc -l)
    log "本机 $HOST 工人数: $n"
    if [ -d "$SHARDS" ]; then
      files=$(ls "$SHARDS"/*.jsonl 2>/dev/null | wc -l)
      lines=$(cat "$SHARDS"/*.jsonl 2>/dev/null | wc -l)
      hosts=$(ls "$SHARDS" 2>/dev/null | sed 's/_[0-9]*_[0-9]*_[0-9]*\.jsonl//' | sort -u | wc -l)
      log "全场: $hosts 台机器, $files 个分片, $lines 个局面"
    fi
    [ -f "$STOP_FILE" ] && log "注意: STOP 文件存在"
    exit 0 ;;
esac

WORKERS=0
CHECK_ONLY=0
LABEL=0
TASKS=""
LABEL_OUT=""
MACHINE=0
MACHINES=1
while [ $# -gt 0 ]; do
  case "$1" in
    --check)    CHECK_ONLY=1; shift ;;
    --workers)  WORKERS="$2"; shift 2 ;;
    --label)    LABEL=1; shift ;;
    --tasks)    TASKS="$2"; shift 2 ;;
    --out)      LABEL_OUT="$2"; shift 2 ;;
    --machine)  MACHINE="$2"; shift 2 ;;
    --machines) MACHINES="$2"; shift 2 ;;
    *) die "未知参数: $1" ;;
  esac
done
if [ "$LABEL" = "1" ]; then
  [ -n "$TASKS" ] || die "--label 需要 --tasks <任务目录>"
  [ -n "$LABEL_OUT" ] || die "--label 需要 --out <输出目录>"
  [ -d "$TASKS" ] || die "任务目录不存在: $TASKS"
fi

# ---------------- 1. 环境自检 ----------------
log "环境自检..."
command -v python3 >/dev/null || die "没有 python3"
PYV=$(python3 -c 'import sys; print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,6) else 1)' || die "python3 版本过低: $PYV (需 >=3.6)"
NCORE=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
log "  主机=$HOST  python3=$PYV  核数=$NCORE"

mkdir -p "$FARM_ROOT" "$SHARDS" "$LOGS" "$ENGINE_DIR/data" || die "无法创建 $FARM_ROOT"
[ -w "$FARM_ROOT" ] || die "$FARM_ROOT 不可写"

# ---------------- 2. 权重文件(跨平台通用, 随仓库带来, 校验 sha256) ----------------
if [ ! -s "$ENGINE_DIR/data/eval.dat" ]; then
  log "  安装 eval.dat ..."
  [ -s "$REPO_DIR/edax_data/eval.dat" ] || die "仓库里缺 edax_data/eval.dat"
  cp "$REPO_DIR/edax_data/eval.dat" "$REPO_DIR/edax_data/book.dat" "$ENGINE_DIR/data/" || die "复制 eval.dat 失败"
fi
if command -v sha256sum >/dev/null; then
  GOT=$(sha256sum "$ENGINE_DIR/data/eval.dat" | cut -d' ' -f1)
elif command -v shasum >/dev/null; then
  GOT=$(shasum -a 256 "$ENGINE_DIR/data/eval.dat" | cut -d' ' -f1)
else
  GOT=""
fi
WANT=$(cut -d' ' -f1 "$REPO_DIR/edax_data/SHA256SUMS")
if [ -n "$GOT" ] && [ "$GOT" != "$WANT" ]; then
  die "eval.dat 校验不符!  期望 $WANT  实得 $GOT —— 权重文件损坏, 不能用"
fi
log "  eval.dat 校验通过"

# ---------------- 3. 引擎(全场共用一份, 只有第一台需要编译) ----------------
if [ -x "$ENGINE_BIN" ]; then
  log "  引擎已就绪(共享目录), 跳过编译"
else
  log "  引擎不存在, 开始编译(仅第一台机器需要, 约 10 秒)..."
  command -v git >/dev/null || die "没有 git"
  CC_BIN=""
  for c in gcc cc clang; do command -v "$c" >/dev/null && { CC_BIN="$c"; break; }; done
  [ -n "$CC_BIN" ] || die "找不到 C 编译器(gcc/cc/clang)"
  log "    编译器: $CC_BIN ($($CC_BIN --version | head -1))"

  if [ ! -d "$EDAX_SRC" ]; then
    git clone --quiet https://github.com/abulmo/edax-reversi.git "$EDAX_SRC" \
      || die "clone Edax 失败(网络?)"
    ( cd "$EDAX_SRC" && git checkout --quiet "$EDAX_COMMIT" ) \
      || die "切到验证过的 commit $EDAX_COMMIT 失败"
  fi
  [ -s "$EDAX_SRC/src/all.c" ] || die "源码里找不到 src/all.c(Edax 版本不对?)"

  # -march: 机房 gcc 8.5 不认 x86-64-v3, 用 native 让编译器自己探测(实测 CPU 有 avx2)
  # -pthread: Edax 用 C11 threads。RHEL8 的 glibc 2.28 把 cnd_broadcast 等放在 libpthread,
  #           不加会报 "undefined reference to cnd_broadcast@@GLIBC_2.28"(机房实测)。
  # -lrt: Linux 需要; macOS 没有这个库(pthread 在 libSystem 里, -pthread 加了也无害)
  EXTRA_LIB="-pthread"
  [ "$(uname -s)" = "Linux" ] && EXTRA_LIB="-pthread -lrt"
  ( cd "$EDAX_SRC/src" && $CC_BIN -std=c17 -O3 -flto -ffast-math -fomit-frame-pointer \
      -DNDEBUG -D_GNU_SOURCE=1 -march=native -w all.c -o "$ENGINE_BIN" -lm $EXTRA_LIB ) \
    || die "编译失败 —— 手动重试:
    cd $EDAX_SRC/src
    $CC_BIN -std=c17 -O3 -DNDEBUG -D_GNU_SOURCE=1 -march=native -w all.c -o $ENGINE_BIN -lm $EXTRA_LIB
  若报 undefined reference to cnd_* / thrd_* , 在末尾再加 -lpthread
  若报 -march=native 不支持, 换成 -march=haswell; 再不行去掉 -march 整项"
  log "  引擎编译完成 -> $ENGINE_BIN"
fi

# ---------------- 4. 引擎验收(编译成功 != 答案正确) ----------------
log "  引擎验收(标准答案向量)..."
# 注意: 不要写成 `python3 ... | sed ... || die` —— 管道的退出码取的是 sed 的,
# die 永远不会触发, 闸门形同虚设(本机实测踩过: 验收明明失败却报"自检通过")。
if ! python3 "$REPO_DIR/verify_engine.py" "$ENGINE_BIN" \
       --vectors "$REPO_DIR/known_answers.jsonl" > "$FARM_ROOT/.verify.out" 2>&1; then
  sed 's/^/    /' "$FARM_ROOT/.verify.out" >&2
  die "引擎验收失败 —— 这个引擎会产出错误标签, 已中止"
fi
sed 's/^/    /' "$FARM_ROOT/.verify.out"

# ---------------- 5. 开局池(标注模式用不上, 跳过) ----------------
if [ "$LABEL" = "1" ]; then
  log "  标注模式: 跳过开局池"
elif [ -s "$OPENINGS" ]; then
  log "  开局池已就绪: $(wc -l < "$OPENINGS") 条"
else
  log "  生成 8 手开局池(对称去重全枚举, 约 1 分钟)..."
  python3 "$REPO_DIR/make_openings.py" --out "$OPENINGS" || die "开局池生成失败"
fi

if [ "$CHECK_ONLY" = "1" ]; then
  log "自检通过(未起工人)"
  exit 0
fi

# ---------------- 6. 起工人 ----------------
if [ "$WORKERS" = "0" ]; then
  WORKERS=$(( NCORE - RESERVE_CORES ))
  [ "$WORKERS" -lt 1 ] && WORKERS=1
fi

# ---- 标注模式: 前台跑到做完为止, 不用 tmux 也不用 STOP 文件 ----
# 云上机器是我们独占的, 不需要"给别人让路"那套; 跑完自己退出, 便于脚本判断何时收结果。
if [ "$LABEL" = "1" ]; then
  mkdir -p "$LABEL_OUT" "$LOGS"
  TOT=$(ls "$TASKS"/part_*.jsonl 2>/dev/null | wc -l)
  log "标注模式: 任务 $TOT 片, 本机 $MACHINE/$MACHINES, $WORKERS 个进程"
  python3 "$REPO_DIR/label_worker.py" \
      --engine "$ENGINE_BIN" --tasks "$TASKS" --out "$LABEL_OUT" \
      --machine "$MACHINE" --machines "$MACHINES" --workers "$WORKERS" \
      2>&1 | tee -a "$LOGS/${HOST}_label.log"
  rc=${PIPESTATUS[0]}
  DONE=$(ls "$LABEL_OUT"/*.done.jsonl 2>/dev/null | wc -l)
  log "标注结束(退出码 $rc), 本机可见的已完成分片 $DONE / $TOT"
  exit $rc
fi

[ -f "$STOP_FILE" ] && die "STOP 文件存在, 先 ./bootstrap.sh --resume"

RUNNING=$(pgrep -u "$USER" -f "worker.py" 2>/dev/null | wc -l)
[ "$RUNNING" -gt 0 ] && die "本机已有 $RUNNING 个工人在跑, 先 --stop 或 pkill -u $USER -f worker.py"

command -v tmux >/dev/null || die "没有 tmux"
tmux kill-session -t "$TMUX_SESSION" 2>/dev/null
tmux new-session -d -s "$TMUX_SESSION" -n main "sleep 100000"
for i in $(seq 1 "$WORKERS"); do
  tmux new-window -d -t "$TMUX_SESSION" -n "w$i" \
    "nice -n 10 python3 '$REPO_DIR/worker.py' \
       --engine '$ENGINE_BIN' --openings '$OPENINGS' \
       --out-dir '$SHARDS' --stop-file '$STOP_FILE' \
       2>> '$LOGS/${HOST}_w${i}.log'"
done
log "已在 $HOST 起 $WORKERS 个工人(nice 10, tmux 会话 '$TMUX_SESSION')"
# 档位从 worker.py 读, 不写死 —— 写死过一次, 改了 TEACHER_LEVEL 却漏了这句提示
LVLS=$(python3 -c "import sys; sys.path.insert(0,'$REPO_DIR'); import worker; \
print('老师 L%d(hint%d) + 选择器 L%d(hint%d)' % (worker.TEACHER_LEVEL, worker.TEACHER_HINT, \
worker.SELECTOR_LEVEL, worker.SELECTOR_HINT))" 2>/dev/null || echo "老师+选择器")
log "  每个工人 = 2 个 Edax 进程($LVLS), 常驻内存约 165MB"
log "  看现场: tmux attach -t $TMUX_SESSION   |   看进度: $0 --status"
log "  全场收工: $0 --stop"
