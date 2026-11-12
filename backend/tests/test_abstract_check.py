"""Test: services/abstract_check 的归一化与词组重合比例。"""
from backend.services.abstract_check import (
    GRAY_THRESHOLD, LOW_THRESHOLD,
    classify_containment, normalize_for_match, shingle_containment,
)


def _abstract_30w() -> str:
    return " ".join(f"w{i}" for i in range(1, 31))


def test_normalize_joins_linebreak_hyphenation():
    assert normalize_for_match("inter-\nneuronal") == "interneuronal"
    assert normalize_for_match("decision-making") == "decision making"


def test_normalize_strips_markdown_ligatures_superscript():
    s = normalize_for_match("**Bold** text with ﬁ ligature and 10⁻³ scaling")
    assert s == "bold text with fi ligature and 10 3 scaling"
    assert normalize_for_match("") == ""
    assert normalize_for_match("###  ***  ") == ""


def test_containment_full_match_despite_hyphenation_and_markdown():
    words = [f"w{i}" for i in range(1, 31)]
    words[4] = "interneuronal"
    abstract = " ".join(words)
    text_words = list(words)
    text_words[4] = "inter-\nneuronal"
    fulltext = ("# Paper\n\n**Abstract** " + " ".join(text_words)
                + "\n\n## Introduction\nbody follows")
    assert shingle_containment(abstract, fulltext) == 1.0


def test_containment_partial_when_words_rewritten():
    # 改写 3/30 个词，模拟预印本↔正式版措辞差异：每个错词拖垮至多 n 个片段
    words = [f"w{i}" for i in range(1, 31)]
    changed = list(words)
    for i in (4, 14, 24):
        changed[i] = "XX"
    ratio = shingle_containment(" ".join(words), " ".join(changed) + " tail")
    assert 0.3 < ratio < 0.6


def test_containment_zero_for_unrelated_text():
    assert shingle_containment(_abstract_30w(),
                               "completely different content " * 50) == 0.0


def test_containment_none_for_short_abstract():
    short = "too short abstract with only some words here"
    assert shingle_containment(short, "full text " * 50) is None


def test_containment_none_when_fulltext_empty():
    assert shingle_containment(_abstract_30w(), "") is None
    assert shingle_containment(_abstract_30w(), "###  ***  ") is None


def test_classify_buckets_follow_thresholds():
    assert classify_containment(GRAY_THRESHOLD) == "match"
    assert classify_containment((LOW_THRESHOLD + GRAY_THRESHOLD) / 2) == "gray"
    assert classify_containment(max(0.0, LOW_THRESHOLD - 0.1)) == "low"
    assert classify_containment(None) is None
