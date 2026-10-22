"""A throwaway Postgres for running the tests by hand (the readiness runner starts its own).

    python scripts/pgtest.py            # prints the URI, keeps running until Ctrl+C
    $env:FLEET_TEST_DATABASE_URL = "<that uri>"; python -m pytest -q
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from readiness import pg  # noqa: E402

if __name__ == "__main__":
    uri = pg.start(Path(__file__).resolve().parents[1] / "readiness" / ".pg")
    print(uri, flush=True)
    print("FLEET_TEST_DATABASE_URL=" + uri, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        pg.stop()
