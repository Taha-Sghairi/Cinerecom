"""recommandation.py - Similarity packs and recommendations (rework).

Two parts, no machine learning (only numpy and Pandas):

1. SIMILARITY PACKS (built ONCE, stored forever). For every title, TWO packs of the
   PACK_SIZE most similar titles: one of movies and one of TV shows. They are written to
   data/similarity_packs.csv by
       python recommandation.py
   and only READ afterwards: nothing is recomputed when the application runs.
   The similarity of two titles is a weighted sum of SIX field similarities
   (SIMILARITY_WEIGHTS): synopsis, title, keywords, genres, people (cast and directors)
   and production (studios and countries). Each field is compared with TF-IDF vectors
   and the cosine similarity. Words of the synopsis, of the title and of the keywords
   are first replaced by their THEME when they are listed in data/themes.csv
   ("kingdom" and "empire" become the same token). A field that one of the two titles
   does not have (for example no director) is left out of that comparison
   (MISSING_FIELD_PENALTY says how much it still counts against the pair).

2. THE RECOMMENDATION (build_recommendation_pool). EVERY title of the user's history
   (ratings, favourites, finished titles, watchlist) VOTES for the titles of its pack
   of the chosen type: vote = history weight x similarity score. A low rating has a
   NEGATIVE weight and votes against. The candidates are ranked by their total votes
   (plus a small catalogue-rating bonus) and the best POOL_SIZE form the pool.
   The pool depends only on the history, so the application keeps it until the history
   changes (build_history_signature) and shows it RECOMMENDATIONS_PER_PAGE at a time.
   When the pool has been shown completely, or when the user comes back to the page,
   arrange_pool_for_display shuffles it a little (inside blocks of ten) so that the
   same titles are shown again in a slightly different order.

This file has no Streamlit code and no database access: app.py reads the user's
data from SQLite by user id and passes it in, so the rules are easy to test.
Command line:
    python recommandation.py                    build and store the packs
    python recommandation.py explain "Title"    top 10 of a title with the score of each field
"""

import hashlib
import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import catalogue

# ==========================================================================
# TUNABLE VALUES: every number below is a first guess, meant to be adjusted
# ==========================================================================

# --- Similarity of two titles -------------------------------------------------
# Weight of each field in the similarity of two titles (they add up to 1).
# The fields do NOT have the same scale: the cosine of two identical genre sets is 1.00, while
# the best synopsis matches are around 0.1 to 0.2 and the best keyword matches around 0.3. The
# same weight therefore gives genres several times more influence than the synopsis, which is
# why the genre weight is small (a first test with genres at 0.15 ranked unrelated titles that
# only shared the genres above titles that shared keywords).
# Tuned on match_test.txt (74 seeds, 338 expected matches): the score rewards a match for being
# HIGH in the ranking, not only for being in the pack. Gains were small (see the tuning report).
SIMILARITY_WEIGHTS = {
    "synopsis": 0.25,
    "title": 0.05,
    "keywords": 0.29,
    "genres": 0.09,
    "people": 0.15,
    "production": 0.09,
    "themes": 0.08,       # only used when THEME_MODE is "field"
}
# How data/themes.csv is used (when the file exists). The choice is meant to be settled by testing:
#   "field"   = the themes found in the synopsis and in the keywords form a SEVENTH field ("themes")
#               of the comparison; the words themselves are left untouched;
#   "replace" = in the synopsis, the title and the keywords, every word or phrase of a theme is
#               replaced by the theme (two different words of the same theme become the same token);
#   "add"     = like "replace", but the words are kept next to the theme;
#   "off"     = the themes are ignored.
THEME_MODE = "field"
# 0 = a field that one of the two titles lacks is simply left out of the comparison.
# 1 = it counts as a similarity of zero. In between = a blend of both.
MISSING_FIELD_PENALTY = 0.0
# Per field: (minimum number of titles that must contain a word, maximum share of the
# titles that may contain it). A word in a single title cannot link two titles; a word
# in too many titles says nothing. Genres, people and production are not limited
# above: their values are whole names, and the idf already lowers common ones.
FIELD_SETTINGS = {
    "synopsis": (2, 0.15),
    "title": (2, 0.10),
    "keywords": (2, 0.15),
    "genres": (1, 1.0),
    "people": (2, 1.0),
    "production": (2, 1.0),
    "themes": (2, 0.30),
}
PACK_SIZE = 20                       # titles in each pack (one pack per type)

