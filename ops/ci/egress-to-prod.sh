#!/usr/bin/env bash
# Stop odin-ci, and the containers its jobs start, reaching production's address by accident.
# Only an accident guard: a job with root through Docker can delete this table, so a real
# boundary belongs on pve3 or the gateway. Trusted Validation doesn't need production.
# Usage: egress-to-prod.sh audit|check|enforce|status|remove (see ops/ci/README.md)
#   audit    count traffic to production without blocking it; leave on for one full validation
#   check    fail unless an audit is loaded and has counted nothing
#   enforce  after a clean check, reject that traffic from now on, and across reboots
#   status   print the table
#   remove   drop the table and its boot unit
set -euo pipefail
PROD_ADDRESS=192.168.71.211
TABLE=odin_ci_egress
RULES=/etc/odin-ci-egress.nft
UNIT=/etc/systemd/system/odin-ci-egress.service

ruleset() {
  # Docker's traffic takes the forward hook, the host's own the output hook. A reject in
  # either base chain is final whatever Docker's own rules accept.
  cat <<RULES
table inet $TABLE {
  chain output {
    type filter hook output priority 0; policy accept;
    ip daddr $PROD_ADDRESS counter $1
  }
  chain forward {
    type filter hook forward priority 0; policy accept;
    ip daddr $PROD_ADDRESS counter $1
  }
}
RULES
}

load() {
  sudo nft delete table inet "$TABLE" 2>/dev/null || true
  ruleset "$1" | sudo nft -f -
}

check() {
  local table counted
  table=$(sudo nft list table inet "$TABLE") || { echo "no audit is loaded: run audit, then one full validation" >&2; return 1; }
  grep -q 'reject' <<<"$table" && { echo "already enforcing; an audit can't count anything now" >&2; return 1; }
  [ "$(grep -c 'counter packets' <<<"$table")" = 2 ] || { echo "audit table is not the expected one" >&2; return 1; }
  counted=$(grep 'counter packets' <<<"$table" | grep -v 'counter packets 0 bytes 0' || true)
  if [ -n "$counted" ]; then
    echo "something on this host reached production; don't enforce until it is explained:" >&2
    printf '%s\n' "$counted" >&2
    return 1
  fi
  echo "nothing reached $PROD_ADDRESS during the audit"
}

case "${1:-}" in
  audit) load "" ; echo "auditing traffic to $PROD_ADDRESS; run a full validation, then: $0 check" ;;
  check) check ;;
  enforce)
    check
    load reject
    ruleset reject | sudo tee "$RULES" >/dev/null
    sudo tee "$UNIT" >/dev/null <<SERVICE
[Unit]
Description=Keep odin-ci away from production
After=network-pre.target
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $RULES

[Install]
WantedBy=multi-user.target
SERVICE
    sudo systemctl daemon-reload
    sudo systemctl enable odin-ci-egress.service
    echo "traffic to $PROD_ADDRESS is rejected"
    ;;
  status) sudo nft list table inet "$TABLE" ;;
  remove)
    sudo systemctl disable odin-ci-egress.service 2>/dev/null || true
    sudo rm -f "$UNIT" "$RULES"
    sudo systemctl daemon-reload
    sudo nft delete table inet "$TABLE" 2>/dev/null || true
    ;;
  *) echo "usage: $0 audit|check|enforce|status|remove" >&2; exit 2 ;;
esac
