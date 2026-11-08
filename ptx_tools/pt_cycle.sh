#!/bin/bash
# pt_cycle.sh <name> <src.ptx> [outdir]: открывает сессию в Pro Tools (PTSL, без диалогов), сохраняет копию SaveSessionAs в <outdir>/<name>/o_<name>.ptx, закрывает без сохранения.
CLI=${PTSL_CLI:-ptsl_cli}   # ptsl_cli собирается из PTSL SDK
run(){ printf '%s\n' "$1" | $CLI --keep-going 2>&1 | cut -c1-300; }
m=$1; src=$2; out=${3:-/tmp/mt/cyc}/$m
echo "--- $m"
cur=$(run CId_GetSessionName)
case "$cur" in OK*) echo "ABORT: a session is already open in Pro Tools ($cur) - not touching it"; exit 3;; esac
case "$cur" in *"not available"*) echo "ABORT: Pro Tools is not running"; exit 4;; esac
run "CId_OpenSession {\"session_path\":\"$src\"}"
for i in $(seq 1 ${WAIT:-90}); do sleep 1; r=$(run CId_GetSessionName); case "$r" in OK*) break;; esac; done
echo "$r"; sleep ${SETTLE:-8}
rm -rf "$out"; mkdir -p "$out"
run "CId_SaveSessionAs {\"session_name\":\"o_$m\",\"session_location\":\"$out/\"}"
sleep 6
run 'CId_CloseSession {"save_on_close":false}'
sleep 6
