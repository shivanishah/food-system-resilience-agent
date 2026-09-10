"""Request chunking for large FAOSTAT code lists.

The FAOSTAT data endpoint (``/en/data/{domain}``) does not expose
server-side pagination (no page/limit/offset parameters or total-count
metadata were found on any discovery or data endpoint) — a request either
returns its full result set or fails. The one practical limit is the
comma-separated code list in ``area=``/``item=``/``element=``/``year=``
query parameters getting too long for a single GET request. ``chunk_codes``
lets callers stay well under that by splitting large code lists into
batches that ``FAOSTATClient.get_data`` issues as separate requests and
concatenates.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator


def chunk_codes(codes: Iterable[str], chunk_size: int = 200) -> Iterator[list[str]]:
    """Yield successive ``chunk_size``-sized batches of ``codes``.

    Args:
        codes: Code values (e.g. area or item codes) to batch.
        chunk_size: Maximum codes per batch. Must be positive.

    Raises:
        ValueError: If ``chunk_size`` is not positive.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    batch: list[str] = []
    for code in codes:
        batch.append(str(code))
        if len(batch) == chunk_size:
            yield batch
            batch = []
    if batch:
        yield batch
