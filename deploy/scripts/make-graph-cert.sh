#!/usr/bin/env bash
# P0-A: create the certificate the app uses to sign in to Microsoft Graph (no password, no client secret).
# Usage: make-graph-cert.sh [--dir <dir>] [--force]     default dir: $OTS_HOST_ETC (/etc/ots)
# Writes graph.key (private, mode 600), graph.pem (cert) and graph.cer (PUBLIC half: upload this to Entra).
set -euo pipefail
dir="${OTS_HOST_ETC:-/etc/ots}"; force=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) dir="$2"; shift 2 ;;
    --force) force=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done
mkdir -p "$dir"; chmod 700 "$dir"
if [[ -e "$dir/graph.key" && $force -eq 0 ]]; then
  echo "refusing to overwrite $dir/graph.key (use --force to rotate)" >&2; exit 1
fi
umask 077
openssl req -x509 -newkey rsa:2048 -sha256 -days 730 -nodes \
  -keyout "$dir/graph.key" -out "$dir/graph.pem" -subj "/CN=Office Task System" 2>/dev/null
openssl x509 -in "$dir/graph.pem" -outform DER -out "$dir/graph.cer"
chmod 600 "$dir/graph.key" "$dir/graph.pem"; chmod 644 "$dir/graph.cer"
thumb="$(openssl x509 -in "$dir/graph.pem" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d ':')"
echo "Created in $dir:"
echo "  graph.key  PRIVATE key, never share, never commit"
echo "  graph.pem  certificate (used by the app)"
echo "  graph.cer  public certificate: upload THIS to Entra > App registration > Certificates & secrets"
echo "SHA-1 thumbprint (must match Entra after upload): $thumb"
echo "Expires: $(openssl x509 -in "$dir/graph.pem" -noout -enddate | cut -d= -f2)"
