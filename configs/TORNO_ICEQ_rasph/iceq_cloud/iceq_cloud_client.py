#!/usr/bin/env python3
import json
import os
import time
import uuid
import threading
import ssl
import socket
import http.client
from urllib.parse import urlparse
from urllib import request, error
from http.client import HTTPSConnection

class _PinnedIPHTTPSConnection(http.client.HTTPSConnection):
    """
    HTTPSConnection que conecta em um IP fixo, mas mantém o Host/SNI do domínio.
    Isso evita quebra de TLS/certificado ao "pin" por IP.
    """

    def __init__(self, host, pinned_ip=None, **kwargs):
        # kwargs pode conter: context, timeout, source_address, etc.
        self._orig_host = host
        self._pinned_ip = pinned_ip or host

        kwargs.pop("resolved_ip", None)
        kwargs.pop("pinned_ip", None)

        # IMPORTANTÍSSIMO: preservar o SSLContext (senão quebra com "no attribute context")
        self.context = kwargs.get("context", None)

        # Chama super com o host ORIGINAL (domínio) para o stack manter a semântica correta
        super().__init__(host, **kwargs)

    def connect(self):
        # Abre o socket indo para o IP fixo, mas com SNI no domínio
        sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address
        )

        # Se houver tunelamento (proxy), respeitar
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            raw_sock = self.sock
        else:
            raw_sock = sock

        # Wrap TLS com server_hostname = domínio (SNI)
        ctx = self.context or ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=self._orig_host)

class _PinnedIPHTTPSHandler(request.HTTPSHandler):
    def __init__(self, host, pinned_ip):
        super().__init__()
        self._host = host
        self._pinned_ip = pinned_ip

    def https_open(self, req):
        return self.do_open(self._conn_factory, req)

    def _conn_factory(self, host, **kwargs):
        return _PinnedIPHTTPSConnection(self._host, pinned_ip=self._pinned_ip, **kwargs)


