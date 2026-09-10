import pytest

from src.api.pagination import chunk_codes


def test_chunk_codes_splits_into_even_batches():
    batches = list(chunk_codes([1, 2, 3, 4], chunk_size=2))
    assert batches == [["1", "2"], ["3", "4"]]


def test_chunk_codes_handles_remainder():
    batches = list(chunk_codes(["a", "b", "c"], chunk_size=2))
    assert batches == [["a", "b"], ["c"]]


def test_chunk_codes_empty_input():
    assert list(chunk_codes([], chunk_size=5)) == []


def test_chunk_codes_rejects_non_positive_size():
    with pytest.raises(ValueError):
        list(chunk_codes([1, 2], chunk_size=0))
