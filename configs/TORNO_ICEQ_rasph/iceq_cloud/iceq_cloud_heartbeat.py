import json
import os
import time
import uuid
import socket
import datetime
import requests


CFG_PATH = "/home/iceq/linuxcnc/configs/TORNO_ICEQ/iceq_cloud/iceq_cloud_config.json"
STATE_PATH = "/home/iceq/linuxcnc/configs/TORNO_ICEQ/iceq_cloud/iceq_cloud_state.json"
LOCAL_LOG_PATH = "/home/iceq/linuxcnc/configs/TORNO_ICEQ/iceq_cloud/iceq_cloud_local.log"


PING_PERIOD_S = 15
RUNTIME_LOG_PERIOD_S = 60


def _utc_iso():
    return datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


def _load_json(path, default=None):
    if default is None:
        default = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(default)


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _append_local(line):
    ts = _utc_iso()
    with open(LOCAL_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{ts} {line}\n")


def _get_device_id():
    state = _load_json(STATE_PATH, default={})
    dev_id = state.get("device_id")
    if not dev_id:
        dev_id = str(uuid.uuid4())
        state["device_id"] = dev_id
        _save_json(STATE_PATH, state)
    return dev_id


def _post(url, headers, payload, timeout_s=8):
    r = requests.post(url, json=payload, headers=headers, timeout=timeout_s)
    return r.status_code, r.text


def send_ping(cfg, headers, device_id, hostname):
    base_url = cfg["base_url"].rstrip("/")
    payload = {
        "machine_id": cfg["machine_id"],
        "timestamp": _utc_iso(),
        "firmware_version": cfg.get("firmware_version", "iceq-ihm-bench"),
        "device_id": device_id,
        "host": hostname,
        "mode": "bench"
    }
    url = f"{base_url}/ihm-ping"
    return _post(url, headers, payload, timeout_s=6)


def send_runtime_log(cfg, headers, device_id, hostname, counters):
    base_url = cfg["base_url"].rstrip("/")

    runtime_payload = {
        "device_id": device_id,
        "firmware_version": cfg.get("firmware_version", "iceq-ihm-bench"),
        "host": hostname,
        "mode": "bench",
        "uptime_s": int(time.time() - counters["t0"]),
        "ping_ok": counters["ping_ok"],
        "ping_fail": counters["ping_fail"],
        "log_ok": counters["log_ok"],
        "log_fail": counters["log_fail"]
    }

    payload = {
        "machine_id": cfg["machine_id"],
        "timestamp": _utc_iso(),
        "log_type": "runtime",
        "payload": json.dumps(runtime_payload, ensure_ascii=False)
    }

    url = f"{base_url}/ihm-logs"
    return _post(url, headers, payload, timeout_s=8)


def main():
    cfg = _load_json(CFG_PATH)
    for k in ("base_url", "machine_id", "api_key"):
        if not cfg.get(k):
            raise RuntimeError(f"Config incompleta: faltando {k} em iceq_cloud_config.json")

    headers = {
        "Content-Type": "application/json",
        "x-api-key": cfg["api_key"]
    }

    hostname = socket.gethostname()
    device_id = _get_device_id()

    counters = {
        "t0": time.time(),
        "ping_ok": 0,
        "ping_fail": 0,
        "log_ok": 0,
        "log_fail": 0
    }

    next_ping = 0.0
    next_runtime = 0.0

    _append_local("[START] iceq_cloud_heartbeat iniciado")

    while True:
        now = time.time()

        if now >= next_ping:
            try:
                code, txt = send_ping(cfg, headers, device_id, hostname)
                if code == 200:
                    counters["ping_ok"] += 1
                    _append_local(f"[PING OK] {txt}")
                else:
                    counters["ping_fail"] += 1
                    _append_local(f"[PING FAIL] HTTP={code} {txt}")
            except Exception as e:
                counters["ping_fail"] += 1
                _append_local(f"[PING EXC] {e!r}")
            next_ping = now + PING_PERIOD_S

        if now >= next_runtime:
            try:
                code, txt = send_runtime_log(cfg, headers, device_id, hostname, counters)
                if code == 200:
                    counters["log_ok"] += 1
                    _append_local(f"[RUNTIME OK] {txt}")
                else:
                    counters["log_fail"] += 1
                    _append_local(f"[RUNTIME FAIL] HTTP={code} {txt}")
            except Exception as e:
                counters["log_fail"] += 1
                _append_local(f"[RUNTIME EXC] {e!r}")
            next_runtime = now + RUNTIME_LOG_PERIOD_S

        time.sleep(0.2)


if __name__ == "__main__":
    main()
