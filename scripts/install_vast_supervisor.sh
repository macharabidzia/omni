#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SCRIPT_DIR=/opt/supervisor-scripts
CONF_DIR=/etc/supervisor/conf.d

install -d "$SCRIPT_DIR" "$CONF_DIR"

cat >"$SCRIPT_DIR/omni-qwen.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
cd /workspace/omni
exec /workspace/omni/scripts/run_qwen_model.sh
EOF

cat >"$SCRIPT_DIR/omni-gateway.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
cd /workspace/omni
exec /workspace/omni/scripts/run_gateway_dev.sh
EOF

cat >"$SCRIPT_DIR/omni-worker.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
cd /workspace/omni
exec /workspace/omni/scripts/run_worker_dev.sh
EOF

cat >"$SCRIPT_DIR/omni-web.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
cd /workspace/omni
exec /workspace/omni/scripts/run_web_dev.sh
EOF

chmod +x \
  "$SCRIPT_DIR/omni-qwen.sh" \
  "$SCRIPT_DIR/omni-gateway.sh" \
  "$SCRIPT_DIR/omni-worker.sh" \
  "$SCRIPT_DIR/omni-web.sh"

cat >"$CONF_DIR/omni-qwen.conf" <<'EOF'
[program:omni-qwen]
environment=PROC_NAME="%(program_name)s"
command=/opt/supervisor-scripts/omni-qwen.sh
directory=/workspace/omni
autostart=true
autorestart=true
startsecs=5
stdout_logfile=/dev/stdout
redirect_stderr=true
stdout_logfile_maxbytes=0
EOF

cat >"$CONF_DIR/omni-gateway.conf" <<'EOF'
[program:omni-gateway]
environment=PROC_NAME="%(program_name)s"
command=/opt/supervisor-scripts/omni-gateway.sh
directory=/workspace/omni
autostart=true
autorestart=true
startsecs=5
stdout_logfile=/dev/stdout
redirect_stderr=true
stdout_logfile_maxbytes=0
EOF

cat >"$CONF_DIR/omni-worker.conf" <<'EOF'
[program:omni-worker]
environment=PROC_NAME="%(program_name)s"
command=/opt/supervisor-scripts/omni-worker.sh
directory=/workspace/omni
autostart=true
autorestart=true
startsecs=5
stdout_logfile=/dev/stdout
redirect_stderr=true
stdout_logfile_maxbytes=0
EOF

cat >"$CONF_DIR/omni-web.conf" <<'EOF'
[program:omni-web]
environment=PROC_NAME="%(program_name)s"
command=/opt/supervisor-scripts/omni-web.sh
directory=/workspace/omni
autostart=true
autorestart=true
startsecs=5
stdout_logfile=/dev/stdout
redirect_stderr=true
stdout_logfile_maxbytes=0
EOF

supervisorctl reread
supervisorctl update
RESTART_CADDY=0 ./scripts/configure_vast_portal.sh
supervisorctl restart omni-qwen omni-gateway omni-worker omni-web || true
supervisorctl restart caddy || true
