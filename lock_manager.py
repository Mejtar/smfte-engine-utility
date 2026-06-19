import os
import sys
import logging
from pathlib import Path

# Detección de plataforma para stdlib correcto
IS_WINDOWS = sys.platform == "win32"

try:
    if IS_WINDOWS:
        import msvcrt
    else:
        import fcntl
except ImportError:
    # Fallback para entornos muy exóticos, aunque no aplica al target (Win2019)
    pass

class LockManager:
    """
    GAP_2: Control de Concurrencia vía File Locking.
    
    Garantiza que solo una instancia de MFT-Orchestrator se ejecute por FIID a la vez.
    Previene:
    1. Race conditions en el registro de transacciones (GAP_1).
    2. Acceso concurrente a archivos en incoming/historico.
    """
    
    def __init__(self, lock_file_path: Path, log: logging.Logger, timeout: int = 0):
        self.lock_file_path = lock_file_path
        self.log = log
        self.timeout = timeout
        self.lock_fd = None

    def __enter__(self):
        self._acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._release()

    def _acquire(self):
        """Intenta adquirir el lock. Si timeout=0, falla rápido."""
        self.lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Abrir archivo en modo lectura/escritura (crea si no existe)
        # Usamos 'w+' para asegurar que podemos escribir el PID si fuera necesario (debug)
        try:
            self.lock_fd = open(self.lock_file_path, 'w')
        except IOError as e:
            self.log.critical(f"LOCK_OPEN_FAIL: {e}")
            raise RuntimeError(f"No se puede abrir el archivo de lock: {self.lock_file_path}")

        self.log.info(f"LOCK_ACQUIRING: {self.lock_file_path.name}")

        if IS_WINDOWS:
            # Windows: msvcrt.locking(fd, mode, size)
            # LK_NBLCK: Non-blocking lock (falla inmediatamente si ocupado)
            try:
                msvcrt.locking(self.lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
                self.log.info("LOCK_ACQUIRED_WINDOWS")
            except OSError:
                self.lock_fd.close()
                self._raise_busy_exception()
        else:
            # Linux/Mac: fcntl.flock(fd, operation)
            # LOCK_EX | LOCK_NB: Exclusive + Non-blocking
            try:
                fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.log.info("LOCK_ACQUIRED_POSIX")
            except IOError:
                self.lock_fd.close()
                self._raise_busy_exception()

    def _release(self):
        """Libera el lock y cierra el descriptor."""
        if not self.lock_fd:
            return
            
        try:
            if IS_WINDOWS:
                msvcrt.locking(self.lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_UN)
            
            self.lock_fd.close()
            
            # Opcional: Borrar archivo de lock al salir limpio
            if self.lock_file_path.exists():
                self.lock_file_path.unlink()
                
            self.log.info("LOCK_RELEASED")
        except Exception as e:
            self.log.warning(f"LOCK_RELEASE_WARN: {e}")

    def _raise_busy_exception(self):
        """Error estandarizado para cuando el engine ya está corriendo."""
        msg = (f"PROCESS_LOCKED: Otra instancia del engine está ejecutándose "
               f"o el archivo {self.lock_file_path} está bloqueado.")
        self.log.error(msg)
        raise RuntimeError(msg)