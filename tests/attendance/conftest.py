import io

import pytest
from PIL import Image


@pytest.fixture(autouse=True)
def selfie_stage_on(settings):
    settings.OTS_SELFIE_STAGE_ENABLED = True


def jpeg(w=480, h=360, fmt="JPEG"):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 90, 60)).save(buf, fmt)
    return buf.getvalue()
