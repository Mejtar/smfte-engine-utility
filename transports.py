from pathlib import Path
import os
import sys
import subprocess
import json
import uuid
from runner import run_command

# --- WINSCP_EXECUTABLE: inyectado desde engine.py via SETTINGS ---
# No se hardcodea aqui. Cada funcion lo recibe como parametro.

def smb_transfer(local_file, route, log, dry_run, winscp_path=None):
    """
    Transferencia SMB via Robocopy (silenciosa, sin logs de consola).
    winscp_path ignorado — SMB usa robocopy nativo.
    """
    transport = route["transport"]
    unc_path = transport["unc_path"]

    cmd = [
        "robocopy",
        str(local_file.parent),
        unc_path,
        local_file.name,
        "/R:0", "/W:0", "/COPY:DAT", "/Z",
        "/NJH", "/NJS", "/NDL", "/NFL", "/NP"
    ]

    result = run_command(cmd=cmd, log=log, dry_run=dry_run)
    if dry_run:
        return True

    if result.returncode >= 8:
        raise RuntimeError(
            f"robocopy_rc={result.returncode} "
            "(Fallo grave de red, permisos o directorio destino inexistente)"
        )

    remote_file = Path(unc_path) / local_file.name
    return remote_file.exists()


def sftp_transfer(local_file, route, log, dry_run=False, winscp_path=None):
    """
    Transferencia SFTP hacia Tandem/Unix via WinSCP (/stdin).
    winscp_path proviene de SETTINGS['paths']['winscp_executable'].
    """
    if not winscp_path:
        raise ValueError("winscp_path_not_provided — configurar paths.winscp_executable en settings.yaml")

    transport_config = route['transport']
    hostname = transport_config.get('host')
    port     = transport_config.get('port', 22)
    user     = transport_config.get('user')
    password = transport_config.get('password')
    remote_path = transport_config.get('remote')

    if not all([hostname, user, password, remote_path]):
        raise ValueError("missing_credentials_or_remote_path_in_config")

    if dry_run:
        log.debug(f"[DRY-RUN] sftp {local_file.name} -> {hostname}:{remote_path}")
        return True

    local_path_raw      = os.path.abspath(str(local_file))
    remote_destination  = f"{remote_path.rstrip('/')}/{local_file.name}"
    winscp_url          = f"sftp://{user}:{password}@{hostname}:{port}"

    winscp_commands = (
        f"open {winscp_url} -hostkey=*\n"
        "option confirm off\n"
        f'put "{local_path_raw}" "{remote_destination}"\n'
        "exit\n"
    )

    try:
        proc = subprocess.run(
            [winscp_path, "/stdin"],
            input=winscp_commands,
            capture_output=True,
            text=True,
            encoding='latin-1',
            timeout=120
        )
        if proc.returncode == 0 and "Error" not in proc.stdout and "failed" not in proc.stdout:
            log.debug(f"winscp_sftp_success file={local_file.name} dest={remote_destination}")
            return True
        raise RuntimeError(
            f"winscp_sftp_failed (Verifique credenciales, permisos o existencia de {remote_destination})"
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("timeout_expired (El host remoto no respondio en 120 segundos)")
    except Exception as e:
        raise RuntimeError(f"sftp_transport_error: {e}")


def winscp_smb_transfer(local_file, route, log, dry_run=False, winscp_path=None):
    """
    Transferencia SMB/UNC via WinSCP file:// (Windows-to-Windows).
    winscp_path proviene de SETTINGS['paths']['winscp_executable'].
    """
    if not winscp_path:
        raise ValueError("winscp_path_not_provided — configurar paths.winscp_executable en settings.yaml")

    transport_config = route['transport']
    unc_path = transport_config.get('unc_path')
    if not unc_path:
        raise ValueError("missing_unc_path_in_config")

    if dry_run:
        log.debug(f"[DRY-RUN] winscp_smb {local_file.name} -> {unc_path}")
        return True

    local_path_raw     = os.path.abspath(str(local_file))
    clean_unc          = unc_path.rstrip('\\')
    remote_destination = f"{clean_unc}\\{local_file.name}"

    winscp_commands = (
        "open file://\n"
        "option confirm off\n"
        f'put "{local_path_raw}" "{remote_destination}"\n'
        "exit\n"
    )

    try:
        proc = subprocess.run(
            [winscp_path, "/stdin"],
            input=winscp_commands,
            capture_output=True,
            text=True,
            encoding='latin-1',
            timeout=120
        )
        if proc.returncode == 0 and "Error" not in proc.stdout and "failed" not in proc.stdout:
            log.debug(f"winscp_smb_success file={local_file.name} dest={remote_destination}")
            return True
        raise RuntimeError(
            f"winscp_smb_failed (Fallo en copia local o permisos UNC en {remote_destination})"
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("timeout_expired (WinSCP local excedio el tiempo de espera)")
    except Exception as e:
        raise RuntimeError(f"winscp_smb_error: {e}")


def ctm_mft_transfer(local_file, route, log, dry_run=False, winscp_path=None):
    """
    Transferencia delegada a Job MFT de Control-M via ctmorder.
    winscp_path ignorado — transporte via CLI de BMC.
    """
    transport_config = route['transport']
    folder_name  = transport_config.get('folder_name')
    job_name     = transport_config.get('job_name')
    remote_path  = transport_config.get('remote')

    if not all([folder_name, job_name, remote_path]):
        raise ValueError("missing_folder_name_job_name_or_remote_path_in_config")

    local_path_raw     = os.path.abspath(str(local_file))
    remote_destination = f"{remote_path.rstrip('/')}/{local_file.name}"

    if dry_run:
        log.info(
            f"[DRY-RUN] ctmorder -FOLDER {folder_name} -NAME {job_name} "
            f"-ODATE odat -VARIABLE %%SRC_FILE {local_path_raw} "
            f"-VARIABLE %%DST_FILE {remote_destination}"
        )
        return True

    cmd = [
        "ctmorder",
        "-FOLDER", folder_name,
        "-NAME",   job_name,
        "-ODATE",  "odat",
        "-FORCE",  "Y",
        "-VARIABLE", "%%SRC_FILE", local_path_raw,
        "-VARIABLE", "%%DST_FILE", remote_destination
    ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding='latin-1', timeout=30
        )
        if proc.returncode == 0 and "error" not in proc.stdout.lower():
            log.debug(f"ctmorder_success job={job_name} folder={folder_name}")
            return True
        error_details = proc.stderr if proc.stderr else proc.stdout
        raise RuntimeError(
            f"ctmorder_failed (No se pudo ordenar el Job MFT. Detalle: {error_details.strip()})"
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("ctmorder_timeout (ctmorder excedio 30 segundos)")
    except Exception as e:
        raise RuntimeError(f"ctmorder_utility_error: {e}")


def ctm_aapi_json_transfer(local_file, route, log, dry_run=False, winscp_path=None):
    """
    Transferencia sincrona via Control-M Automation API (Jobs-as-Code JSON + ctm run).
    winscp_path ignorado — transporte via CLI de BMC.
    """
    transport_config = route['transport']
    ctm_server  = transport_config.get('controlm_server')
    cp_src      = transport_config.get('connection_profile_src', 'LOCAL_WINDOWS')
    cp_dst      = transport_config.get('connection_profile_dst')
    remote_path = transport_config.get('remote')

    if not all([ctm_server, cp_dst, remote_path]):
        raise ValueError("missing_aapi_parameters_in_config")

    local_path_raw     = os.path.abspath(str(local_file))
    remote_destination = f"{remote_path.rstrip('/')}/{local_file.name}"
    tx_id              = uuid.uuid4().hex[:8]
    job_name           = f"MFT_Job_{tx_id}"

    job_def = {
        "Type": "Folder",
        "ControlmServer": ctm_server,
        "ActiveRetention": 1,
        job_name: {
            "Type": "Job:FileTransfer",
            "ConnectionProfileSrc": cp_src,
            "ConnectionProfileDst": cp_dst,
            "FileTransfers": [{
                "Src": local_path_raw,
                "Dst": remote_destination,
                "TransferOption": "SrcDelete",
                "TransferType": "Binary"
            }]
        }
    }

    temp_dir      = Path("temporal")
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_json_path = temp_dir / f"transfer_{tx_id}.json"

    if dry_run:
        log.info(
            f"[DRY-RUN] AAPI job seria escrito en {temp_json_path}:\n"
            f"{json.dumps(job_def, indent=2)}"
        )
        return True

    with open(temp_json_path, "w", encoding="utf-8") as jf:
        json.dump(job_def, jf, indent=2)

    try:
        proc = subprocess.run(
            ["ctm", "run", str(temp_json_path)],
            capture_output=True, text=True, encoding='latin-1', timeout=120
        )
        if (proc.returncode == 0
                and "error"  not in proc.stdout.lower()
                and "failed" not in proc.stdout.lower()):
            log.debug(f"ctm_aapi_success: {proc.stdout.strip()}")
            return True
        error_details = proc.stderr if proc.stderr else proc.stdout
        raise RuntimeError(
            f"ctm_aapi_failed (Autenticacion o copia fallo. Detalle: {error_details.strip()})"
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("ctm_aapi_timeout (ctm run excedio 120 segundos)")
    except Exception as e:
        raise RuntimeError(f"ctm_aapi_utility_error: {e}")
    finally:
        if temp_json_path.exists():
            temp_json_path.unlink()


TRANSPORTS = {
    "smb":           smb_transfer,
    "sftp":          sftp_transfer,
    "winscp_smb":    winscp_smb_transfer,
    "ctm_mft":       ctm_mft_transfer,
    "ctm_aapi_json": ctm_aapi_json_transfer,
}
