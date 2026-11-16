"""F-TK-7: the recipient is offline. The receipt stays pending and shows "Not delivered yet"; after the
reconnect the page fetches what it missed and acks, and it becomes delivered with the server's time."""

from tests.tasks.test_receipts import (  # noqa: F401
    team,
)
