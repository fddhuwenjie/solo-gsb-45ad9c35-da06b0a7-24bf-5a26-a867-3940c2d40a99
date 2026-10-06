#!/usr/bin/env bash
# End-to-end demo of the blind classification review workflow.
# Requires: curl and python3. Usage: ./scripts/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://localhost:8000}"

json_get() { python3 -c "import sys, json; d = json.load(sys.stdin); print($1)"; }

echo "== 1. create batch (credentials appear only in this response) =="
CREATE=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "allowed_categories": ["cat", "dog", "bird"],
  "samples": [
    {"code": "S01", "true_label": "cat"},
    {"code": "S02", "true_label": "dog"},
    {"code": "S03", "true_label": "bird"}
  ]
}')
echo "$CREATE" | python3 -m json.tool
BID=$(json_get "d['batch_id']" <<<"$CREATE")
R1=$(json_get "d['reviewer1_token']" <<<"$CREATE")
R2=$(json_get "d['reviewer2_token']" <<<"$CREATE")
ARB=$(json_get "d['arbiter_token']" <<<"$CREATE")

echo "== 2. public view: only codes and phase =="
curl -sS "$BASE/batches/$BID" | python3 -m json.tool

echo "== 3. reviewer 1 submits =="
curl -sS -X POST "$BASE/batches/$BID/submissions" \
  -H "X-Reviewer-Token: $R1" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "bird"}}' | python3 -m json.tool

echo "== 4. reviewer 2 submits (disagrees on S03) -> adjudicating =="
curl -sS -X POST "$BASE/batches/$BID/submissions" \
  -H "X-Reviewer-Token: $R2" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "cat"}}' | python3 -m json.tool

echo "== 5. arbiter lists disagreements =="
curl -sS "$BASE/batches/$BID/disagreements" -H "X-Arbiter-Token: $ARB" | python3 -m json.tool

echo "== 6. arbiter adjudicates -> sealed =="
curl -sS -X POST "$BASE/batches/$BID/adjudications" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"decisions": {"S03": "bird"}}' | python3 -m json.tool

echo "== 7. unblinded results (version 0: the sealed original) =="
curl -sS "$BASE/batches/$BID/results" | python3 -m json.tool

echo "== 8. issue errata v1: S01's recorded truth 'cat' is actually 'dog' =="
curl -sS -X POST "$BASE/batches/$BID/errata" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0, "corrections": [{"code": "S01", "true_label": "dog"}]}' \
  | python3 -m json.tool

echo "== 9. a stale request (expected_revision 0) is rejected with the current revision =="
curl -sS -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/batches/$BID/errata" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0, "corrections": [{"code": "S02", "true_label": "cat"}]}'

echo "== 10. retry with revision 1; correct two samples at once -> v2 =="
curl -sS -X POST "$BASE/batches/$BID/errata" \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "corrections": [
        {"code": "S02", "true_label": "cat"},
        {"code": "S03", "true_label": "dog"}
      ]}' | python3 -m json.tool

echo "== 11. query by version (v0 / v1 / v2) =="
for V in 0 1 2; do
  echo "-- version $V --"
  curl -sS "$BASE/batches/$BID/errata/$V" \
    | python3 -c "import sys, json; d=json.load(sys.stdin); print('version:', d['version'], 'summary:', d['summary'], 'corrected:', d['corrected_samples'])"
done

echo "== 12. list every version =="
curl -sS "$BASE/batches/$BID/errata" | python3 -m json.tool

echo "== 13. the original results query still returns the sealed original =="
curl -sS "$BASE/batches/$BID/results" | python3 -m json.tool
