#!/bin/bash
# fishbench lane runner — key-safe benchmark launcher.
#
# The API key never appears in command text, process listings, or workflow
# journals: lane.sh reads it at runtime from the ZCode provider config and
# hands it to fishbench via the --api-key-env indirection (FB_LANE_KEY).
#
# Usage:
#   lane.sh pong  PROVIDER_ID MODEL       liveness probe; exit 0 iff model replies PONG
#   lane.sh bench PROVIDER_ID [ARGS...]   python3 -m fishbench.bench --api-key-env FB_LANE_KEY ARGS...
#   lane.sh arena                          ensure the arena server on 127.0.0.1:8383 is up
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$HOME/.zcode/v2/config.json"
ARENA_URL="http://127.0.0.1:8383"

_cfg() {  # _cfg PROVIDER_ID FIELD -> prints provider options.FIELD
  python3 - "$CFG" "$1" "$2" <<'PY'
import json, os, sys
path, pid, field = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    cfg = json.load(open(os.path.expanduser(path)))
    prov = cfg["provider"][pid]
except Exception as e:
    sys.exit(f"lane.sh: cannot read provider {pid!r} from {path}: {e}")
val = prov.get("options", {}).get(field, "")
if not val:
    sys.exit(f"lane.sh: provider {pid!r} has no options.{field}")
print(val)
PY
}

cmd="${1:-help}"
[ "$#" -ge 1 ] && shift || true

case "$cmd" in
  pong)
    [ "$#" -eq 2 ] || { echo "usage: lane.sh pong PROVIDER_ID MODEL" >&2; exit 2; }
    PROV="$1"; MODEL="$2"
    BASE="$(_cfg "$PROV" baseURL)"
    KEY="$(_cfg "$PROV" apiKey)"
    python3 - "$BASE" "$KEY" "$MODEL" <<'PY'
import json, sys, urllib.request, urllib.parse
base, key, model = sys.argv[1], sys.argv[2], sys.argv[3]
payload = {"model": model, "max_tokens": 512,
           "messages": [{"role": "user", "content": "Reply with exactly: PONG"}]}
req = urllib.request.Request(
    f"{base}/chat/completions", data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
             "User-Agent": "fishbench-lane/0.1"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=90) as r:
        body = json.load(r)
except Exception as e:
    sys.exit(f"lane.sh: pong failed: {e}")
content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
host = urllib.parse.urlparse(base).netloc
print(f"pong host={host} model={model} content={content[:40]!r}")
sys.exit(0 if "PONG" in content.upper() else 3)
PY
    ;;

  bench)
    [ "$#" -ge 1 ] || { echo "usage: lane.sh bench PROVIDER_ID [BENCH ARGS...]" >&2; exit 2; }
    PROV="$1"; shift
    BASE="$(_cfg "$PROV" baseURL)"
    KEY="$(_cfg "$PROV" apiKey)"
    mkdir -p "$REPO/out"
    cd "$REPO"
    exec env FB_LANE_KEY="$KEY" python3 -m fishbench.bench \
      --base-url "$BASE" --api-key-env FB_LANE_KEY "$@"
    ;;

  arena)
    if curl -sf --max-time 5 "$ARENA_URL/api/health" >/dev/null 2>&1; then
      echo "arena: already up on :8383"
      exit 0
    fi
    echo "arena: down — restarting (nohup, log → data/server.log)" >&2
    cd "$REPO"
    nohup python3 -m fishbench.server --port 8383 >> data/server.log 2>&1 &
    for _ in $(seq 1 30); do
      sleep 0.5
      if curl -sf --max-time 2 "$ARENA_URL/api/health" >/dev/null 2>&1; then
        echo "arena: restarted on :8383"
        exit 0
      fi
    done
    echo "arena: failed to come up — see data/server.log" >&2
    exit 1
    ;;

  *)
    sed -n '2,13p' "$0"
    exit 2
    ;;
esac
