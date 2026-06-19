import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Set, Tuple

class TransactionRegistry:
    """
    GAP_1: Implementación de Idempotencia (File-Based + In-Memory Cache).
    
    Arquitectura:
    1. Storage: Append-Only JSONL (journal/registry.log).
    2. Check: In-Memory Set Lookup (O(1)).
    3. Cache: Write-Through.
    """
    
    def __init__(self, registry_path: Path, log: logging.Logger):
        self.registry_path = registry_path
        self.log = log
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Set[Tuple[str, str]] = self._load_cache()

    def _load_cache(self) -> Set[Tuple[str, str]]:
        cache = set()
        if not self.registry_path.exists():
            return cache
            
        self.log.info(f"REGISTRY_LOADING: indexando historial desde {self.registry_path}")
        try:
            with open(self.registry_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        key = (record.get("file_name"), record.get("sha256_hash"))
                        if key[0] and key[1]: 
                            cache.add(key)
                    except json.JSONDecodeError:
                        continue
            self.log.debug(f"REGISTRY_LOADED: {len(cache)} registros indexados.")
        except Exception as e:
            self.log.warning(f"REGISTRY_LOAD_FAIL: {e}. Iniciando con cache vacía.")
        return cache

    def is_processed(self, file_path: Path, sha256_hash: str, fiid: str) -> bool:
        return (file_path.name, sha256_hash) in self._cache

    def register_success(self, tx_uuid: str, file_path: Path, sha256_hash: str, fiid: str):
        record = {
            "tx_uuid": tx_uuid,
            "file_name": file_path.name,
            "sha256_hash": sha256_hash,
            "fiid": fiid,
            "ts": datetime.now().isoformat()
        }
        
        key = (file_path.name, sha256_hash)
        
        if key in self._cache:
            self.log.debug(f"REGISTRY_DUPLICATE_SKIP: {file_file.name} ya en cache.")
            return

        try:
            with open(self.registry_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            
            self._cache.add(key) # FIX INDENTACIÓN
            self.log.debug(f"REGISTRY_APPEND: tx={tx_uuid} file={file_path.name}")
            
        except Exception as e:
            self.log.critical(f"REGISTRY_WRITE_FAIL: tx={tx_uuid} error={e}")