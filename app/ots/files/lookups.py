"""`__ilike`: a plain `col ILIKE pattern`, which the trigram indexes can serve.

Django's own `icontains` wraps the column in UPPER(), which an index on the column cannot answer. The
pattern is passed through as given, so the caller escapes % and _ (see selectors._like).
"""

from __future__ import annotations

from typing import Any

from django.db.models import CharField, Lookup


@CharField.register_lookup
class ILike(Lookup):  # type: ignore[type-arg]
    lookup_name = "ilike"

    def as_sql(self, compiler: Any, connection: Any) -> tuple[str, list[Any]]:
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return f"{lhs} ILIKE {rhs}", [*lhs_params, *rhs_params]
