import sys
import time
import json
import uuid
import socket
import getpass
import logging
import hashlib
import shutil
import os
import yaml
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from resolver import Resolver
from transports import TRANSPORTS
from registry import TransactionRegistry

SETTINGS_PATH = Path("config/settings.yaml")

def load_environment_settings():
    if not SETTINGS_PATH.exists():
        # Defaults para modo Synthetic / Linux sin config
        return {
            "paths": {
                "base_incoming": "/tmp/mft_incoming",
                "links_config": "config/links.yaml.example",
                "log_file": "logs/mftp.log",
                "registry_db": "journal/transactions.db",
                "winscp_executable": "/usr/bin/false"
            }
        }
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def get_setting(key, default):
    try:
        parts = key.split(".")
        val = SETTINGS
        for p in parts:
            val = val[p]
        return val
    except (KeyError, TypeError):
        return default

SETTINGS = load_environment_settings()

YAML_PATH = Path(get_setting("paths.links_config", "config/links.yaml.example"))
LOG_PATH = Path(get_setting("paths.log_file", "logs/mftp.log"))
REGISTRY_PATH = Path(get_setting("paths.registry_db", "journal/transactions.db"))
# ----------------------------------

class MFTOrchestrator:
    def __init__(self, fiid: str, debug: bool = False, dry_run: bool = False, synthetic: bool = False):
        self.fiid = fiid
        self.debug = debug
        self.dry_run = dry_run
        self.synthetic = synthetic
        
        if self.synthetic:
            self.dry_run = True
            
        self.base_incoming_path = Path(get_setting("paths.base_incoming", "/tmp/mft_incoming"))
        self.winscp_path = get_setting("paths.winscp_executable", "")
        
        self.log = self._setup_logging()
        
        # GAP_1: Registry (Manejo de fallo en init para tests)
        try:
            self.registry = TransactionRegistry(registry_path=REGISTRY_PATH, log=self.log)
        except Exception as e:
            self.log.warning(f"Registry init failed: {e}. Running without persistence.")
            self.registry = None

        self.audit_entries = []

    def _setup_logging(self):
        """Configura y aisla el registro de logs"""
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
            
        logger = logging.getLogger("mftp")
        logger.handlers.clear()
        logger.propagate = False
        
        level = logging.DEBUG if self.debug else logging.INFO
        logger.setLevel(level)

        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
            fh.setLevel(level)
            formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        except Exception as e:
            print(f"[WARNING] No se pudo inicializar el archivo de log: {e}")

        if self.debug:
            sh = logging.StreamHandler(sys.stdout)
            sh.setLevel(level)
            sh.setFormatter(formatter)
            logger.addHandler(sh)

        return logger
    
    def _is_file_writable(self, file_path: Path) -> bool:
        """R1: Control de Escritura Atomica"""
        try:
            with open(file_path, "ab"):
                return True
        except IOError:
            return False

    def _calculate_sha256(self, file_path: Path) -> str:
        """Calcula SHA256 o mock en synthetic"""
        if self.synthetic:
            return "deadbeef_synthetic_hash_1234567890abcdef"
            
        hasher = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception as e:
            self.log.error(f"No se pudo calcular SHA-256 para {file_path.name}: {e}")
            return "unknown_hash"

    def _get_local_ip(self) -> str:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"

    def _extract_destination_ip(self, transport, t_type) -> str:
        try:
            if t_type == "sftp":
                return transport.get('host', 'unknown')
            else:
                unc = transport.get('unc_path', '')
                clean_unc = unc.replace('/', '\\').lstrip('\\')
                parts = [p for p in clean_unc.split('\\') if p]
                return parts[0] if parts else 'unknown'
        except Exception:
            return "unknown"

    def _check_host_connectivity(self, host: str, port: int, timeout: int = 3) -> bool:
        if self.synthetic: return True # Skip check en synthetic
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False

    def _validate_routes(self, routes):
        for route in routes:
            transport = route.get("transport")
            if not transport:
                raise ValueError(f"route={route.get('lnk')} missing_transport")
            t_type = transport.get("type")
            if t_type not in TRANSPORTS:
                raise ValueError(f"unsupported_transport={t_type}")

    def _safe_archive(self, local_file: Path, fs_historico: Path):
        """
        GAP_4_RESOLUTION: Archivo atómico o fallback seguro.
        """
        if self.synthetic: 
            self.log.info(f"[SYNTHETIC] Would archive {local_file.name}")
            return True

        dest_path = fs_historico / local_file.name
        
        try:
            os.replace(local_file, dest_path)
            self.log.info(f"ARCHIVE_ATOMIC_OK src={local_file.name} dst={dest_path.name}")
            return True
        except OSError as e:
            if "cross-device" in str(e).lower() or e.winerror == 17:
                self.log.warning(f"ARCHIVE_CROSS_DEVICE src={local_file.name} attempting fallback_copy")
            else:
                self.log.error(f"ARCHIVE_ATOMIC_FAIL src={local_file.name} error={e}")
                raise

            try:
                if dest_path.exists():
                    dest_path.unlink()
                shutil.copy2(local_file, dest_path)
                if local_file.stat().st_size != dest_path.stat().st_size:
                    raise IOError("SIZE_MISMATCH_POST_COPY")
                local_file.unlink()
                self.log.info(f"ARCHIVE_FALLBACK_OK src={local_file.name} dst={dest_path.name}")
                return True
            except Exception as fb_e:
                self.log.critical(f"ARCHIVE_FALLBACK_FAIL src={local_file.name} error={fb_e}")
                return False

    def _print_summary_panel(self, processed: int, success: int, failed: int, details: list):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        run_status = "EXITOSO" if failed == 0 else "FALLIDO"

        print("========================================================================")
        print(f"             MONITOR DE TRANSFERENCIA MSFTP (FIID: {self.fiid})            ")
        print("========================================================================")
        print(f"[FECHA / HORA] {now_str}")
        print(f"[ESTADO RUN]   {run_status}")
        print("------------------------------------------------------------------------")
        print(f"* Total Archivos Procesados : {processed}")
        print(f"* Transmitidos con exito    : {success}")
        print(f"* Fallidos / Omitidos       : {failed}")
        print("------------------------------------------------------------------------")
        print("DETALLES DE OPERACIONES:")
        for block in details:
            print(block)
        print("------------------------------------------------------------------------")
        print(f"[JOB FINISHED] STATUS: {run_status} | EXIT CODE: {0 if failed == 0 else 1}")
        print("========================================================================")

    # --- SYNTHETIC HELPERS ---
    def _get_mock_routes(self):
        return [
            {
                "lnk": "TEST_SFTP_SYNTHETIC",
                "fiid_filter": self.fiid,
                "patterns": ["TEST_*.TXT"],
                "transport": {
                    "type": "sftp",
                    "host": "127.0.0.1",
                    "port": 22,
                    "user": "test",
                    "password": "test",
                    "remote": "/incoming/"
                }
            },
            {
                "lnk": "TEST_SMB_SYNTHETIC",
                "fiid_filter": self.fiid,
                "patterns": ["BATCH_*.DAT"],
                "transport": {
                    "type": "smb",
                    "unc_path": f"\\\\server\\share\\{self.fiid}"
                }
            }
        ]

    def _get_mock_files(self):
        mock_files = []
        m1 = MagicMock(spec=Path)
        m1.name = "TEST_001.TXT"
        m1.stat.return_value.st_size = 1024 * 1024
        mock_files.append(m1)
        
        m2 = MagicMock(spec=Path)
        m2.name = "BATCH_A.DAT"
        m2.stat.return_value.st_size = 5 * 1024 * 1024
        mock_files.append(m2)
        return mock_files

    def run(self) -> bool:
        self.log.info(f"START fiid={self.fiid} mode={'SYNTHETIC' if self.synthetic else 'PROD'}")

        # --- BRANCH SYNTHETIC ---
        if self.synthetic:
            self.log.info("USING MOCK DATA: Routes and Files")
            routes = self._get_mock_routes()
            self._validate_routes(routes)
            
            transfer_details = []
            files_processed = 0
            files_success = 0
            files_failed = 0
            
            mock_files = self._get_mock_files()
            ip_origen = "192.168.1.100"
            user_operador = "synthetic_user"

            for local_file in mock_files:
                files_processed += 1
                is_writable = True 
                size_orig = local_file.stat().st_size
            
                matched_routes = []
                for route in routes:
                    for pattern in route.get("patterns", []):
                        import fnmatch # Importar aquí o al principio del archivo
                        if fnmatch.fnmatchcase(local_file.name.upper(), pattern.upper()):
                            matched_routes.append(route)
                            break
                if not matched_routes:
                    self.log.warning(f"NO MOCK MATCH for {local_file.name}")
                    continue

                tx_uuid = f"tx-synth-{uuid.uuid4().hex[:4]}"
                file_sha256 = self._calculate_sha256(local_file)
                
                for route in matched_routes:
                    t_type = route["transport"]["type"]
                    handler = TRANSPORTS[t_type]
                    
                    delivered = False
                    try:
                        self.log.info(f"SYNTHETIC_EXECUTION file={local_file.name} type={t_type}")
                        delivered = handler(
                            local_file=local_file,
                            route=route,
                            log=self.log,
                            dry_run=True, 
                            winscp_path=self.winscp_path
                        )
                    except Exception as e:
                        self.log.error(f"SYNTHETIC_ERROR: {e}")
                    
                    if delivered:
                        files_success += 1
                        if self.registry:
                            self.registry.register_success(tx_uuid, local_file, file_sha256, self.fiid)
                        
                        block_log = f"[SYNTHETIC] OK {local_file.name} -> {t_type}"
                        transfer_details.append(block_log)
                    else:
                        files_failed += 1
                        block_log = f"[SYNTHETIC] FAIL {local_file.name} -> {t_type}"
                        transfer_details.append(block_log)

            self._print_summary_panel(files_processed, files_success, files_failed, transfer_details)
            return files_failed == 0

        # --- BRANCH NORMAL (PROD) ---
        
        resolver = Resolver(
            yaml_path=YAML_PATH,
            fiid=self.fiid,
            log=self.log
        )

        routes = resolver.load()
        self._validate_routes(routes)

        base_incoming_path = self.base_incoming_path
        
        if not base_incoming_path.exists():
            self.log.error(f"BASE_INCOMING_NOT_FOUND path={base_incoming_path}")
            print(f"[CRITICAL_ERROR] No se encuentra la ruta de red de origen: {base_incoming_path}")
            return False

        matched_dirs = [
            d for d in base_incoming_path.iterdir()
            if d.is_dir() and d.name.startswith(self.fiid)
        ]

        if not matched_dirs:
            self.log.error(f"INCOMING_DIR_NOT_FOUND_FOR_FIID fiid={self.fiid} under base={base_incoming_path}")
            print(f"[CRITICAL_ERROR] No se encontro la carpeta de la entidad para el FIID {self.fiid}")
            return False

        folder_name_raw = matched_dirs[0].name
        parts = folder_name_raw.split('_')
        entity_display = parts[1].capitalize() if len(parts) > 1 else folder_name_raw.capitalize()

        fs_incoming = matched_dirs[0] / "incoming"

        source_dirs = {"incoming": fs_incoming} 
        
        for route in routes:
            subfolder = route.get("local_source_subfolder")
            if subfolder:
                subfolder_resolved = subfolder.replace("$FIID", self.fiid)
                source_dirs[subfolder_resolved] = matched_dirs[0] / subfolder_resolved

        transfer_details = []
        files_processed = 0
        files_success = 0
        files_failed = 0
        processed_files_set = set()

        ip_origen = self._get_local_ip()
        user_operador = getpass.getuser()

        for subfolder_name, fs_dir in source_dirs.items():
            if not fs_dir.exists():
                if subfolder_name != "incoming":
                    continue
                else:
                    self.log.error(f"INCOMING_NOT_FOUND path={fs_dir}")
                    print(f"[CRITICAL_ERROR] No existe la ruta 'incoming' para la entidad: {fs_dir}")
                    return False

            all_files = [f for f in fs_dir.iterdir() if f.is_file()]

            for local_file in all_files:
                file_key = (subfolder_name, local_file.name)
                if file_key in processed_files_set:
                    continue
                processed_files_set.add(file_key)

                matched = []
                for route in routes:
                    route_subfolder = route.get("local_source_subfolder", "incoming")
                    route_subfolder_resolved = route_subfolder.replace("$FIID", self.fiid)
                    
                    if route_subfolder_resolved == subfolder_name:
                        if resolver.match(local_file.name, [route]):
                            matched.append(route)

                if not matched:
                    continue

                files_processed += 1
                is_writable = self._is_file_writable(local_file)
                size_orig = local_file.stat().st_size
                time_str = datetime.now().strftime("%H:%M:%S")
                date_str = datetime.now().strftime("%d/%m/%Y")

                tx_uuid = f"tx-{uuid.uuid4().hex[:9]}-{datetime.now().year}"
                file_sha256 = self._calculate_sha256(local_file)
                
                # GAP_1 START: Check Idempotencia
                if self.registry and self.registry.is_processed(local_file, file_sha256, self.fiid):
                    self.log.info(f"SKIP_IDEMPOTENT file={local_file.name} sha256={file_sha256[:8]}...")
                    continue
                # GAP_1 END

                self.log.info(f"AUDIT_HASH file={local_file.name} sha256={file_sha256}")

                for route in matched:
                    transport = route["transport"]
                    t_type = transport["type"]
                    handler = TRANSPORTS[t_type]

                    if t_type == "sftp" or t_type == "winscp_smb":
                        remote_path = transport.get('remote', transport.get('unc_path', ''))
                        remote_destination = f"{remote_path.rstrip('/')}/{local_file.name}"
                    else:
                        remote_destination = f"{transport.get('unc_path', '')}\\\\{local_file.name}"

                    clean_dest = remote_destination.replace('\\\\', '/')
                    dest_display = f"{route['lnk']}/{clean_dest}".lower()
                    ip_destino = self._extract_destination_ip(transport, t_type)

                    if not is_writable:
                        files_failed += 1
                        error_msg = "El archivo aun no termina de descargarse en entrada"
                        
                        block_log_lines = [
                            f"[{date_str} {time_str}] | Entidad: {entity_display} (FIID: {self.fiid}) | Archivo: {local_file.name}",
                            "  * Estado Envio   : OMITIDO (LOCKED)",
                            f"  * Motivo Error   : {error_msg}, se tomara en la siguiente corrida"
                        ]
                        block_log = "\n".join(block_log_lines)
                        transfer_details.append(block_log)

                        audit_entry = {
                            "idTransferencia": tx_uuid,
                            "usuarioOperador": user_operador,
                            "ipOrigen": ip_origen,
                            "ipDestino": ip_destino,
                            "algoritmoChecksum": "SHA-256",
                            "checksum": "none",
                            "duracionTransferenciaSegundos": 0.0,
                            "velocidadPromedioKbs": 0.0,
                            "reintentosConexion": 0,
                            "tipoInformacion": route["lnk"],
                            "codigoError": "ERR_FILE_LOCKED",
                            "mensajeError": error_msg
                        }
                        self.audit_entries.append(audit_entry)
                        continue

                    # --- VALIDACION DE PRE-VUELO DE RED ---
                    if not self.dry_run and ip_destino != 'unknown':
                        target_port = 22 if t_type == "sftp" else 445
                        
                        if not self._check_host_connectivity(ip_destino, target_port):
                            files_failed += 1
                            error_msg = f"host_unreachable_port_{target_port}"
                            
                            block_log_lines = [
                                f"[{date_str} {time_str}] | Entidad: {entity_display} (FIID: {self.fiid}) | Archivo: {local_file.name}",
                                "  * Estado Envio   : FALLIDO",
                                f"  * Motivo Error   : {error_msg}"
                            ]
                            block_log = "\n".join(block_log_lines)
                            transfer_details.append(block_log)

                            audit_entry = {
                                "idTransferencia": tx_uuid,
                                "usuarioOperador": user_operador,
                                "ipOrigen": ip_origen,
                                "ipDestino": ip_destino,
                                "algoritmoChecksum": "SHA-256",
                                "checksum": file_sha256,
                                "duracionTransferenciaSegundos": 0.0,
                                "velocidadPromedioKbs": 0.0,
                                "reintentosConexion": 0,
                                "tipoInformacion": route["lnk"],
                                "codigoError": f"ERR_HOST_UNREACHABLE_{target_port}",
                                "mensajeError": error_msg
                            }
                            self.audit_entries.append(audit_entry)
                            continue
                

                    delivered = False
                    max_retries = 3
                    retry_delays = [5, 10, 15]
                    attempts_made = 0
                    start_time = time.perf_counter()
                    
                    last_exception = None
                    active_handler = handler
                    active_route = route
                    active_type = t_type
                    is_fallback_active = False
                    current_max_retries = 1 if t_type == "sftp" else max_retries

                    for attempt in range(current_max_retries + 1):
                        attempts_made = attempt + 1
                        try:
                            self.log.info(f"MATCH file={local_file.name} route={active_route['lnk']} transport={active_type} attempt={attempt + 1}")
                            delivered = active_handler(
                                local_file=local_file,
                                route=active_route,
                                log=self.log,
                                dry_run=self.dry_run,
                                winscp_path=self.winscp_path
                            )
                            if not delivered:
                                raise RuntimeError("winscp_transfer_failed")
                            break
                        except Exception as e:
                            last_exception = e
                            self.log.warning(f"Attempt {attempt + 1} failed for {local_file.name} via {active_type}: {e}")

                        
                            if active_type == "sftp" and (attempt == current_max_retries) and transport.get("fallback_job"):
                                self.log.warning(f"SFTP_ATTEMPTS_EXHAUSTED for {local_file.name}. Triggering Control-M Fallback...")
                                is_fallback_active = True
                                active_type = "ctm_mft"
                                active_handler = TRANSPORTS["ctm_mft"]
                                active_route = {
                                    "lnk": route["lnk"],
                                    "transport": {
                                        "type": "ctm_mft",
                                        "folder_name": transport.get("fallback_folder"),
                                        "job_name": transport.get("fallback_job"),
                                        "remote": transport.get("remote")
                                    }
                                }

                                self.log.info(f"Starting Fallback execution for {local_file.name} via {active_type}...")
                                for fb_attempt in range(max_retries + 1):
                                    attempts_made = fb_attempt + 1
                                    try:
                                        self.log.info(f"FALLBACK MATCH file={local_file.name} route={active_route['lnk']} transport={active_type} attempt={fb_attempt + 1}")
                                        delivered = active_handler(
                                            local_file=local_file,
                                            route=active_route,
                                            log=self.log,
                                            dry_run=self.dry_run,
                                            winscp_path=self.winscp_path
                                        )
                                        if not delivered:
                                            raise RuntimeError("ctm_mft_fallback_failed")
                                        break
                                    except Exception as fb_e:
                                        last_exception = fb_e
                                        self.log.warning(f"Fallback Attempt {fb_attempt + 1} failed for {local_file.name}: {fb_e}")
                                        if fb_attempt < max_retries:
                                            delay = retry_delays[fb_attempt]
                                            self.log.info(f"Retrying Fallback file={local_file.name} in {delay} seconds...")
                                            time.sleep(delay)
                                break

                            if attempt < current_max_retries:
                                delay = retry_delays[attempt]
                                self.log.info(f"Retrying file={local_file.name} in {delay} seconds...")
                                time.sleep(delay)

                    end_time = time.perf_counter()
                    duration = round(end_time - start_time, 2)
                    time_str_completed = datetime.now().strftime("%H:%M:%S")

                    speed_kbs = 0.0
                    if delivered and duration > 0:
                        speed_kbs = round((size_orig / 1024) / duration, 2)

                    if delivered:
                        files_success += 1
                        
                        block_log_lines = [
                            f"[{date_str} {time_str_completed}] | Entidad: {entity_display} (FIID: {self.fiid}) | Archivo: {local_file.name}",
                            f"  * Hash SHA-256   : {file_sha256}",
                            f"  * Tamano Origen  : {size_orig:,} bytes",
                            f"  * Tamano Destino : {size_orig:,} bytes",
                            "  * Estado Envio   : OK (Exitoso)",
                            f"  * Ruta Destino   : {dest_display}",
                            f"  * ID Transaccion : {tx_uuid}"
                        ]
                        block_log = "\n".join(block_log_lines)
                        transfer_details.append(block_log)

                        audit_entry = {
                            "idTransferencia": tx_uuid,
                            "usuarioOperador": user_operador,
                            "ipOrigen": ip_origen,
                            "ipDestino": ip_destino,
                            "algoritmoChecksum": "SHA-256",
                            "checksum": file_sha256,
                            "duracionTransferenciaSegundos": duration,
                            "velocidadPromedioKbs": speed_kbs,
                            "reintentosConexion": attempts_made,
                            "tipoInformacion": route["lnk"],
                            "codigoError": None,
                            "mensajeError": None
                        }
                        self.audit_entries.append(audit_entry)

                        # GAP_1 START: Register Success
                        if self.registry:
                            self.registry.register_success(tx_uuid, local_file, file_sha256, self.fiid)
                        # GAP_1 END

                        if not self.dry_run:
                            fs_historico = fs_incoming.parent / "historico"
                            fs_historico.mkdir(parents=True, exist_ok=True)
                            
                            archive_ok = self._safe_archive(local_file, fs_historico)
                            if not archive_ok:
                                self.log.error(f"MANUAL_INTERVENTION_REQUIRED file={local_file.name} archive_failed")

                        should_delete = route.get("delete") if not is_fallback_active else False
                        if should_delete and not self.dry_run:
                            if local_file.exists():
                                local_file.unlink()
                                self.log.info(f"DELETED file={local_file.name}")
                    else:
                        files_failed += 1
                        error_msg = str(last_exception) if last_exception else "Error en el canal o protocolo de transferencia"
                        
                        block_log_lines = [
                            f"[{date_str} {time_str_completed}] | Entidad: {entity_display} (FIID: {self.fiid}) | Archivo: {local_file.name}",
                            "  * Estado Envio   : FALLIDO",
                            f"  * Motivo Error   : {error_msg}"
                        ]
                        block_log = "\n".join(block_log_lines)
                        transfer_details.append(block_log)

                        audit_entry = {
                            "idTransferencia": tx_uuid,
                            "usuarioOperador": user_operador,
                            "ipOrigen": ip_origen,
                            "ipDestino": ip_destino,
                            "algoritmoChecksum": "SHA-256",
                            "checksum": file_sha256,
                            "duracionTransferenciaSegundos": duration,
                            "velocidadPromedioKbs": 0.0,
                            "reintentosConexion": attempts_made,
                            "tipoInformacion": route["lnk"],
                            "codigoError": "ERR_TRANSPORT_FAILED",
                            "mensajeError": error_msg
                        }
                        self.audit_entries.append(audit_entry)

        if files_processed == 0:
            self._print_summary_panel(0, 0, 0, ["  - (No se encontraron archivos en la carpeta de entrada)"])
        else:
            self._print_summary_panel(files_processed, files_success, files_failed, transfer_details)
            
        self.log.info(f"END fiid={self.fiid} errors={files_failed}")

        return files_failed == 0