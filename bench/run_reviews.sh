#!/usr/bin/env bash
# Прогін рев'ю по списку PR із журналом: час, коди виходу, coverage.
#
#   ./run_reviews.sh prs.txt api        # рушій api (Gemini)
#   ./run_reviews.sh prs.txt claude_code
#
# prs.txt — по одному посиланню в рядку:
#   github:celmis-bench/sentry-93824#1

LIST="${1:?вкажіть файл зі списком PR}"
ENGINE="${2:-api}"
DIR="$(cd "$(dirname "$0")" && pwd)"
STAMP=$(date +%Y%m%d-%H%M%S)
LOG="$DIR/results/reviews_${ENGINE}_${STAMP}.csv"
RAW="$DIR/results/raw_${ENGINE}_${STAMP}"
mkdir -p "$DIR/results" "$RAW"

echo "pr,started,seconds,exit_code,status" > "$LOG"
TOTAL=$(grep -cve '^\s*$' "$LIST")
N=0; OK=0; FAIL=0

echo "▶ рушій=$ENGINE  PR=$TOTAL  журнал=$LOG"

while IFS= read -r PR; do
  [ -z "${PR// }" ] && continue
  N=$((N+1))
  SAFE=$(echo "$PR" | tr '/:#' '___')
  T0=$(date +%s); TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf "[%2d/%2d] %-46s " "$N" "$TOTAL" "$PR"

  analyzer review "$PR" --post > "$RAW/$SAFE.txt" 2>&1
  RC=$?
  T1=$(date +%s); SEC=$((T1-T0))

  if [ $RC -eq 0 ]; then STATUS=ok; OK=$((OK+1)); echo "✓ ${SEC}s"
  else STATUS=failed; FAIL=$((FAIL+1)); echo "✗ ${SEC}s (rc=$RC)"; fi
  echo "$PR,$TS,$SEC,$RC,$STATUS" >> "$LOG"
done < "$LIST"

echo
echo "═══ ПІДСУМОК ($ENGINE) ═══"
echo "  усього PR:    $TOTAL"
echo "  відпрацювало: $OK"
echo "  впало:        $FAIL"
python3 -c "print(f'  COVERAGE:     {100*$OK/max($TOTAL,1):.1f}%')"
python3 - "$LOG" <<'PY'
import csv,sys,statistics
r=[x for x in csv.DictReader(open(sys.argv[1]))]
s=[int(x["seconds"]) for x in r if x["status"]=="ok"]
if s:
    print(f"  час на PR:    медіана {statistics.median(s):.0f}s, "
          f"сер {statistics.mean(s):.0f}s, макс {max(s)}s")
    print(f"  разом:        {sum(int(x['seconds']) for x in r)/60:.0f} хв")
PY
echo
echo "  ⚠ COVERAGE обов'язково в звіті: рушій, що мовчить, виглядає точнішим"
