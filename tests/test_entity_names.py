"""The normalization rule and the comparison built on it, with no database in the way.

These are the cases the old substring predicate could not answer and that a firm's
estate is full of: the same company written with and without its legal form, with a
seat appended, with an umlaut spelled two ways, and two companies that merely share a
word.
"""

from __future__ import annotations

from knowledge_index.entity_names import name_similarity, name_tokens, normalize_entity_name


def _score(left: str, right: str) -> tuple[float, str]:
    return name_similarity(normalize_entity_name(left), normalize_entity_name(right))


def test_legal_form_is_not_part_of_the_name() -> None:
    assert normalize_entity_name("Nexford Industrial Holdings Inc.") == "nexford industrial holdings"
    assert normalize_entity_name("Nordwind Energie GmbH") == "nordwind energie"
    assert normalize_entity_name("Whitfield & Crane LLP") == "whitfield crane"
    # ... but "Holdings" and "Group" are identity, not form.
    assert "holdings" in name_tokens(normalize_entity_name("Nexford Holdings"))


def test_both_german_spellings_reach_one_form() -> None:
    assert normalize_entity_name("Müller Verwaltungs AG") == normalize_entity_name(
        "Mueller Verwaltungs AG"
    )
    assert normalize_entity_name("Groß & Partner") == normalize_entity_name("Gross and Partner")


def test_the_two_directions_the_substring_predicate_missed() -> None:
    # "%query%" against the stored name could only ever match one of these.
    short_first, component = _score("Verimark Group", "Verimark Hospitality Group Inc.")
    long_first, _ = _score("Verimark Hospitality Group Inc.", "Verimark Group")
    assert short_first == long_first
    assert component == "token_containment"
    assert short_first >= 0.80

    score, component = _score("Nexford", "Nexford Industrial Holdings Inc.")
    assert component == "token_containment"
    assert score >= 0.80


def test_one_shared_token_scores_below_three() -> None:
    one, _ = _score("Meridian", "Meridian Health Partners Inc.")
    three, _ = _score("Meridian Health Partners", "Meridian Health Partners Inc.")
    assert one < three


def test_a_shared_word_is_not_a_match() -> None:
    score, _ = _score("Meridian Capital Partners LP", "Meridian Health Partners Inc.")
    assert score < 0.80


def test_a_typo_is_recognized_without_a_shared_token() -> None:
    score, component = _score("Verimarc Hospitality Group", "Verimark Hospitality Group Inc.")
    assert component in ("characters", "token_overlap")
    assert score >= 0.80


def test_unrelated_names_score_nothing() -> None:
    assert _score("Nordwind Energie", "Kensington Bank") == (0.0, "")


def test_a_name_that_is_only_a_legal_form_keeps_it() -> None:
    """Stripping a name down to nothing would key every such mention identically, and
    under the uniqueness constraint that merges unrelated companies rather than merely
    failing to tell them apart."""
    assert normalize_entity_name("GmbH & Co. KG") == "gmbh and co kg"
    assert normalize_entity_name("The Company") == "the company"
    assert normalize_entity_name("GmbH & Co. KG") != normalize_entity_name("The Company")
    # Only when there is nothing else. A real name still sheds its form.
    assert normalize_entity_name("Nordwind Energie GmbH & Co. KG") == "nordwind energie"


def test_nothing_in_makes_nothing_out() -> None:
    assert normalize_entity_name("") == ""
    assert normalize_entity_name(None) == ""
    assert normalize_entity_name("   ") == ""
