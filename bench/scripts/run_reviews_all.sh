#!/usr/bin/env bash
# Прогін рев'ю по всіх 50 бенчмарк-PR із розкладки mapping.json.
#
# Відновлюваний: перед кожним PR дивиться у state-файл і пропускає зроблене.
# Поважає ліміт GitHub — акаунт бота зрізаний до 60 запитів на годину, одне
# рев'ю коштує ~3, тож перед кожним запуском перевіряємо залишок і чекаємо
# скидання замість того, щоб отримати 403 посеред прогону.
set -u
WS=774d8a12-5b58-4064-b565-9c5933626d31
CD=~/Desktop/Celmis
W=~/Desktop/celmis-bench-work
STATE=$W/reviewed.txt
CSV=$W/results-run-a.csv
touch "$STATE"
[ -s "$CSV" ] || echo "fork,pr,status,findings,agents_run,agents_failed,tokens_in,tokens_out,seconds" > "$CSV"

TOKEN=$(cd $CD && docker compose exec -T api python -c "
from src.credentials import resolve_git_credential
print(resolve_git_credential('github', workspace_id='$WS', account_label='default').secret)" 2>/dev/null | tr -d '\r\n')

remaining() {
  curl -s -H "Authorization: token $TOKEN" https://api.github.com/rate_limit \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["resources"]["core"]["remaining"])' 2>/dev/null || echo 0
}
reset_in() {
  curl -s -H "Authorization: token $TOKEN" https://api.github.com/rate_limit \
    | python3 -c 'import sys,json,time;print(max(60,int(json.load(sys.stdin)["resources"]["core"]["reset"]-time.time())+10))' 2>/dev/null || echo 300
}

python3 -c "
import json,pathlib
m=json.loads(pathlib.Path.home().joinpath('Desktop/celmis-bench-work/mapping.json').read_text())
for v in m.values(): print(v['fork'], v['pr_number'])
" | while read -r FORK NUM; do
  KEY="$FORK#$NUM"
  grep -qxF "$KEY" "$STATE" && continue

  while [ "$(remaining)" -lt 8 ]; do
    W_S=$(reset_in); echo "[$(date +%H:%M)] ліміт вичерпано — чекаю $((W_S/60)) хв"; sleep "$W_S"
  done

  LOG=$W/logs/review-$(echo "$KEY" | tr '/#' '__').log
  echo "[$(date +%H:%M)] $KEY"
  START=$(date +%s)
  # < /dev/null обов'язково: `docker compose exec` читає stdin, а stdin тут —
  # той самий канал, яким живиться цикл. Без цього перший же виклик з'їдає
  # решту списку і прогін «успішно завершується» на другому PR.
  (cd $CD && docker compose exec -T api analyzer review "github:$FORK#$NUM" -w "$WS" --post) > "$LOG" 2>&1 < /dev/null
  RC=$?
  SEC=$(( $(date +%s) - START ))

  python3 - "$LOG" "$FORK" "$NUM" "$SEC" "$CSV" <<'PY'
import re, sys, pathlib
log, fork, num, sec, csv = sys.argv[1:6]
t = pathlib.Path(log).read_text(errors="replace")
def find(p, d=""):
    m = re.search(p, t)
    return m.group(1) if m else d
row = [fork, num,
       find(r"status=(\w+)", "error"),
       find(r"findings=(\d+)", "0"),
       find(r"agents_run=([\w,]+)", "-").replace(",", "|"),
       find(r"agents_failed=([\w,\-]+)", "-").replace(",", "|"),
       find(r"tokens:\s+([\d,]+)/", "0").replace(",", ""),
       find(r"tokens:\s+[\d,]+/([\d,]+)", "0").replace(",", ""),
       sec]
open(csv, "a").write(",".join(str(x) for x in row) + "\n")
print("   ", row[2], "findings=" + row[3], "failed=" + row[5], sec + "s")
PY
  echo "$KEY" >> "$STATE"
done
echo "ПРОГІН A ЗАВЕРШЕНО $(date)"
