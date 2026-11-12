"""Seams for modules that do not exist yet. Each has a safe default until its owner replaces it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# M08 (files) replaces this: does the assignment have at least one SUBMISSION file linked to it?
has_submission_file: Callable[[Any], bool] = lambda assignment: False  # noqa: E731


def link_reference_files(task: Any, file_ids: Any, actor: Any) -> None:
    """M08 replaces this to link REFERENCE files. Until the Files module exists nothing can be attached."""
    if file_ids:
        from ots.core.errors import ValidationFailed

        raise ValidationFailed("Files can be attached once the Files module is ready.")
