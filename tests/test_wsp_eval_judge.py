"""The evaluation math must be right before any number goes into a release note."""

import math

from scripts.wsp_eval import judge


def test_canonical_url_ignores_presentation_variants_but_keeps_identity():
    key = judge.canonical_url("https://docs.example.com/guide/")
    assert judge.canonical_url("http://www.docs.example.com/guide") == key
    assert judge.canonical_url("https://m.docs.example.com/guide#intro") == key
    assert judge.canonical_url("https://docs.example.com/guide?utm_source=x&srsltid=y") == key
    assert judge.canonical_url("https://docs.example.com/guide/amp/") == key
    # Query parameters that identify a page stay part of the key.
    assert judge.canonical_url("https://youtube.com/watch?v=a") != judge.canonical_url("https://youtube.com/watch?v=b")


def test_ndcg_is_one_for_the_ideal_order_and_penalises_misses():
    judged = {"a": 3, "b": 2, "c": 0}
    assert judge.ndcg_at(["a", "b", "c"], judged) == 1.0
    worse = judge.ndcg_at(["c", "b", "a"], judged)
    assert 0 < worse < 1
    # An unjudged result counts as not relevant.
    assert judge.ndcg_at(["x", "a"], judged) < judge.ndcg_at(["a", "x"], judged)
    expected = (7 / math.log2(2) + 3 / math.log2(3)) / (7 / math.log2(2) + 3 / math.log2(3))
    assert judge.ndcg_at(["a", "b"], judged) == expected


def test_ndcg_without_any_relevant_result_is_zero():
    assert judge.ndcg_at(["a"], {"a": 0, "b": 0}) == 0.0


def test_grade_parser_takes_the_last_complete_json_answer():
    text = 'thinking {"r1": 9}\nfinal: {"r1": 3, "r2": 0}'
    assert judge._parse_grades(text, ["r1", "r2"]) == {"r1": 3, "r2": 0}
    assert judge._parse_grades('{"r1": 2}', ["r1", "r2"]) is None
    assert judge._parse_grades('{"r1": 4, "r2": 1}', ["r1", "r2"]) is None
