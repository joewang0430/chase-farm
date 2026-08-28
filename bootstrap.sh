#!/bin/bash
# chase-farm 一键启动 —— 在机房任意一台机器上跑这一条命令即可。
#
#   ./bootstrap.sh              起工人(自动检测/编译引擎, 按核数决定并发)
#   ./bootstrap.sh --check      只做环境自检, 不起工人
#   ./bootstrap.sh --workers 8  指定工人数
#   ./bootstrap.sh --stop       全场收工(建 STOP 文件, 所有机器的工人下一局结束后退出)
#   ./bootstrap.sh --resume     撤销 STOP
#   ./bootstrap.sh --status     看本机工人数与全场产量
#
# 约定: 家目录是 NFS 共享的, 所有机器共用 $FARM_ROOT。
set -u

FARM_ROOT="${FARM_ROOT:-$HOME/chase_farm}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE_DIR="$FARM_ROOT/engine"
ENGINE_BIN="$ENGINE_DIR/bin/Egaroucid_for_Console.out"
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
    n=$(pgrep -u "$USER" -f "farm/worker.py" 2>/dev/null | wc -l)
    log "本机 $HOST 工人数: $n"
    if [ -d "$SHARDS" ]; then
      files=$(ls "$SHARDS"/*.jsonl 2>/dev/null | wc -l)
      lines=$(cat "$SHARDS"/*.jsonl 2>/dev/null | wc -l)
      hosts=$(ls "$SHARDS" 2>/dev/null | sed 's/_[0-9]*_[0-9]*\.jsonl//' | sort -u | wc -l)
      log "全场: $hosts 台机器, $files 个分片, $lines 个局面"
    fi
    [ -f "$STOP_FILE" ] && log "注意: STOP 文件存在"
    exit 0 ;;
esac

WORKERS=0
CHECK_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check)   CHECK_ONLY=1; shift ;;
    --workers) WORKERS="$2"; shift 2 ;;
    *) die "未知参数: $1" ;;
  esac
done

# ---------------- 1. 环境自检 ----------------
log "环境自检..."
command -v python3 >/dev/null || die "没有 python3"
PYV=$(python3 -c 'import sys; print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,6) else 1)' || die "python3 版本过低: $PYV (需 >=3.6)"
NCORE=$(nproc 2>/dev/null || echo 4)
log "  主机=$HOST  python3=$PYV  核数=$NCORE"

mkdir -p "$FARM_ROOT" "$SHARDS" "$LOGS" || die "无法创建 $FARM_ROOT"
[ -w "$FARM_ROOT" ] || die "$FARM_ROOT 不可写"

# ---------------- 2. 引擎(全场共用一份, 只有第一台需要编译) ----------------
if [ -x "$ENGINE_BIN" ]; then
  log "  引擎已就绪(共享目录), 跳过编译"
else
  log "  引擎不存在, 开始编译(仅第一台机器需要, 约几分钟)..."
  command -v cmake >/dev/null || die "没有 cmake, 无法编译引擎"
  command -v git >/dev/null   || die "没有 git"
  SRC="$FARM_ROOT/Egaroucid_src"
  [ -d "$SRC" ] || git clone --depth 1 https://github.com/Nyanyan/Egaroucid.git "$SRC" \
      || die "clone Egaroucid 失败"
  # mac 上需要补 <bit>; Linux/GCC 通常不需要, 加了也无害
  grep -q '#include <bit>' "$SRC/src/engine/bit_generic.hpp" || \
    sed -i '0,/#include/s//#include <bit>\n#include/' "$SRC/src/engine/bit_generic.hpp"
  ( cd "$SRC" && cmake -B build -DBUILD_CONSOLE=ON -DCMAKE_BUILD_TYPE=Release >/dev/null \
      && cmake --build build -j "$NCORE" >/dev/null ) || die "编译失败"
  mkdir -p "$ENGINE_DIR/bin"
  cp "$SRC/bin/Egaroucid_for_Console.out" "$ENGINE_DIR/bin/" || die "找不到编译产物"
  cp -r "$SRC/bin/resources" "$ENGINE_DIR/bin/" 2>/dev/null || true
  log "  引擎编译完成 -> $ENGINE_BIN"
fi

# ---------------- 3. 开局池 ----------------
if [ -s "$OPENINGS" ]; then
  log "  开局池已就绪: $(wc -l < "$OPENINGS") 条"
else
  log "  生成 8 手开局池(对称去重全枚举)..."
  python3 "$REPO_DIR/make_openings.py" --out "$OPENINGS" || die "开局池生成失败"
fi

# ---------------- 4. 引擎冒烟 ----------------
log "  引擎冒烟测试..."
python3 - "$ENGINE_BIN" <<'EOF' || die "引擎冒烟失败"
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])) or '.', ''))
sys.path.insert(0, os.environ.get('FARM_REPO', '.'))
from worker import Egaroucid
from othello import INIT_BLACK, INIT_WHITE, pos_to_mv
e = Egaroucid(sys.argv[1])
pos, score = e.hint(INIT_BLACK, INIT_WHITE)
e.close()
if pos is None:
    sys.exit(1)
print("    初始局面 -> %s (%+d)" % (pos_to_mv(pos), score))
EOF

if [ "$CHECK_ONLY" = "1" ]; then
  log "自检通过(未起工人)"
  exit 0
fi

# ---------------- 5. 起工人 ----------------
[ -f "$STOP_FILE" ] && die "STOP 文件存在, 先 ./bootstrap.sh --resume"

if [ "$WORKERS" = "0" ]; then
  WORKERS=$(( NCORE - RESERVE_CORES ))
  [ "$WORKERS" -lt 1 ] && WORKERS=1
fi

RUNNING=$(pgrep -u "$USER" -f "farm/worker.py" 2>/dev/null | wc -l)
[ "$RUNNING" -gt 0 ] && die "本机已有 $RUNNING 个工人在跑, 先 --stop 或 pkill -f farm/worker.py"

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
log "  看现场: tmux attach -t $TMUX_SESSION   |   看进度: $0 --status"
log "  全场收工: $0 --stop"
