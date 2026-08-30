#!/bin/bash
# GCP 实例启动脚本 —— 开机自动装环境、拉代码、标注,不需要人登录。
#
# 用 --metadata-from-file startup-script=farm/gcp_startup.sh 传给 gcloud,
# 机器编号从实例元数据读(见下), 所以**一份脚本服务所有机器**。
#
# 启动命令示例:
#   gcloud compute instances create label-0 \
#     --machine-type=n2-highcpu-96 --zone=us-east1-b \
#     --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
#     --metadata=machine-index=0,machines-total=2 \
#     --metadata-from-file=startup-script=farm/gcp_startup.sh
#
# 进度看: gcloud compute ssh label-0 --command='tail -f /var/log/label.log'
# 完成标志: /root/LABEL_DONE 存在, 且 /root/chase_farm/labeled/ 里的 .done.jsonl 数量对得上
set -x
exec >> /var/log/label.log 2>&1
echo "===== 启动 $(date -u) ====="

meta() {
  curl -sf -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"
}
M=$(meta machine-index); M=${M:-0}
N=$(meta machines-total); N=${N:-1}
echo "本机是 $M / $N"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y build-essential git curl

cd /root
[ -d chase-farm ] || git clone https://github.com/joewang0430/chase-farm.git
cd chase-farm
git pull --ff-only || true

# 标注。bootstrap 会自己下任务分片并校验 sha256, 校验不过就中止。
#
# **重试循环**: 无人值守跑十几个小时, 进程可能因为任何原因挂掉(崩溃/OOM/引擎卡死),
# 而云上没人会把它拉起来 —— 醒来发现几小时前就停了。
# 重跑是安全且廉价的: 已完成的分片会被直接跳过(输出文件存在即跳过),
# 所以最坏情况只是重做最后那一片的一部分。
rc=1
for attempt in $(seq 1 20); do
  echo "----- 第 $attempt 次尝试 $(date -u) -----"
  ./bootstrap.sh --label --machine "$M" --machines "$N"
  rc=$?
  [ "$rc" = "0" ] && break
  echo "退出码 $rc, 60 秒后重试"
  sleep 60
done
echo "===== 标注结束 退出码 $rc $(date -u) ====="
ls /root/chase_farm/labeled/*.done.jsonl 2>/dev/null | wc -l > /root/LABEL_DONE
echo "已完成分片数: $(cat /root/LABEL_DONE)"
