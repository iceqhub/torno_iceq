#!/usr/bin/env python3
"""
iceq_cloud_client.py — ICEQ Cloud Client (versão consolidada)
Destino: configs/torno_iceq/iceq_cloud/iceq_cloud_client.py

Versão que combina:
  - TLS pinning por IP fixo (evita falhas de DNS durante LinuxCNC/Mesa)
  - Escrita atômica do config (.tmp → rename + .bak)
  - Blindagem contra JSON corrompido (não sobrescreve config válido)
  - Ping periódico em thread daemon
  - send_log com campos obrigatórios da Edge Function Supabase

Uso:
    from iceq_cloud_client import IceqCloudClient
    client = IceqCloudClient("/caminho/para/iceq_cloud_config.json")
    client.send_ping()
    client.send_startup_log("IHM iniciou")
    client.start_periodic_ping(interval_s=20.0)
"""

import json
import os
import ssl
import socket
import http.client
import time
import uuid
import threading
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────
# TLS com IP fixo (evita dependência de DNS durante operação CNC)
# ─────────────────────────────────────────────────────────────

class _PinnedIPHTTPSConnection(http.client.HTTPSConnection):
    """
    Conecta em um IP fixo mas mantém o Host/SNI do domínio.
    Isso evita quebra de certificado ao "pinar" por IP.
    """

    def __init__(self, host, pinned_ip=None, **kwargs):
        self._orig_host = host
        self._pinned_ip = pinned_ip or host
        kwargs.pop("resolved_ip", None)
        kwargs.pop("pinned_ip", None)
        self.context = kwargs.get("context", None)
        super().__init__(host, **kwargs)

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address
        )
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            raw_sock = self.sock
        else:
            raw_sock = sock

        ctx = self.context or ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=self._orig_host)


# ─────────────────────────────────────────────────────────────
# Cliente principal
# ─────────────────────────────────────────────────────────────

