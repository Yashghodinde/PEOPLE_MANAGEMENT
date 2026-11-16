"""F-LV-2: the SSE connection limit. A 4th connection closes the oldest; the process limit refuses with 503."""

from tests.live.test_hub import (  # noqa: F401
    test_a_fourth_connection_closes_the_oldest as test_fourth_connection,
)
