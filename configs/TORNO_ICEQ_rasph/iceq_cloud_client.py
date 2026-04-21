import json
import os
import time
import uuid
import threading
from datetime import datetime, timezone

try:
    import requests
except Exception:
    requests = None


class IceqCloudClient:
    """
    Cliente mínimo para enviar ping e logs para Edge Functions do Supabase (Lovable).
    Requisitos do backend:
      - Base URL: https://...supabase.co/functions/v1
      - Endpoints: /ihm-ping e /ihm-logs
      - Header obrigatório: x-api-key: <api_key_da_maquina>
      - Content-Type: application/json
    """

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.cfg = {}
        self._lock = threading.RLock()
        self._last_error = ""
        self._last_ok_ts = 0.0

        self._load_or_init_config()

    def _utc_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load_or_init_config(self) -> None:
        with self._lock:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    self.cfg = json.load(f)
            else:
                self.cfg = {}

            # Campos mínimos
            self.cfg.setdefault("base_url", "https://atiklcfqprpfujezhfmd.supabase.co/functions/v1")
            self.cfg.setdefault("machine_id", "")
            self.cfg.setdefault("api_key", "")
            self.cfg.setdefault("device_id", str(uuid.uuid4()))
            self.cfg.setdefault("firmware_version", "0.1.0")
            self.cfg.setdefault("app_version", "0.1.0")

            self._save_config()

    def _save_config(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2, ensure_ascii=False)

    def is_configured(self) -> bool:
        with self._lock:
            return bool(self.cfg.get("base_url") and self.cfg.get("machine_id") and self.cfg.get("api_key"))

    def get_last_error(self) -> str:
        with self._lock:
            return self._last_error

    def _post_json(self, endpoint: str, payload: dict, timeout_s: float = 6.0) -> dict:
        if requests is None:
            raise RuntimeError("Biblioteca 'requests' não disponível. Instale python3-requests.")

        with self._lock:
            base_url = (self.cfg.get("base_url") or "").rstrip("/")
            api_key = (self.cfg.get("api_key") or "").strip()

        if not base_url:
            raise RuntimeError("base_url não configurada")
        if not api_key:
            raise RuntimeError("api_key não configurada")

        url = f"{base_url}/{endpoint.lstrip('/')}"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
        }

        r = requests.post(url, json=payload, headers=headers, timeout=timeout_s)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:600]}")

        try:
            return r.json()
        except Exception:
            return {"ok": True, "raw": r.text[:600]}

    def send_ping(self) -> bool:
        with self._lock:
            payload = {
                "machine_id": self.cfg.get("machine_id"),
                "device_id": self.cfg.get("device_id"),
                "firmware_version": self.cfg.get("firmware_version"),
                "app_version": self.cfg.get("app_version"),
                "timestamp": self._utc_iso(),
                "nonce": str(uuid.uuid4()),
            }

        try:
            resp = self._post_json("ihm-ping", payload)
            with self._lock:
                self._last_ok_ts = time.time()
                self._last_error = ""
            print(f"[ICEQ][CLOUD] PING OK: {resp}")
            return True
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            print(f"[ICEQ][CLOUD] PING ERRO: {e}")
            return False

    def send_startup_log(self, message: str = "IHM startup (bancada)") -> bool:
        with self._lock:
            payload = {
                "machine_id": self.cfg.get("machine_id"),
                "device_id": self.cfg.get("device_id"),
                "firmware_version": self.cfg.get("firmware_version"),
                "app_version": self.cfg.get("app_version"),
                "log_type": "startup",
                "timestamp": self._utc_iso(),
                "tags": ["ihm", "startup", "bench"],
                "payload": {
                    "msg": message,
                }
            }

        try:
            resp = self._post_json("ihm-logs", payload)
            with self._lock:
                self._last_ok_ts = time.time()
                self._last_error = ""
            print(f"[ICEQ][CLOUD] LOG STARTUP OK: {resp}")
            return True
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            print(f"[ICEQ][CLOUD] LOG STARTUP ERRO: {e}")
            return False

    def start_periodic_ping(self, interval_s: float = 20.0) -> None:
        def _worker():
            while True:
                self.send_ping()
                time.sleep(max(5.0, float(interval_s)))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()