# --- Recommendation from the history -------------------------------------------
# Weight of what the user did with a title. Weights of the same title are added up.
RATING_WEIGHTS = {5: 2.0, 4: 1.0, 3: 0.0, 2: -1.0, 1: -2.0}
FAVORITE_WEIGHT = 2.0
FINISHED_WEIGHT = 0.5
WATCHLIST_WEIGHT = 0.5
EXCLUDE_WATCHLIST_TITLES = True      # a title already in the watchlist is not recommended
CATALOGUE_RATING_WEIGHT = 0.10       # bonus (quality score / 10 x this) added to votes scaled 0 to 1
POOL_SIZE = 50                       # titles kept for one history and one type
RECOMMENDATIONS_PER_PAGE = 5         # titles shown at once
SHUFFLE_BLOCK_SIZE = 10              # a replay only shuffles inside blocks of this size
REASON_COUNT = 2                     # history titles named as the reason of a recommendation

# Words that carry no meaning for the comparison of two texts.
STOP_WORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had
has have having he her here hers herself him himself his how i if in into is it its itself just
me more most my myself no nor not now of off on once only or other our ours ourselves out over own
same she should so some such than that the their theirs them themselves then there these they this
those through to too under until up very was we were what when where which while who whom why will
with would you your yours yourself yourselves one two three new back get gets got make makes made
""".split())

MODE_PERSONALISED = "personalised"   # every title of the pool comes from the votes of the history
MODE_MIXED = "mixed"                 # best-rated titles complete a pool that was too small
MODE_TOP_RATED = "top_rated"         # nothing to vote with: the best-rated titles of the catalogue


# ==========================================================================
# Part 1: similarity packs
# ==========================================================================

# --------------------------------------------------------------------------
# Cutting the fields into tokens
# --------------------------------------------------------------------------

def tokenize_text(text, theme_dictionary: catalogue.ThemeDictionary | None = None,
                  keep_words: bool = False) -> list[str]:
    """Tokens of a free text: its themes, then its meaningful words.

    The text is read from left to right. At each position the LONGEST term of the
    themes dictionary that starts there (up to catalogue.ThemeDictionary.longest_term_length
    words) is replaced by its theme token; any other word is kept as it is, unless
    it is a stop word or a one-letter word. A term that belongs to several themes gives one
    token for each of them. With keep_words=True the words of a theme term are kept as well.
    """
    words = catalogue.split_into_words(text)
    tokens: list[str] = []
    position = 0
    while position < len(words):
        theme_tokens = None
        matched_length = 0
        if theme_dictionary is not None:
            longest_possible = min(theme_dictionary.longest_term_length, len(words) - position)
            for length in range(longest_possible, 0, -1):
                theme_tokens = theme_dictionary.term_to_themes.get(tuple(words[position:position + length]))
                if theme_tokens is not None:
                    matched_length = length
                    break
        if theme_tokens is not None:
            tokens.extend(theme_tokens)
            if keep_words:
                tokens.extend(word for word in words[position:position + matched_length]
                              if len(word) > 1 and word not in STOP_WORDS)
            position += matched_length
            continue
        word = words[position]
        position += 1
        if len(word) > 1 and word not in STOP_WORDS:
            tokens.append(word)
    return tokens


def tokenize_keywords(keywords_text, theme_dictionary: catalogue.ThemeDictionary | None = None,
                      keep_words: bool = False) -> list[str]:
    """Tokens of the "mots_cles" column ("time travel, grandfather, alcoholism").

    Every keyword is cut on its own, so a theme can never be found across two keywords.
    """
    if not isinstance(keywords_text, str):
        return []
    tokens: list[str] = []
    for keyword in keywords_text.split(","):
        tokens.extend(tokenize_text(keyword, theme_dictionary, keep_words))
    return tokens


def extract_theme_tokens(text, theme_dictionary: catalogue.ThemeDictionary) -> list[str]:
    """Only the themes found in a text (no word): ["theme:2", "theme:105", ...]."""
    return [token for token in tokenize_text(text, theme_dictionary)
            if token.startswith(catalogue.THEME_TOKEN_PREFIX)]


def normalize_name(text) -> str:
    """A name (genre, actor, studio, country) in lower case, without accents or punctuation."""
    lowered_text = catalogue.normalize_for_search(text)
    letters_and_spaces = "".join(character if character.isalnum() else " " for character in lowered_text)
    return " ".join(letters_and_spaces.split())


def split_names(names_text) -> list[str]:
    """"Bryan Cranston, Aaron Paul" gives ["bryan cranston", "aaron paul"] (no duplicates)."""
    if not isinstance(names_text, str):
        return []
    normalized_names = (normalize_name(name) for name in names_text.split(","))
    return list(dict.fromkeys(name for name in normalized_names if name))


def build_field_token_lists(catalogue_table: pd.DataFrame,
                            theme_dictionary: catalogue.ThemeDictionary | None = None,
                            theme_mode: str | None = None) -> dict[str, list[list[str]]]:
    """For each field of SIMILARITY_WEIGHTS: the list of tokens of every title (catalogue order).

    The "themes" field exists only in THEME_MODE "field" (see the top of this file).
    """
    theme_mode = THEME_MODE if theme_mode is None else theme_mode
    if theme_dictionary is None or theme_mode == "off":
        theme_mode = "off"
    # In "replace" and "add" mode the themes go INTO the three text fields.
    themes_in_text = theme_dictionary if theme_mode in ("replace", "add") else None
    keep_words = theme_mode == "add"
    token_lists = {
        "synopsis": [tokenize_text(text, themes_in_text, keep_words) for text in catalogue_table["synopsis"]],
        "title": [tokenize_text(text, themes_in_text, keep_words) for text in catalogue_table["titre"]],
        "keywords": [tokenize_keywords(text, themes_in_text, keep_words)
                     for text in catalogue_table["mots_cles"]],
        "genres": [[normalize_name(genre) for genre in catalogue.split_genres(text)]
                   for text in catalogue_table["genres"]],
        # Actors and directors are compared together: an actor who also directs still matches.
        "people": [list(dict.fromkeys(split_names(cast) + split_names(directors)))
                   for cast, directors in zip(catalogue_table["casting_principal"],
                                              catalogue_table["realisateur"])],
        # Studios and countries are compared together (the idf makes "united states" count little).
        "production": [list(dict.fromkeys(split_names(studios) + split_names(countries)))
                       for studios, countries in zip(catalogue_table["studio_production"],
                                                     catalogue_table["pays_origine"])],
    }
    if theme_mode == "field":
        token_lists["themes"] = [extract_theme_tokens(synopsis, theme_dictionary)
                                 + extract_theme_tokens(keywords, theme_dictionary)
                                 for synopsis, keywords in zip(catalogue_table["synopsis"],
                                                               catalogue_table["mots_cles"])]
    return token_lists


# --------------------------------------------------------------------------
# TF-IDF vectors of one field
# --------------------------------------------------------------------------

def build_tfidf_vectors(token_lists: list[list[str]], minimum_document_frequency: int,
                        maximum_document_frequency: float) -> list[dict[int, float]]:
    """One TF-IDF vector per title, as {word number: weight}, each of length 1 (L2-normalised).

    - tf  = 1 + ln(number of times the word is in the title's field) (sub-linear);
    - idf = ln((1 + N) / (1 + df)) + 1, N = number of titles, df = titles that contain the word;
    - words in fewer than `minimum_document_frequency` titles or in more than
      `maximum_document_frequency` x N titles are ignored.
    With vectors of length 1, the cosine similarity of two titles is their dot product.
    A title without any usable token gets an empty vector.
    """
    title_count = len(token_lists)
    word_counts = [Counter(tokens) for tokens in token_lists]
    document_frequency: Counter = Counter()
    for counts in word_counts:
        document_frequency.update(counts.keys())
    kept_words = sorted(
        word for word, frequency in document_frequency.items()
        if frequency >= minimum_document_frequency
        and frequency <= maximum_document_frequency * title_count)
    word_number = {word: number for number, word in enumerate(kept_words)}
    inverse_document_frequency = {
        word: math.log((1 + title_count) / (1 + document_frequency[word])) + 1
        for word in kept_words}

    vectors = []
    for counts in word_counts:
        weights = {word_number[word]: (1 + math.log(count)) * inverse_document_frequency[word]
                   for word, count in counts.items() if word in word_number}
        length = math.sqrt(sum(weight * weight for weight in weights.values()))
        vectors.append({number: weight / length for number, weight in weights.items()}
                       if length > 0 else {})
    return vectors


@dataclass
class FieldIndex:
    """One field of all the titles, ready to compare ONE title with every other in a few milliseconds."""
    name: str
    vectors: list[dict[int, float]]                          # one vector per title (row of the catalogue)
    # word number -> (rows of the titles that contain it, their weights): the "postings"
    postings: dict[int, tuple[np.ndarray, np.ndarray]]


@dataclass
class FieldData:
    """Every field index of the catalogue (built once, then reused for any number of weights)."""
    indexes: list[FieldIndex]
    # presence[row, field] = 1 if the title has at least one usable token in that field
    presence: np.ndarray
    title_ids: np.ndarray                                    # id of every row
    title_types: np.ndarray                                  # type of every row ("movie" / "tv show")


def build_field_index(field_name: str, token_lists: list[list[str]]) -> FieldIndex:
    """TF-IDF vectors of one field + the postings list of every word."""
    minimum_frequency, maximum_frequency = FIELD_SETTINGS[field_name]
    vectors = build_tfidf_vectors(token_lists, minimum_frequency, maximum_frequency)
    rows_by_word: dict[int, list[int]] = defaultdict(list)
    weights_by_word: dict[int, list[float]] = defaultdict(list)
    for row_number, vector in enumerate(vectors):
        for word, weight in vector.items():
            rows_by_word[word].append(row_number)
            weights_by_word[word].append(weight)
    postings = {word: (np.array(rows, dtype=np.int32),
                       np.array(weights_by_word[word], dtype=np.float32))
                for word, rows in rows_by_word.items()}
    return FieldIndex(field_name, vectors, postings)


def build_field_data(catalogue_table: pd.DataFrame,
                     theme_dictionary: catalogue.ThemeDictionary | None = None,
                     theme_mode: str | None = None) -> FieldData:
    """Cut every field of every title into tokens and index them (a few seconds for the catalogue)."""
    token_lists_by_field = build_field_token_lists(catalogue_table, theme_dictionary, theme_mode)
    indexes = [build_field_index(field_name, token_lists_by_field[field_name])
               for field_name in SIMILARITY_WEIGHTS if field_name in token_lists_by_field]
    presence = np.array([[1.0 if vector else 0.0 for vector in index.vectors] for index in indexes],
                        dtype=np.float32).T
    return FieldData(
        indexes=indexes,
        presence=presence,
        title_ids=np.array([int(title_id) for title_id in catalogue_table["id"]]),
        title_types=np.array([str(title_type).strip().lower() for title_type in catalogue_table["type"]]),
    )


# --------------------------------------------------------------------------
# Similarity of ONE title with all the others
# --------------------------------------------------------------------------

def calculate_field_similarity_rows(field_data: FieldData, row_number: int) -> list[np.ndarray]:
    """Cosine similarity of one title with every title, field by field (same order as field_data.indexes).

    This part does NOT depend on the weights: it can be computed once and combined
    with many different weights (combine_field_rows), which makes weight tuning fast.
    """
    title_count = len(field_data.title_ids)
    field_rows = []
    for index in field_data.indexes:
        similarity_row = np.zeros(title_count, dtype=np.float32)
        for word, weight in index.vectors[row_number].items():
            rows_with_word, weights_of_word = index.postings[word]
            similarity_row[rows_with_word] += weight * weights_of_word
        field_rows.append(similarity_row)
    return field_rows


def combine_field_rows(field_data: FieldData, field_rows: list[np.ndarray], row_number: int,
                       weights: dict[str, float] | None = None,
                       missing_field_penalty: float | None = None) -> np.ndarray:
    """Similarity (0 to 1) of one title with every title: the weighted sum of its field similarities.

    The sum is divided by the weight of the fields that BOTH titles have, plus
    `missing_field_penalty` x the weight of the fields that one of them lacks. So a
    pair is not punished for a field that a title simply does not have (penalty 0).
    The title is not excluded from its own row: the caller sets that entry to 0.
    """
    weights = SIMILARITY_WEIGHTS if weights is None else weights
    missing_field_penalty = MISSING_FIELD_PENALTY if missing_field_penalty is None else missing_field_penalty
    weight_vector = np.array([weights.get(index.name, 0.0) for index in field_data.indexes],
                             dtype=np.float32)
    weighted_sum = np.zeros(len(field_data.title_ids), dtype=np.float32)
    for field_weight, similarity_row in zip(weight_vector, field_rows):
        if field_weight:
            weighted_sum += field_weight * similarity_row
    shared_weight = field_data.presence @ (weight_vector * field_data.presence[row_number])
    divisor = shared_weight + missing_field_penalty * (weight_vector.sum() - shared_weight)
    return np.divide(weighted_sum, divisor, out=np.zeros_like(weighted_sum), where=divisor > 0)


def build_pack_of_type(scores: np.ndarray, candidate_rows: np.ndarray, field_data: FieldData,
                       pack_size: int) -> list[tuple[int, float]]:
    """The `pack_size` best titles among `candidate_rows`: [(title_id, score), ...], best first.

    A title with a score of 0 (nothing in common) is never put in a pack. Equal scores are
    ordered by id, so the result never depends on chance.
    """
    candidate_scores = scores[candidate_rows]
    candidate_ids = field_data.title_ids[candidate_rows]
    best_positions = np.lexsort((candidate_ids, -candidate_scores))[:pack_size]
    return [(int(candidate_ids[position]), round(float(candidate_scores[position]), 6))
            for position in best_positions if candidate_scores[position] > 0]


def build_similarity_packs(catalogue_table: pd.DataFrame,
                           theme_dictionary: catalogue.ThemeDictionary | None = None,
                           weights: dict[str, float] | None = None,
                           missing_field_penalty: float | None = None,
                           pack_size: int = PACK_SIZE, show_progress: bool = False,
                           theme_mode: str | None = None
                           ) -> dict[int, dict[str, list[tuple[int, float]]]]:
    """The packs of every title: {title_id: {"movie": [(id, score), ...], "tv show": [...]}}, best first.

    A pack never contains the title itself.
    """
    field_data = build_field_data(catalogue_table, theme_dictionary, theme_mode)
    rows_by_type = {title_type: np.flatnonzero(field_data.title_types == title_type)
                    for title_type in (catalogue.TYPE_MOVIE, catalogue.TYPE_TV_SHOW)}
    packs: dict[int, dict[str, list[tuple[int, float]]]] = {}
    for row_number, title_id in enumerate(field_data.title_ids):
        field_rows = calculate_field_similarity_rows(field_data, row_number)
        scores = combine_field_rows(field_data, field_rows, row_number, weights, missing_field_penalty)
        scores[row_number] = 0.0
        packs[int(title_id)] = {title_type: build_pack_of_type(scores, candidate_rows, field_data, pack_size)
                                for title_type, candidate_rows in rows_by_type.items()}
        if show_progress and (row_number + 1) % 1000 == 0:
            print(f"      ... {row_number + 1} / {len(field_data.title_ids)} titles")
    return packs


def write_similarity_packs(packs: dict[int, dict[str, list[tuple[int, float]]]],
                           path: Path = catalogue.SIMILARITY_PACKS_PATH) -> None:
    """Store the packs in a CSV file: one line per (title, type, rank): id, type, rank, similar_id, score."""
    rows = [(title_id, pack_type, rank, similar_id, score)
            for title_id in sorted(packs)
            for pack_type in (catalogue.TYPE_MOVIE, catalogue.TYPE_TV_SHOW)
            for rank, (similar_id, score) in enumerate(packs[title_id].get(pack_type, []), start=1)]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=catalogue.SIMILARITY_PACK_COLUMNS).to_csv(path, index=False)


def load_theme_dictionary_or_none() -> catalogue.ThemeDictionary | None:
    """The themes of data/themes.csv, or None (with a warning) if the file does not exist yet."""
    if not catalogue.THEMES_CSV_PATH.is_file():
        print(f"[WARNING] {catalogue.THEMES_CSV_PATH} not found: continuing WITHOUT themes.")
        return None
    theme_dictionary = catalogue.read_theme_dictionary()
    print(f"[OK] Themes loaded: {theme_dictionary.theme_count} themes, "
          f"{theme_dictionary.term_count} terms ({theme_dictionary.shared_term_count} shared by several themes)")
    return theme_dictionary


def build_and_write_similarity_packs(path: Path = catalogue.SIMILARITY_PACKS_PATH) -> dict:
    """Build the packs of the real catalogue and store them (run once: python recommandation.py)."""
    catalogue_table = catalogue.read_catalogue_csv()
    theme_dictionary = load_theme_dictionary_or_none()
    packs = build_similarity_packs(catalogue_table, theme_dictionary, show_progress=True)
    write_similarity_packs(packs, path)
    return packs


# ==========================================================================
# Part 2: the recommendation
# ==========================================================================

@dataclass
class ScoredCandidate:
    """A title voted for by the history, with its points (kept to explain and to test the scoring)."""
    title_id: int
    vote_total: float             # sum of (history weight x similarity score) over the history titles
    relative_votes: float         # vote_total / the biggest vote_total (0 to 1)
    catalogue_rating: float       # quality score, 0 to 10 (rating corrected by the number of votes)
    points: float                 # relative_votes + the catalogue rating bonus
    # (history title id, contribution) of the history titles that voted FOR it, best first
    supporters: list[tuple[int, float]] = field(default_factory=list)


@dataclass
class RecommendationPool:
    """What build_recommendation_pool returns."""
    title_ids: list[int]                                   # the pool, best first
    mode: str                                              # MODE_PERSONALISED / MODE_MIXED / MODE_TOP_RATED
    filler_count: int = 0                                  # best-rated titles added at the end of the pool
    candidates: list[ScoredCandidate] = field(default_factory=list)   # voted titles, best first
    # title id -> the (at most REASON_COUNT) history titles that made it a candidate
    reasons: dict[int, list[int]] = field(default_factory=dict)


def calculate_history_weights(favorite_ids: list[int], user_ratings: list[tuple[int, int]],
                              watchlist_ids: list[int], finished_ids: list[int]) -> dict[int, float]:
    """{title_id: weight} of everything the user did. Weights of the same title are added up.

    A positive weight means "more like this", a negative one "less like this". A title
    whose weights cancel out (or a rating of 3) has a weight of 0 and does not vote.
    """
    history_weights: dict[int, float] = defaultdict(float)
    for title_id, rating in user_ratings:
        history_weights[title_id] += RATING_WEIGHTS.get(rating, 0.0)
    for title_id in set(favorite_ids):
        history_weights[title_id] += FAVORITE_WEIGHT
    for title_id in set(finished_ids):
        history_weights[title_id] += FINISHED_WEIGHT
    for title_id in set(watchlist_ids):
        history_weights[title_id] += WATCHLIST_WEIGHT
    return dict(history_weights)


def build_history_signature(title_type: str, favorite_ids: list[int],
                            user_ratings: list[tuple[int, int]], watchlist_ids: list[int],
                            finished_ids: list[int]) -> str:
    """A short text that is the same as long as the history and the type are the same.

    The application keeps the pool while the signature is unchanged and rebuilds it when it
    changes. The order of the lists does not matter.
    """
    canonical_text = "|".join([
        title_type,
        ",".join(str(title_id) for title_id in sorted(set(favorite_ids))),
        ",".join(f"{title_id}:{rating}" for title_id, rating in sorted(user_ratings)),
        ",".join(str(title_id) for title_id in sorted(set(watchlist_ids))),
        ",".join(str(title_id) for title_id in sorted(set(finished_ids))),
    ])
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()[:16]


def build_recommendation_pool(catalogue_table: pd.DataFrame, packs: dict, title_type: str,
                              favorite_ids: list[int], user_ratings: list[tuple[int, int]],
                              watchlist_ids: list[int], finished_ids: list[int],
                              pool_size: int = POOL_SIZE) -> RecommendationPool:
    """The best `pool_size` titles of `title_type` ("movie" or "tv show") for ONE user.

    packs: {title_id: {type: [(similar_id, score), ...]}} (catalogue.load_similarity_packs)
    favorite_ids / user_ratings [(id, 1-5)] / watchlist_ids / finished_ids: the user's data (SQLite, by user id)

    1. every title of the history with a non-zero weight votes for the titles of ITS pack of the chosen type;
    2. titles the user already rated, favourited, finished (or put in the watchlist) are never candidates;
    3. a title is kept if its votes are positive, and ranked by votes (scaled so that the best has 1)
       plus a small bonus for its catalogue rating; equal points are ordered by id (no randomness);
    4. if fewer than `pool_size` titles remain, the best-rated titles of the type complete the pool.
    """
    type_table = catalogue.filter_catalogue(catalogue_table, title_type=title_type)
    type_ids = set(int(title_id) for title_id in type_table["id"])
    # The quality score is the catalogue rating corrected by the number of votes.
    rating_column = catalogue.QUALITY_COLUMN if catalogue.QUALITY_COLUMN in type_table.columns else "note"
    catalogue_rating_by_id = {int(title_id): float(rating)
                              for title_id, rating in zip(type_table["id"], type_table[rating_column])}

    known_ids = ({title_id for title_id, _ in user_ratings} | set(favorite_ids) | set(finished_ids))
    if EXCLUDE_WATCHLIST_TITLES:
        known_ids |= set(watchlist_ids)

    history_weights = calculate_history_weights(favorite_ids, user_ratings, watchlist_ids, finished_ids)
    vote_totals: dict[int, float] = defaultdict(float)
    contributions: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for history_title_id, history_weight in history_weights.items():
        if history_weight == 0:
            continue
        for similar_id, similarity_score in packs.get(history_title_id, {}).get(title_type, []):
            if similar_id not in type_ids or similar_id in known_ids:
                continue
            contribution = history_weight * similarity_score
            vote_totals[similar_id] += contribution
            if contribution > 0:
                contributions[similar_id].append((contribution, history_title_id))

    voted_ids = [title_id for title_id, total in vote_totals.items() if total > 0]
    biggest_vote_total = max((vote_totals[title_id] for title_id in voted_ids), default=0.0)
    candidates = []
    for title_id in voted_ids:
        relative_votes = vote_totals[title_id] / biggest_vote_total
        catalogue_rating = catalogue_rating_by_id[title_id]
        best_supporters = sorted(contributions[title_id], reverse=True)
        candidates.append(ScoredCandidate(
            title_id=title_id, vote_total=vote_totals[title_id], relative_votes=relative_votes,
            catalogue_rating=catalogue_rating,
            points=relative_votes + CATALOGUE_RATING_WEIGHT * catalogue_rating / 10,
            supporters=[(supporter_id, contribution) for contribution, supporter_id in best_supporters]))
    candidates.sort(key=lambda candidate: (-candidate.points, candidate.title_id))

    pool_ids = [candidate.title_id for candidate in candidates[:pool_size]]
    filler_ids: list[int] = []
    if len(pool_ids) < pool_size:
        taken_ids = set(pool_ids) | known_ids
        best_rated_ids = (int(title_id) for title_id in catalogue.sort_by_external_rating(type_table)["id"])
        filler_ids = [title_id for title_id in best_rated_ids
                      if title_id not in taken_ids][:pool_size - len(pool_ids)]

    if not pool_ids:
        mode = MODE_TOP_RATED
    elif filler_ids:
        mode = MODE_MIXED
    else:
        mode = MODE_PERSONALISED
    return RecommendationPool(
        title_ids=pool_ids + filler_ids, mode=mode, filler_count=len(filler_ids),
        candidates=candidates,
        reasons={candidate.title_id: [supporter_id for supporter_id, _ in
                                      candidate.supporters[:REASON_COUNT]]
                 for candidate in candidates[:pool_size]})


def arrange_pool_for_display(pool_title_ids: list[int], signature: str, pass_number: int,
                             block_size: int = SHUFFLE_BLOCK_SIZE) -> list[int]:
    """The order in which the pool is shown during one pass.

    Pass 0 is the pool exactly as ranked. A later pass (the pool was shown completely, or the user
    came back to the page without changing anything) shuffles it a little: only INSIDE blocks of
    `block_size` titles, so the best titles stay near the beginning. The shuffle depends only on
    (signature, pass_number): it is repeatable and needs no storage.
    """
    arranged_ids = list(pool_title_ids)
    if pass_number <= 0:
        return arranged_ids
    for block_start in range(0, len(arranged_ids), block_size):
        block = arranged_ids[block_start:block_start + block_size]
        random.Random(f"{signature}|{pass_number}|{block_start}").shuffle(block)
        arranged_ids[block_start:block_start + block_size] = block
    return arranged_ids


def get_recommendation_page(arranged_ids: list[int], page_index: int,
                            page_size: int = RECOMMENDATIONS_PER_PAGE) -> list[int]:
    """The ids shown on one page of the arranged pool (page_index starts at 0; empty if past the end)."""
    first_position = max(0, page_index) * page_size
    return arranged_ids[first_position:first_position + page_size]


# ==========================================================================
# Command line
# ==========================================================================

def explain_title(title_text: str) -> None:
    """Print the 10 most similar titles of each type with the cosine of every field.

    Used to see WHY two titles are linked when adjusting SIMILARITY_WEIGHTS.
    """
    catalogue_table = catalogue.read_catalogue_csv()
    theme_dictionary = load_theme_dictionary_or_none()
    field_data = build_field_data(catalogue_table, theme_dictionary)

    wanted_text = catalogue.keep_letters_and_digits(catalogue.normalize_for_search(title_text))
    compact_titles = [catalogue.keep_letters_and_digits(catalogue.normalize_for_search(title))
                      for title in catalogue_table["titre"]]
    matching_rows = ([row for row, title in enumerate(compact_titles) if title == wanted_text]
                     or [row for row, title in enumerate(compact_titles) if wanted_text in title])
    if not matching_rows:
        print(f"[ERROR] No title matches '{title_text}'.")
        return
    row_number = matching_rows[0]
    chosen_title = catalogue_table.iloc[row_number]
    print(f"Title: {chosen_title['titre']} ({chosen_title['annee']}), id {chosen_title['id']}, "
          f"{len(matching_rows)} match(es), the first one is used")
    print(f"Weights: {SIMILARITY_WEIGHTS}   theme mode: {THEME_MODE}   "
          f"missing-field penalty: {MISSING_FIELD_PENALTY}")

    field_rows = calculate_field_similarity_rows(field_data, row_number)
    scores = combine_field_rows(field_data, field_rows, row_number)
    scores[row_number] = 0.0
    field_names = [index.name for index in field_data.indexes]
    for title_type in (catalogue.TYPE_MOVIE, catalogue.TYPE_TV_SHOW):
        candidate_rows = np.flatnonzero(field_data.title_types == title_type)
        print(f"\nTop 10 {title_type} (score | cosine of each field: "
              f"{' / '.join(field_names)})")
        for rank, (similar_id, score) in enumerate(
                build_pack_of_type(scores, candidate_rows, field_data, 10), start=1):
            similar_row = int(np.flatnonzero(field_data.title_ids == similar_id)[0])
            similar_title = catalogue_table.iloc[similar_row]
            cosines = " / ".join(f"{field_row[similar_row]:.2f}" for field_row in field_rows)
            print(f"  {rank:2d}. {score:.3f} | {cosines} | {similar_title['titre']} "
                  f"({similar_title['annee']}, id {similar_id})")


def main(arguments: list[str]) -> int:
    """python recommandation.py [explain "Title"]"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # titles with unusual letters must not crash the console
    try:
        if arguments and arguments[0] == "explain":
            if len(arguments) < 2:
                print('Usage: python recommandation.py explain "Title"')
                return 1
            explain_title(" ".join(arguments[1:]))
            return 0
        built_packs = build_and_write_similarity_packs()
    except catalogue.CatalogueError as error:
        print(f"[ERROR] {error}")
        return 1
    entry_counts = [len(pack) for title_packs in built_packs.values() for pack in title_packs.values()]
    print(f"[OK] {len(built_packs)} titles, 2 packs each, written to {catalogue.SIMILARITY_PACKS_PATH}")
    print(f"[OK] pack length: min {min(entry_counts)}, max {max(entry_counts)}, "
          f"empty packs: {entry_counts.count(0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
