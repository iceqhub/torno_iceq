import json
import datetime
import socket
import requests


CFG_PATH = "/home/iceq/linuxcnc/configs/TORNO_ICEQ/iceq_cloud/iceq_cloud_config.json"


def _utc_iso():
    return datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


def _load_cfg():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def send_startup_log():
    cfg = _load_cfg()

    base_url = cfg.get("base_url", "").rstrip("/")
    machine_id = cfg.get("machine_id", "").strip()
    api_key = cfg.get("api_key", "").strip()
    device_id = cfg.get("device_id", "").strip()
    fw = cfg.get("firmware_version", "iceq-ihm-unknown")
    app = cfg.get("app_version", "iceq-app-unknown")

    if not base_url or not machine_id or not api_key:
        raise RuntimeError("Config incompleta. Verifique base_url, machine_id e api_key no JSON.")
    if not device_id or device_id == "MANTER_FIXO_AQUI":
        raise RuntimeError("device_id inválido. Gere um UUID e grave no JSON (campo device_id).")

    hostname = socket.gethostname()

    payload = {
        "machine_id": machine_id,
        "timestamp": _utc_iso(),
        "log_type": "startup",
        "payload": json.dumps({
            "device_id": device_id,
            "firmware_version": fw,
            "host": hostname,
            "mode": "bench",
            "message": f"IHM ICEQ startup (bench). host={hostname}"
        }, ensure_ascii=False)
    }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key
    }

    url = f"{base_url}/ihm-logs"
    r = requests.post(url, json=payload, headers=headers, timeout=8)

    print("HTTP:", r.status_code)
    print("Response:", r.text)
    r.raise_for_status()


if __name__ == "__main__":
    send_startup_log()
