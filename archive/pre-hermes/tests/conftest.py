"""Block outbound delivery for every pytest run in this directory.

Belt and braces with _state_isolation, which does the same for suites run as
plain scripts. A suite added later that forgets both would still be caught by
this file, because pytest imports conftest before collecting anything.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _no_outbound  # noqa: E402,F401
