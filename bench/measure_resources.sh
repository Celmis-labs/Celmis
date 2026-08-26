#!/usr/bin/env bash
# Семплер ресурсів усього стека — не лише процесу api.
# Пишe CSV, який лягає в графік і в скріншот.
#
#   ./measure_resources.sh index-grafana 5      # мітка, інтервал сек
#   Ctrl-C зупиняє, підсумок друкується в кінці.

LABEL="${1:-run}"
INTERVAL="${2:-5}"
OUT="$(dirname "$0")/results/resources_${LABEL}_$(date +%Y%m%d-%H%M%S).csv"
mkdir -p "$(dirname "$OUT")"

echo "ts,container,cpu_pct,mem_used_mb,mem_limit_mb,mem_pct,net_in_mb,net_out_mb,block_in_mb,block_out_mb,pids" > "$OUT"
echo "▶ семплю кожні ${INTERVAL}с → $OUT"
echo "  Ctrl-C щоб зупинити"

to_mb() { python3 -c "
import sys,re
v=sys.argv[1].strip()
m=re.match(r'([\d.]+)\s*([A-Za-z]+)',v)
if not m: print(0); raise SystemExit
n=float(m.group(1)); u=m.group(2).lower()
f={'b':1/1048576,'kb':1/1024,'kib':1/1024,'mb':1,'mib':1,'gb':1024,'gib':1024,'tb':1048576}
print(round(n*f.get(u,0),2))" "$1"; }

trap 'echo; echo "■ зупинено"; summary; exit 0' INT

summary() {
  echo
  echo "═══ ПІК ПО КОНТЕЙНЕРАХ ($LABEL) ═══"
  python3 - "$OUT" <<'PY'
import csv,sys,collections
rows=list(csv.DictReader(open(sys.argv[1])))
if not rows: print("даних немає"); raise SystemExit
peak=collections.defaultdict(lambda:{"cpu":0.0,"mem":0.0})
for r in rows:
    c=r["container"]
    peak[c]["cpu"]=max(peak[c]["cpu"],float(r["cpu_pct"] or 0))
    peak[c]["mem"]=max(peak[c]["mem"],float(r["mem_used_mb"] or 0))
tot_mem=sum(v["mem"] for v in peak.values())
print(f"{'контейнер':28} {'пік CPU %':>10} {'пік RAM MB':>12}")
for c,v in sorted(peak.items(), key=lambda x:-x[1]["mem"]):
    print(f"{c:28} {v['cpu']:>10.1f} {v['mem']:>12.0f}")
print(f"{'—':28} {'':>10} {'':>12}")
print(f"{'СУМА ПІКІВ RAM':28} {'':>10} {tot_mem:>12.0f} MB  ({tot_mem/1024:.1f} GB)")
print(f"\nзаписів: {len(rows)}  |  файл: {sys.argv[1]}")
PY
}

while true; do
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  docker stats --no-stream --format \
    '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}\t{{.PIDs}}' 2>/dev/null |
  while IFS=$'\t' read -r name cpu mem mempct net block pids; do
    [ -z "$name" ] && continue
    mu=$(to_mb "$(echo "$mem" | awk -F' / ' '{print $1}')")
    ml=$(to_mb "$(echo "$mem" | awk -F' / ' '{print $2}')")
    ni=$(to_mb "$(echo "$net" | awk -F' / ' '{print $1}')")
    no=$(to_mb "$(echo "$net" | awk -F' / ' '{print $2}')")
    bi=$(to_mb "$(echo "$block" | awk -F' / ' '{print $1}')")
    bo=$(to_mb "$(echo "$block" | awk -F' / ' '{print $2}')")
    echo "$TS,$name,${cpu%\%},$mu,$ml,${mempct%\%},$ni,$no,$bi,$bo,$pids" >> "$OUT"
  done
  sleep "$INTERVAL"
done
