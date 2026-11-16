"""F-LV-1: dispatcher lag. Events waiting over 60 s raise the live.lag alert; it clears when they are sent."""

from tests.live.test_hub import test_a_stalled_dispatcher_raises_the_lag_alert as test_stalled_dispatcher  # noqa: F401
