#!/bin/bash
# Usage:
#   ./test_scenarios/run.sh                     # list all scenarios
#   ./test_scenarios/run.sh spam_01             # run one scenario
#   ./test_scenarios/run.sh all                 # run ALL scenarios
#   ./test_scenarios/run.sh emergency           # run all emergency scenarios
#   ./test_scenarios/run.sh normal              # run all normal scenarios
#   ./test_scenarios/run.sh spam                # run all spam scenarios
#   ./test_scenarios/run.sh clear               # clear the database

DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_URL="${BASE_URL:-http://localhost:8080}"

if [ -z "$1" ]; then
  echo "Available scenarios:"
  echo ""
  for f in "$DIR"/*.json; do
    name=$(basename "$f" .json)
    desc=$(python3.12 -c "import json; d=json.load(open('$f')); v=d.get('call',{}).get('retell_llm_dynamic_variables',{}); print(f\"{v.get('customer_name','?')} - {v.get('service_type') or v.get('issue_description','?')[:50]}\")")
    echo "  $name  →  $desc"
  done
  echo ""
  echo "Usage: $0 <scenario_name|all|emergency|normal|spam|clear>"
  exit 0
fi

if [ "$1" = "clear" ]; then
  python3.12 -c "
import db
conn = db.get_connection()
conn.execute('DELETE FROM messages')
conn.execute('DELETE FROM jobs')
conn.commit()
print('Database cleared.')
"
  exit 0
fi

# Find matching files
if [ "$1" = "all" ]; then
  files="$DIR"/*.json
elif [ -f "$DIR/$1.json" ]; then
  files="$DIR/$1.json"
else
  files="$DIR"/${1}*.json
fi

count=0
for f in $files; do
  [ -f "$f" ] || continue
  name=$(basename "$f" .json)
  echo "▶ Running: $name"
  result=$(curl -s -X POST "$BASE_URL/webhook/retell" \
    -H "Content-Type: application/json" \
    -d @"$f")
  echo "  Response: $result"
  count=$((count + 1))
  sleep 1
done

if [ $count -eq 0 ]; then
  echo "No scenarios matched '$1'"
  exit 1
fi

echo ""
echo "Done. $count scenario(s) fired."
echo "Dashboard: $BASE_URL/dash/$(python3.12 -c 'from config import DASHBOARD_SLUG; print(DASHBOARD_SLUG)')"
