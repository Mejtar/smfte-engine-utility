import subprocess
import time
import logging

DEFAULT_TIMEOUT = 300

DEFAULT_RETRIES = 2

DEFAULT_RETRY_DELAY = 3


def run_command(
    cmd,
    log,
    input_data=None,
    timeout=DEFAULT_TIMEOUT,
    retries=DEFAULT_RETRIES,
    retry_delay=DEFAULT_RETRY_DELAY,
    dry_run=False
):

    if log.isEnabledFor(logging.DEBUG):

        log.debug(
            f"COMMAND {' '.join(cmd)}"
        )

    if dry_run:
        return None

    last_error = None

    for attempt in range(1, retries + 2):

        try:

            result = subprocess.run(
                cmd,
                input=input_data,
                text=True,
                capture_output=True,
                timeout=timeout
            )

            if log.isEnabledFor(logging.DEBUG):

                if result.stdout:
                    log.debug(result.stdout)

                if result.stderr:
                    log.debug(result.stderr)

            return result

        except subprocess.TimeoutExpired as e:

            last_error = e

            log.warning(
                f"RETRY timeout attempt={attempt}"
            )

        except Exception as e:

            last_error = e

            log.warning(
                f"RETRY error={e} attempt={attempt}"
            )

        if attempt <= retries:
            time.sleep(retry_delay)

    raise RuntimeError(last_error)