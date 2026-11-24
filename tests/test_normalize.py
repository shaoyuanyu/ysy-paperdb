from __future__ import annotations

from paperdb.db.models import IdentifierScheme
from paperdb.papers.service import normalize_identifier, normalize_title, title_word
from paperdb.venues.normalize import (
    ccf_field_key,
    dblp_series_key,
    normalize_issn,
    normalize_name,
    parse_issn_pair,
)


def test_normalize_venue_name() -> None:
    assert normalize_name("The   Neural Information Processing Systems") == (
        "neural information processing systems"
    )
    assert normalize_name("ACM SIGCOMM 2024") == "acm sigcomm"
    assert normalize_name("Data & Knowledge Engineering") == "data and knowledge engineering"


def test_dblp_key() -> None:
    assert dblp_series_key("https://dblp.org/db/conf/nips/") == "conf/nips"
    assert dblp_series_key("https://dblp.org/db/journals/pami/") == "journals/pami"
    assert dblp_series_key(None) is None


def test_issn() -> None:
    assert normalize_issn("1553-877X") == "1553877X"
    assert parse_issn_pair("1553-877X/1553-877X") == ("1553877X", "1553877X")
    assert parse_issn_pair("0162-8828") == ("01628828", None)


def test_ccf_field_key() -> None:
    assert ccf_field_key("计算机体系结构/并行与分布计算/存储系统") == "计算机体系结构"
    assert ccf_field_key(None) is None


def test_title_and_identifiers() -> None:
    assert title_word("The Interpretability of Retrieval", 1) == "Interpretability"
    assert normalize_title("  A  Test: Title! ") == "a test title"
    assert (
        normalize_identifier(IdentifierScheme.doi, "https://doi.org/10.1145/ABC.123")
        == "10.1145/abc.123"
    )
    assert normalize_identifier(IdentifierScheme.arxiv, "arXiv:2601.01234v2") == "2601.01234"
    assert normalize_identifier(IdentifierScheme.openalex, "https://openalex.org/W123") == "W123"
