import sys
from pathlib import Path

# --- MANDATORIO: Inyeccion de dependencias offline ---
VENDOR_PATH = Path(__file__).parent / "vendor"
if VENDOR_PATH.exists() and str(VENDOR_PATH) not in sys.path:
    sys.path.insert(0, str(VENDOR_PATH))

import argparse
from engine import MFTOrchestrator
from lock_manager import LockManager

# Configurar logger básico
import logging
log = logging.getLogger("mftp_main")
log.setLevel(logging.INFO)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fiid", required=True)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--synthetic", action="store_true", help="Modo prueba: bypass filesystem y usa mocks en memoria")
    args = parser.parse_args()

    lock_path = Path("journal") / f"lock_{args.fiid}.lck"

    try:
        # NOTA: En modo synthetic, quizás quieras desactivar el LockManager para testeo local rápido,
        # pero para simular realismo, lo mantenemos.
        with LockManager(lock_file_path=lock_path, log=log):
            orchestrator = MFTOrchestrator(
                fiid=args.fiid,
                debug=args.debug,
                dry_run=args.dry_run,
                synthetic=args.synthetic  # NUEVO PARAMETRO
            )
            success = orchestrator.run()
            sys.exit(0 if success else 1)

    except RuntimeError as e:
        if "PROCESS_LOCKED" in str(e):
            print(f"[BUSY] {e}")
            sys.exit(99)
        else:
            raise

if __name__ == "__main__":
    main()