class IceqCloudClient:
    """
    Cliente mínimo e robusto para o ICEQ Cloud (Supabase Edge Functions).
    - Ping:  POST {base_url}/ihm-ping
    - Logs:  POST {base_url}/ihm-logs
    Headers:
      Content-Type: application/json
      x-api-key: <api_key_da_maquina>
    """

    def __init__(self, config_path: str):
        self.config_path = str(config_path)
        self._lock = threading.RLock()
        self._cfg = {}
        self._load_config()

        # fallback: firmware (pode ajustar depois)
        self._firmware_version = self._cfg.get("firmware_version", "1.0.0")

        # IP fixo (opcional) para evitar falhas de DNS durante LinuxCNC/Mesa
        self._resolved_ip = str(self._cfg.get("resolved_ip", "")).strip() or None

        # device_id único por IHM (gera e persiste se faltar)
        # BLINDAGEM: só persiste device_id automaticamente quando o arquivo NÃO existia.
        # Se o load falhou (JSON corrompido), NÃO salvamos nada para não apagar base_url/api_key/machine_id.
        if not self._cfg.get("device_id"):
            self._cfg["device_id"] = self._gen_device_id()
            if getattr(self, "_cfg_file_missing", False):
                self._save_config(force=True)

    def _gen_device_id(self) -> str:
        # UUID4 é suficiente para unicidade prática em produção
        return str(uuid.uuid4())

    def _load_config(self):
        """
        BLINDAGEM:
        - Se o JSON estiver inválido, NÃO zera e NÃO salva nada depois.
        - Tenta restaurar de um .bak.
        """
        with self._lock:
            self._cfg = {}
            self._cfg_loaded_ok = False
            self._cfg_file_missing = False

            try:
                if not os.path.exists(self.config_path):
                    self._cfg_file_missing = True
                    self._cfg_loaded_ok = True
                    self._cfg = {}
                    return

                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if not isinstance(data, dict):
                    raise ValueError("config JSON não é um objeto/dict")

                self._cfg = data
                self._cfg_loaded_ok = True
                return

            except Exception as e:
                print(f"[ICEQ][CLOUD] Falha carregando config (mantendo proteção): {e}")

            # Se chegou aqui, load falhou. Tenta recuperar do backup.
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

            # Último recurso: mantém vazio, mas marca como NÃO ok para impedir saves automáticos.
            self._cfg = {}
            self._cfg_loaded_ok = False

    def _save_config(self, force: bool = False):
        """
        BLINDAGEM DEFINITIVA
        - Escrita atômica (.tmp -> rename)
        - Mantém backup .bak
        - Não permite sobrescrever config existente com um cfg "pobre"
          (ex.: só device_id) a menos que force=True
        - Se o load não foi OK, não salva automaticamente (evita apagar config bom)
        """
        with self._lock:
            # Se o load falhou (JSON corrompido), bloquear save automático
            if not force and not getattr(self, "_cfg_loaded_ok", False):
                print("[ICEQ][CLOUD] Save bloqueado: config não carregou OK (proteção anti-apagar).")
                return

            # Se já existe arquivo e o cfg atual está "pobre", bloqueia sobrescrita.
            if not force and os.path.exists(self.config_path):
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        disk_cfg = json.load(f)
                    if isinstance(disk_cfg, dict):
                        disk_has_core = all(str(disk_cfg.get(k, "")).strip() for k in ("base_url", "api_key", "machine_id"))
                        mem_has_core = all(str(self._cfg.get(k, "")).strip() for k in ("base_url", "api_key", "machine_id"))
                        if disk_has_core and not mem_has_core:
                            print("[ICEQ][CLOUD] Save bloqueado: cfg incompleto evitar apagar config válido.")
                            return
                except Exception:
                    # Se não conseguir ler o do disco, não bloqueia apenas por isso
                    pass

            # Garante pasta
            try:
                os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            except Exception:
                pass

            tmp_path = self.config_path + ".tmp"
            bak_path = self.config_path + ".bak"

            # Snapshot do cfg em memória
            cfg = dict(self._cfg or {})

            # device_id deve sempre existir (mas não deve provocar “degradação”)
            if not str(cfg.get("device_id", "")).strip():
                cfg["device_id"] = self._gen_device_id()

            # Preserva campos críticos do arquivo atual, se existirem lá e faltarem aqui
            old = {}
            try:
                if os.path.exists(self.config_path):
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        old = json.load(f) or {}
            except Exception:
                old = {}

            for k in ("base_url", "api_key", "machine_id", "firmware_version", "app_version", "resolved_ip"):
                if (not str(cfg.get(k, "")).strip()) and str(old.get(k, "")).strip():
                    cfg[k] = old.get(k)

            # Escreve tmp (atômico)
            try:
                # Backup antes de salvar (se existir)
                try:
                    if os.path.exists(self.config_path):
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
                print(f"[ICEQ][CLOUD] Falha salvando config (atomic): {e}")
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass

    def is_configured(self) -> bool:
        with self._lock:
            base_url = str(self._cfg.get("base_url", "")).strip()
            api_key = str(self._cfg.get("api_key", "")).strip()
            machine_id = str(self._cfg.get("machine_id", "")).strip()
            return bool(base_url and api_key and machine_id)

    def _post_json(self, url: str, payload: dict, timeout_s: float = 5.0):
        data = json.dumps(payload).encode("utf-8")

        with self._lock:
            api_key = str(self._cfg.get("api_key", "")).strip()

        u = urlparse(url)
        host = u.hostname or ""
        port = int(u.port or 443)
        path = u.path or "/"
        if u.query:
            path = f"{path}?{u.query}"

        # Contexto SSL padrão do sistema (validação de cert ativa)
        ctx = ssl.create_default_context()

        # Se tiver IP fixo configurado, conecta no IP, mas mantém SNI/Host do domínio
        pinned_ip = self._resolved_ip if (self._resolved_ip and host) else None

        try:
            conn = _PinnedIPHTTPSConnection(
                host=host,                 # domínio (SNI/cert)
                port=port,
                pinned_ip=pinned_ip,       # IP alvo do socket
                timeout=float(timeout_s),
                context=ctx
            )

            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                # Host explícito garante que o HTTP vá para o domínio correto mesmo conectando no IP
                "Host": host,
            }

            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            status = int(resp.status)
            conn.close()
            return status, body

        except Exception as e:
            return 0, str(e)

    def refresh_resolved_ip(self, host: str):
        try:
            import socket
            ip = socket.gethostbyname(host)
            if ip and isinstance(ip, str):
                with self._lock:
                    self._cfg["resolved_ip"] = ip
                self._save_config()
                return True
        except Exception:
            pass
        return False

    def _base_url(self) -> str:
        """
        Retorna SEMPRE a base_url do config (com o domínio).
        O "pin" por IP é aplicado apenas na camada de conexão (TLS/SNI),
        nunca trocando o host do URL, para não quebrar handshake/certificado.
        """
        with self._lock:
            base = str(self._cfg.get("base_url", "")).strip().rstrip("/")

        return base

    def _machine_id(self) -> str:
        with self._lock:
            return str(self._cfg.get("machine_id", "")).strip()

    def _device_id(self) -> str:
        with self._lock:
            return str(self._cfg.get("device_id", "")).strip()

    def send_ping(self):
        """
        Mantém a máquina "online" no painel.
        """
        if not self.is_configured():
            return False

        url = f"{self._base_url()}/ihm-ping"
        payload = {
            "machine_id": self._machine_id(),
            "device_id": self._device_id(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "firmware_version": self._firmware_version,
        }

        status, body = self._post_json(url, payload, timeout_s=4.0)
        if status not in (200, 201):
            print(f"[ICEQ][CLOUD] ping HTTP={status} body={body}")
            return False
        return True

    def send_startup_log(self, message: str):
        """
        Log de boot (startup).
        """
        return self.send_log(
            log_type="startup",
            payload={"message": str(message)},
            severity="info",
        )


    def send_log(self, tag: str, payload: dict, log_type: str = "info") -> bool:
        """
        Envia log estruturado para o ICEQ Cloud.
        Campos obrigatórios exigidos pela Edge Function:
        - machine_id
        - log_type
        - payload
        - timestamp (ISO 8601)
        """

        if not self.is_configured():
            print("[ICEQ][CLOUD] send_log ignorado: cliente não configurado.")
            return False

        body = {
            "machine_id": self._machine_id(),
            "log_type": str(log_type),
            "tag": str(tag),
            "payload": payload if isinstance(payload, dict) else {"msg": str(payload)},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }

        url = f"{self._base_url()}/ihm-logs"

        status, resp = self._post_json(url, body)

        if status in (200, 201):
            return True

        print(f"[ICEQ][CLOUD] logs HTTP={status} body={resp}")
        return False