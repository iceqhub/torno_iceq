#!/usr/bin/env python3
"""
iceq_cloud_heartbeat.py — Heartbeat standalone do ICEQ Cloud
Destino: configs/torno_iceq/iceq_cloud/iceq_cloud_heartbeat.py

Pode ser executado como serviço systemd independente do LinuxCNC,
ou chamado manualmente para manter a máquina "online" no painel.

Uso:
    python3 iceq_cloud_heartbeat.py

    # Como serviço (opcional):
    # systemctl --user start iceq-heartbeat
"""

import os
import sys
import time
import json
import socket
import datetime

# Localiza o config relativo a este script
_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(_DIR, "iceq_cloud_config.json")
LOCAL_LOG_PATH = os.path.join(_DIR, "iceq_cloud_local.log")

PING_PERIOD_S    = 15
RUNTIME_LOG_PERIOD_S = 60


def _utc_iso():
    return datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


def _append_local(line: str):
    ts = _utc_iso()
    try:
        with open(LOCAL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts} {line}\n")
    except Exception:
        pass


def main():
    # Importa o cliente da pasta pai (configs/torno_iceq/)
    parent_dir = os.path.dirname(_DIR)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    # Também tenta importar da própria pasta iceq_cloud/
    if _DIR not in sys.path:
        sys.path.insert(0, _DIR)

    try:
        from iceq_cloud_client import IceqCloudClient
    except ImportError as e:
        print(f"[HEARTBEAT] Falha importando IceqCloudClient: {e}")
        sys.exit(1)

    client = IceqCloudClient(CFG_PATH)

    if not client.is_configured():
        print("[HEARTBEAT] Cliente não configurado. Verifique iceq_cloud_config.json.")
        sys.exit(1)

    hostname = socket.gethostname()
    counters = {
        "t0":       time.time(),
        "ping_ok":  0,
        "ping_fail": 0,
        "log_ok":   0,
        "log_fail": 0,
    }

    next_ping    = 0.0
    next_runtime = 0.0

    _append_local("[START] iceq_cloud_heartbeat iniciado")

    while True:
        now = time.time()

        # ── PING ──
        if now >= next_ping:
            try:
                ok = client.send_ping()
                if ok:
                    counters["ping_ok"] += 1
                    _append_local("[PING OK]")
                else:
                    counters["ping_fail"] += 1
                    _append_local(f"[PING FAIL] {client.get_last_error()}")
            except Exception as e:
                counters["ping_fail"] += 1
                _append_local(f"[PING EXC] {e!r}")
            next_ping = now + PING_PERIOD_S

        # ── RUNTIME LOG ──
        if now >= next_runtime:
            try:
                runtime_payload = {
                    "host":       hostname,
                    "uptime_s":   int(time.time() - counters["t0"]),
                    "ping_ok":    counters["ping_ok"],
                    "ping_fail":  counters["ping_fail"],
                    "log_ok":     counters["log_ok"],
                    "log_fail":   counters["log_fail"],
                    "mode":       "heartbeat",
                }
                ok = client.send_log(
                    log_type="runtime",
                    tag="heartbeat",
                    severity="info",
                    payload=runtime_payload,
                )
                if ok:
                    counters["log_ok"] += 1
                    _append_local("[RUNTIME OK]")
                else:
                    counters["log_fail"] += 1
                    _append_local(f"[RUNTIME FAIL] {client.get_last_error()}")
            except Exception as e:
                counters["log_fail"] += 1
                _append_local(f"[RUNTIME EXC] {e!r}")
            next_runtime = now + RUNTIME_LOG_PERIOD_S

        time.sleep(0.5)


if __name__ == "__main__":
    main()
