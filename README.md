**INTERVENCIÓN N3 (Completo - Documentación Técnica)**

**Archivo: `README.md`**

```markdown
# MFT Engine

Declarative MFT orchestration engine for banking file transfer pipelines (SMB, SFTP, Control-M).

## Abstract

Orquestador modular diseñado para entornos financieros regulados (BCRA). Gestionar la transferencia, archivado atómico y auditoría de archivos entre sistemas Windows Legacy (Tandem/Mainframe) y destinos modernos.

## Architecture

El sistema sigue un patrón de inyección de dependencias y procesamiento secuencial por lotes.

1.  **Entry Point**: `MSFTP-Main.py` parsea argumentos (`--fiid`, `--dry-run`, `--synthetic`) e inicializa el lock de concurrencia.
2.  **Resolver**: Carga `links.yaml`, filtra rutas por entidad (FIID) y aplica patrones `fnmatch` a los nombres de archivo.
3.  **Engine**:
    *   Escanea directorios `incoming/`.
    *   Calcula SHA256 y verifica estado en `TransactionRegistry` (Idempotencia).
    *   Ejecuta `Handler` específico del transporte.
    *   Verifica atomicidad del archivado (`os.replace` / `fallback`).
4.  **Transports**:
    *   `smb`: Robocopy nativo.
    *   `sftp`: WinSCP (Production) o Paramiko (Failover Nativo).
    *   `ctm`: Integration via API.

## Quick Start

### Modo Synthetic (Testing / CI)

Ejecuta el pipeline completo simulando filesystem y red. Requiere solo Python stdlib.

```bash
python3 MSFTP-Main.py --fiid 0311 --synthetic --debug
```

### Production Execution

Ejecuta orquestación real. Requiere `config/settings.yaml` y `config/links.yaml`.

```bash
python3 MSFTP-Main.py --fiid 0000
```

## Features iniciales implementadas

*   **GAP_1 (Idempotency):** `TransactionRegistry` en modo JSONL con cache en memoria O(1). Previene reprocesamiento tras reinicios.
*   **GAP_2 (Concurrency):** `LockManager` usando Kernel Locks (`msvcrt`/`fcntl`). Previene race conditions en instancias simultáneas.
*   **GAP_4 (Atomicity):** Archivado seguro vía `os.replace` (volumen único) con fallback cross-device.
*   **Synthetic Mode:** Bypass de dependencias de infraestructura para testing unitario y validacion de pipelines en CI.

## Configuration

*   `config/settings.yaml`: Paths de entorno, binarios WinSCP, logs.
*   `config/links.yaml`: Rutas declarativas. Estructura:
    ```yaml
    routes:
      - lnk: "NOMBRE"
        fiid_filter: "0000" # <-- tambien puede matchear "CARACTERES" o "MEZCLA1232"
        patterns: ["BATCH_*.DAT"]
        transport:
          type: "sftp" # <-- tipo de transporte (SMB, SFTP, MCONTROL-API, etc)
          host: "192.168.1.1" # <-- FQDN o Dominio
          # ... creds
    ```

## Pending Gaps

Los siguientes items fueron identificados en la auditoria y permanecen pendientes:

*   **GAP_3 (Partial Files):** Detección de archivos bloqueados implementada via `_is_file_writable`, pero susceptible a race conditions finas si el lock se libera entre el check y el transfer.
*   **GAP_14 (Security):** Sanitización de logs. `transports.py` puede exponer passwords en `debug` mode si se loguean las cadenas de comando completas.
*   **GAP_10 (Log Rotation):** `registry.log` crece indefinidamente. Requiere script de mantenimiento o rotación por tamaño.
*   **GAP_12 (Progress):** Falta reporte de progreso para archivos > 2GB.

## License

Propietario.
```