class IceqCloudClient:
    """
    Cliente mínimo e robusto para o ICEQ Cloud (Supabase Edge Functions).

    Endpoints esperados:
      POST {base_url}/ihm-ping
      POST {base_url}/ihm-logs

    Headers obrigatórios:
      Content-Type: application/json
      x-api-key: <api_key_da_maquina>

    Config JSON (iceq_cloud_config.json):
    {
      "base_url":         "https://<projeto>.supabase.co/functions/v1",
      "machine_id":       "<uuid>",
      "api_key":          "<uuid>",
      "device_id":        "<uuid>",
      "firmware_version": "1.0.0",
      "app_version":      "1.0.0",
      "resolved_ip":      "x.x.x.x"   (opcional — para DNS pinning)
    }
    """

    def __init__(self, config_path: str):
        self.config_path = str(config_path)
        self._lock = threading.RLock()
        self._cfg = {}
        self._cfg_loaded_ok = False
        self._cfg_file_missing = False
        self._last_error = ""
        self._last_ok_ts = 0.0

        self._load_config()

        # Gera device_id se ausente ou inválido
        dev = str(self._cfg.get("device_id", "")).strip()
        if not dev or dev.upper() == "MANTER_FIXO_AQUI":
            self._cfg["device_id"] = str(uuid.uuid4())
            # Só persiste se o arquivo não existia antes
            if getattr(self, "_cfg_file_missing", False):
                self._save_config(force=True)

    # ── Config: load ──────────────────────────────────────────

    def _load_config(self):
        with self._lock:
            self._cfg = {}
            self._cfg_loaded_ok = False
            self._cfg_file_missing = False

            try:
                if not os.path.exists(self.config_path):
                    self._cfg_file_missing = True
                    self._cfg_loaded_ok = True
                    return

                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if not isinstance(data, dict):
                    raise ValueError("config JSON não é um objeto/dict")

                self._cfg = data
                self._cfg_loaded_ok = True
                return

            except Exception as e:
                print(f"[ICEQ][CLOUD] Falha carregando config: {e}")

            # Tenta recuperar do backup
            bak_path = self.config_path + ".bak"
            try:
                if os.path.exists(bak_path):
                    with open(bak_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        self._cfg = data
                        self._cfg_loaded_ok = True
                        print("[ICEQ][CLOUD] Config recuperado do .bak")
                        return
            except Exception as e:
                print(f"[ICEQ][CLOUD] Falha recuperando .bak: {e}")

            self._cfg = {}
            self._cfg_loaded_ok = False

    # ── Config: save ──────────────────────────────────────────

    def _save_config(self, force: bool = False):
        """
        Escrita atômica com backup.
        Blindagem: não sobrescreve config válido com dados incompletos.
        """
        with self._lock:
            if not force and not getattr(self, "_cfg_loaded_ok", False):
                print("[ICEQ][CLOUD] Save bloqueado: config não carregou OK.")
                return

            # Não sobrescreve config existente com cfg "pobre"
            if not force and os.path.exists(self.config_path):
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        disk_cfg = json.load(f)
                    if isinstance(disk_cfg, dict):
                        disk_has_core = all(
                            str(disk_cfg.get(k, "")).strip()
                            for k in ("base_url", "api_key", "machine_id")
                        )
                        mem_has_core = all(
                            str(self._cfg.get(k, "")).strip()
                            for k in ("base_url", "api_key", "machine_id")
                        )
                        if disk_has_core and not mem_has_core:
                            print("[ICEQ][CLOUD] Save bloqueado: cfg incompleto.")
                            return
                except Exception:
                    pass

            try:
                os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            except Exception:
                pass

            tmp_path = self.config_path + ".tmp"
            bak_path = self.config_path + ".bak"
            cfg = dict(self._cfg or {})

            # Garante device_id
            if not str(cfg.get("device_id", "")).strip():
                cfg["device_id"] = str(uuid.uuid4())

            # Preserva campos críticos do disco
            old = {}
            try:
                if os.path.exists(self.config_path):
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        old = json.load(f) or {}
            except Exception:
                old = {}

            for k in ("base_url", "api_key", "machine_id",
                      "firmware_version", "app_version", "resolved_ip"):
                if not str(cfg.get(k, "")).strip() and str(old.get(k, "")).strip():
                    cfg[k] = old[k]

            try:
                # Backup antes de salvar
                if os.path.exists(self.config_path):
                    try:
                        with open(self.config_path, "r", encoding="utf-8") as f:
                            current = f.read()
                        with open(bak_path, "w", encoding="utf-8") as f:
                            f.write(current)
                    except Exception:
                        pass

                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())

                os.replace(tmp_path, self.config_path)
                self._cfg = cfg

            except Exception as e:
                print(f"[ICEQ][CLOUD] Falha salvando config: {e}")
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass

    # ── Propriedades ──────────────────────────────────────────

    def is_configured(self) -> bool:
        with self._lock:
            base_url   = str(self._cfg.get("base_url",   "")).strip()
            api_key    = str(self._cfg.get("api_key",    "")).strip()
            machine_id = str(self._cfg.get("machine_id", "")).strip()
            return bool(base_url and api_key and machine_id)

    def get_last_error(self) -> str:
        with self._lock:
            return self._last_error

    def _utc_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _base_url(self) -> str:
        with self._lock:
            return str(self._cfg.get("base_url", "")).strip().rstrip("/")

    def _machine_id(self) -> str:
        with self._lock:
            return str(self._cfg.get("machine_id", "")).strip()

    def _device_id(self) -> str:
        with self._lock:
            return str(self._cfg.get("device_id", "")).strip()

    def _api_key(self) -> str:
        with self._lock:
            return str(self._cfg.get("api_key", "")).strip()

    def _firmware_version(self) -> str:
        with self._lock:
            return str(self._cfg.get("firmware_version", "1.0.0")).strip()

    def _app_version(self) -> str:
        with self._lock:
            return str(self._cfg.get("app_version", "1.0.0")).strip()

    def _resolved_ip(self):
        with self._lock:
            v = str(self._cfg.get("resolved_ip", "")).strip()
            return v if v else None

    # ── HTTP POST ─────────────────────────────────────────────

    def _post_json(self, url: str, payload: dict, timeout_s: float = 6.0):
        """
        Envia POST JSON usando TLS com IP pinning opcional.
        Retorna (status_code: int, body: str).
        Em caso de erro de conexão retorna (0, mensagem_erro).
        """
        from urllib.parse import urlparse

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        api_key = self._api_key()

        u = urlparse(url)
        host    = u.hostname or ""
        port    = int(u.port or 443)
        path    = u.path or "/"
        if u.query:
            path = f"{path}?{u.query}"

        ctx        = ssl.create_default_context()
        pinned_ip  = self._resolved_ip()

        try:
            conn = _PinnedIPHTTPSConnection(
                host=host,
                port=port,
                pinned_ip=pinned_ip,
                timeout=float(timeout_s),
                context=ctx,
            )
            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "Host": host,
            }
            conn.request("POST", path, body=data, headers=headers)
            resp   = conn.getresponse()
            body   = resp.read().decode("utf-8", errors="replace")
            status = int(resp.status)
            conn.close()
            return status, body
        except Exception as e:
            return 0, str(e)

    # ── Ping ──────────────────────────────────────────────────

    def send_ping(self) -> bool:
        """
        Mantém a máquina "online" no painel ICEQ Cloud.
        """
        if not self.is_configured():
            return False

        payload = {
            "machine_id":       self._machine_id(),
            "device_id":        self._device_id(),
            "timestamp":        self._utc_iso(),
            "firmware_version": self._firmware_version(),
            "app_version":      self._app_version(),
            "nonce":            str(uuid.uuid4()),
        }

        url = f"{self._base_url()}/ihm-ping"
        status, body = self._post_json(url, payload, timeout_s=5.0)

        if status in (200, 201):
            with self._lock:
                self._last_ok_ts  = time.time()
                self._last_error  = ""
            return True

        with self._lock:
            self._last_error = f"HTTP {status}: {body[:200]}"
        print(f"[ICEQ][CLOUD] ping HTTP={status} body={body[:200]}")
        return False

    # ── Logs ──────────────────────────────────────────────────

    def send_log(self, log_type: str, payload: dict,
                 tag: str = "ihm", severity: str = "info") -> bool:
        """
        Envia log estruturado para o ICEQ Cloud.

        Campos obrigatórios pela Edge Function:
          machine_id, log_type, payload, timestamp
        """
        if not self.is_configured():
            return False

        body = {
            "machine_id": self._machine_id(),
            "log_type":   str(log_type),
            "tag":        str(tag),
            "severity":   str(severity),
            "payload":    payload if isinstance(payload, dict) else {"msg": str(payload)},
            "timestamp":  self._utc_iso(),
        }

        url = f"{self._base_url()}/ihm-logs"
        status, resp = self._post_json(url, body, timeout_s=6.0)

        if status in (200, 201):
            with self._lock:
                self._last_ok_ts = time.time()
                self._last_error = ""
            return True

        with self._lock:
            self._last_error = f"HTTP {status}: {resp[:200]}"
        print(f"[ICEQ][CLOUD] logs HTTP={status} body={resp[:200]}")
        return False

    def send_startup_log(self, message: str = "IHM ICEQ startup") -> bool:
        """
        Log de boot — chamado uma vez na inicialização da IHM.
        """
        return self.send_log(
            log_type="startup",
            tag="ihm",
            severity="info",
            payload={
                "message":          str(message),
                "device_id":        self._device_id(),
                "firmware_version": self._firmware_version(),
                "app_version":      self._app_version(),
                "host":             socket.gethostname(),
            },
        )

    # ── Ping periódico ────────────────────────────────────────

    def start_periodic_ping(self, interval_s: float = 20.0) -> None:
        """
        Inicia thread daemon que envia ping a cada interval_s segundos.
        Seguro chamar múltiplas vezes (só inicia uma thread).
        """
        if getattr(self, "_ping_thread_running", False):
            return

        self._ping_thread_running = True

        def _worker():
            while True:
                try:
                    self.send_ping()
                except Exception as e:
                    print(f"[ICEQ][CLOUD] ping periódico erro: {e}")
                time.sleep(max(5.0, float(interval_s)))

        t = threading.Thread(target=_worker, daemon=True, name="iceq-cloud-ping")
        t.start()

    # ── DNS refresh ───────────────────────────────────────────

    def refresh_resolved_ip(self) -> bool:
        """
        Resolve o IP do host da base_url e persiste no config.
        Útil para atualizar o IP fixo antes de iniciar a operação,
        enquanto ainda há conectividade de rede confiável.
        """
        try:
            base = self._base_url()
            if not base:
                return False
            from urllib.parse import urlparse
            host = urlparse(base).hostname or ""
            if not host:
                return False
            ip = socket.gethostbyname(host)
            if ip:
                with self._lock:
                    self._cfg["resolved_ip"] = ip
                self._save_config()
                print(f"[ICEQ][CLOUD] resolved_ip atualizado: {host} → {ip}")
                return True
        except Exception as e:
            print(f"[ICEQ][CLOUD] refresh_resolved_ip falhou: {e}")
        return False
