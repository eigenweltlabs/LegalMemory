"""One normalization rule for legal-entity names, and the comparison built on it.

Dependency-light on purpose: the persistence layer writes ``normalized_name`` with
it, the insertion-time resolver searches on it, and the uniqueness constraint is
defined over the column it produces. If the rule lived in two places — say Python
for the search and plpgsql for a generated column — the two would drift and the
constraint would stop meaning what the resolver believes it means.

Why normalization alone is not the answer, and what is. On the 9,288-document run
that motivated this module, 985 distinct client names normalized down to 942: the
duplicate rows were not spelling variants of each other, they were the SAME name
searched with a substring predicate that could not match it ("Nexford" against a
stored "Nexford Industrial Holdings Inc."). So the comparison here is token-set
based rather than string-containment based, and normalization only removes the
noise that is genuinely noise — case, punctuation, diacritics, the legal form.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Legal-form tokens. Dropped wherever they appear, not only at the end: a name is
# routinely written "Nexford Industrial Holdings Inc." in one document and
# "Nexford Industrial Holdings" in the next, and German filings put the form in
# the middle ("Nexford GmbH & Co. KG Beteiligungen").
#
# "Holdings", "Group" and "Partners" are deliberately NOT here: they are part of a
# company's identity, and dropping them would merge "Verimark Group" into
# "Verimark Hospitality" rather than merely letting the two find each other.
# Two- and three-letter forms that are also ordinary words ("as", "ad", "lc") are
# left out on purpose: dropping them costs more than it saves when they are the
# only distinctive token a name has.
LEGAL_FORM_TOKENS = frozenset(
    """
    ag gmbh mbh ug kg kgaa ohg gbr eg ev se sa sas sarl sca scs spa srl sl slu
    nv bv cv ab asa aps oy oyj plc llp lllp llc lp pc pllc ltd limited inc
    incorporated corp corporation co company pte pty zoo doo jsc pjsc ojsc oao zao
    """.split()
)

# Words that carry no identity of their own once the legal form is gone. "&" and
# "and" are the same conjunction written two ways ("Whitfield & Crane" /
# "Whitfield and Crane"), and the articles are noise in every language the corpus
# mixes.
CONNECTOR_TOKENS = frozenset("and und et the der die das le la les el los".split())

# German umlauts and eszett have two accepted spellings and legal documents use
# both. Folding to the diacritic-free form alone would map "Müller" to "muller"
# and leave "Mueller" unmatched, so the transliteration runs first.
_TRANSLITERATIONS = {
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
    "æ": "ae",
    "ø": "oe",
    "å": "aa",
    "đ": "d",
    "ł": "l",
}

# Removed outright rather than turned into a space, because these join a word
# rather than separate two: "L.P." has to become one "lp" token the legal-form list
# recognizes, not "l" and "p", and "Sainsbury's" must not shed an "s".
_JOINING_PUNCTUATION = re.compile(r"[.'’´`]")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_entity_name(name: str | None) -> str:
    """The comparable form of an entity name: case-folded, transliterated,
    diacritic-folded, punctuation-stripped, legal form and conjunctions removed.

    Token order is preserved so the value stays readable in the database and in a
    tool result; order-insensitivity is the comparison's job, not the string's.

    Stripping only happens when something survives it. A mention the extraction agent
    left as a bare form — "GmbH & Co. KG", "The Company", "S.A." — has every one of
    its tokens on the two lists above, and returning "" for it would key EVERY such
    mention to the same value. Under the uniqueness constraint that is not a missing
    match, it is a silent merge: the second unrelated company written that way becomes
    the first one. The legal form is noise around an identity, not when it is all the
    identity there is.
    """
    text = (name or "").strip().lower()
    if not text:
        return ""
    text = text.replace("&", " and ")
    for source, replacement in _TRANSLITERATIONS.items():
        text = text.replace(source, replacement)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _JOINING_PUNCTUATION.sub("", text)
    text = _PUNCTUATION.sub(" ", text)
    all_tokens = text.split()
    tokens = [
        token
        for token in all_tokens
        if token not in LEGAL_FORM_TOKENS and token not in CONNECTOR_TOKENS
    ]
    return " ".join(tokens or all_tokens)


def name_tokens(normalized: str) -> frozenset[str]:
    """The token set a normalized name is compared as."""
    return frozenset(normalized.split())


def name_similarity(left: str, right: str) -> tuple[float, str]:
    """How alike two ALREADY-NORMALIZED names are, and which rule said so.

    Returns ``(score, component)`` with score in 0..1. The components, strongest
    first:

    ``exact``
        identical normalized names.
    ``token_containment``
        every token of the shorter name appears in the longer one — the case the
        old substring predicate could only match in one direction. "Verimark
        Group" and "Verimark Hospitality Group Inc." reach each other here, and
        so do "Nexford" and "Nexford Industrial Holdings Inc.". A single shared
        token scores lower than three, because a one-word containment ("Meridian"
        inside "Meridian Health Partners") is a much weaker claim.
    ``token_overlap``
        partial overlap, scored by how much of the shorter name is covered and
        how much of the union the two share.
    ``characters``
        no useful token overlap, but the strings themselves are near-identical —
        typos and transcription slips ("Verimarc" for "Verimark").
    """
    if not left or not right:
        return 0.0, ""
    if left == right:
        return 1.0, "exact"
    left_tokens, right_tokens = name_tokens(left), name_tokens(right)
    shorter, longer = (
        (left_tokens, right_tokens)
        if len(left_tokens) <= len(right_tokens)
        else (right_tokens, left_tokens)
    )
    shared = shorter & longer
    union = left_tokens | right_tokens
    coverage = len(shared) / len(shorter) if shorter else 0.0
    jaccard = len(shared) / len(union) if union else 0.0
    if shared and coverage == 1.0:
        # 0.80 at one shared token, 0.90 at three, plus how much of the whole
        # union the two names agree on.
        score = 0.80 + 0.05 * min(2, len(shorter) - 1) + 0.05 * jaccard
        return round(score, 4), "token_containment"
    characters = SequenceMatcher(None, left, right).ratio()
    if shared:
        score = max(0.45 * coverage + 0.35 * jaccard, 0.85 * characters)
        return round(score, 4), "token_overlap" if score > 0.85 * characters else "characters"
    if characters >= 0.85:
        return round(0.85 * characters, 4), "characters"
    return 0.0, ""


def normalize_group(value: str | None) -> str | None:
    """One spelling for a group a firm writes several ways.

    "Healthcare & Life Sciences Practice Group", "Litigation Department" and
    "Banking and Finance" all name a group whose members would otherwise not match
    each other, or a caller's filter. The firm's own wording is kept, minus the
    organisational suffix.
    """
    if not value:
        return None
    group = value.strip().strip(" ,.-")
    # Case-insensitively: a document writes "the Energy Enforcement &
    # Investigations practice group" mid-sentence as readily as it writes
    # "Energy Enforcement & Investigations Practice Group" in a letterhead, and
    # a case-sensitive match kept the first one's trailing noun — leaving a
    # group that matched nothing and no one.
    lowered = group.casefold()
    for suffix in (" practice group", " practice", " group", " department", " team"):
        if lowered.endswith(suffix):
            group = group[: -len(suffix)].strip()
            break
    return group.replace(" and ", " & ").replace(" And ", " & ") or None
