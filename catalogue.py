"""catalogue.py - Reading the catalogue (data/titres.csv) with Pandas.

Increment 1 (Data Layer): loading, validation and lookup by id.
Increment 3 (Catalogue Browsing): search, type/genre filters, pagination
and poster handling.
Increment 5 (Personal space and profiles): get_titles_by_ids, which turns the
title ids stored in SQLite into catalogue rows.
Increment 6 (Random mode): pick_random_title.
Increment 7 (Mon Bilan): calculate_personal_summary, the statistics of the
personal dashboard, calculated with Pandas from the user's own ratings.

Increment 8 (Functional refinements): multi-genre and year filters, the six
homepage carousels (daily random genres), "Could be related" (read from the stored
similarity packs) and the new summary (based on titles marked Finished).

Recommendation rework: the five optional CSV columns (director, studio, cast,
keywords, country), the themes dictionary (data/themes.csv), similarity packs
stored PER TYPE (one pack of movies and one pack of TV shows for every title),
and "Could be related" = the 6 best titles of both packs mixed.

The CSV file is READ-ONLY: it is never modified, and its content is never
copied into SQLite (SQLite only stores title ids, see database.py).
"""

import datetime
import math
import random
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import streamlit as st

# Absolute path built from this file's location, so the app works
# no matter which folder "streamlit run app.py" is launched from.
CATALOGUE_CSV_PATH = Path(__file__).resolve().parent / "data" / "titres.csv"

# Columns required by the cahier des charges (section 6.4).
EXPECTED_COLUMNS = [
    "id", "titre", "type", "annee", "genres", "synopsis", "affiche", "note",
]

# Extra columns used ONLY to compare titles (similarity packs). A title may have
# any of them empty, and an older CSV without them still works (they become "").
OPTIONAL_TEXT_COLUMNS = [
    "realisateur", "studio_production", "casting_principal", "mots_cles", "pays_origine",
]
# Number of votes behind the rating "note" (TMDB vote_count). Without it a title rated 9.9 by 25
# people would look better than one rated 8.9 by 16,000 people. An older CSV without this column
# still works: the votes become 0 and the ranking falls back to the plain rating.
VOTE_COUNT_COLUMN = "nombre_votes"
QUALITY_COLUMN = "quality_score"      # computed when the CSV is read (not a column of the file)
OPTIONAL_COLUMNS = OPTIONAL_TEXT_COLUMNS + [VOTE_COUNT_COLUMN]
OPTIONAL_COLUMN_TYPES = {**{column_name: "string" for column_name in OPTIONAL_TEXT_COLUMNS},
                         VOTE_COUNT_COLUMN: "int64"}
# Quality score: a title needs about as many votes as the typical title of its type before its
# own rating is trusted (see calculate_quality_scores).
QUALITY_MINIMUM_VOTES_QUANTILE = 0.5

# Types we force on each column, so the data is always read the same way.
COLUMN_TYPES = {
    "id": "int64",
    "titre": "string",
    "type": "string",
    "annee": "int64",
    "genres": "string",
    "synopsis": "string",
    "affiche": "string",
    "note": "float64",
}


class CatalogueError(Exception):
    """Raised when the catalogue CSV is missing, unreadable or invalid.

    The interface will catch it and show the message with st.error()
    instead of crashing the whole application (BNF-F2).
    """


def calculate_quality_scores(catalogue_table: pd.DataFrame) -> pd.Series:
    """Rating (0 to 10) of every title, corrected by its number of votes ("weighted rating").

        quality = v / (v + m) x rating  +  m / (v + m) x mean rating of the type

    v = votes of the title, m = typical number of votes of the titles of its TYPE (the median).
    A title with few votes is pulled towards the mean rating of its type; with many votes
    its own rating stays almost unchanged. It is computed on the WHOLE catalogue when the file
    is read, so a title keeps the same score in every list (carousel, genre, recommendation).
    Movies and TV shows are corrected separately (their votes are very different).
    Without any vote count (older CSV) the quality is the plain rating.
    """
    quality_scores = catalogue_table["note"].astype("float64").copy()
    vote_counts = catalogue_table[VOTE_COUNT_COLUMN].astype("float64")
    title_types = catalogue_table["type"].str.strip().str.lower()
    for raw_type in title_types.unique():
        of_this_type = (title_types == raw_type).to_numpy()
        votes = vote_counts[of_this_type]
        if not (votes > 0).any():
            continue
        ratings = quality_scores[of_this_type]
        minimum_votes = max(float(votes.quantile(QUALITY_MINIMUM_VOTES_QUANTILE)), 1.0)
        quality_scores[of_this_type] = (votes / (votes + minimum_votes) * ratings
                                        + minimum_votes / (votes + minimum_votes) * ratings.mean())
    return quality_scores


def read_catalogue_csv(csv_path: Path = CATALOGUE_CSV_PATH) -> pd.DataFrame:
    """Read and validate the catalogue CSV. No caching here (easy to test).

    Raises CatalogueError with a clear message if anything is wrong.
    """
    csv_path = Path(csv_path)

    if not csv_path.is_file():
        raise CatalogueError(f"Catalogue file not found: {csv_path}")

    try:
        # keep_default_na=False: a title such as "NA" or "None" must stay text
        # and must not silently become a missing value.
        catalogue_table = pd.read_csv(
            csv_path,
            encoding="utf-8",
            dtype={**COLUMN_TYPES, **OPTIONAL_COLUMN_TYPES},
            keep_default_na=False,
        )
    except pd.errors.EmptyDataError:
        raise CatalogueError(f"Catalogue file is empty: {csv_path}")
    except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as error:
        raise CatalogueError(f"Catalogue file could not be read: {error}")
    except OSError as error:
        raise CatalogueError(f"Catalogue file could not be opened: {error}")

    missing_columns = [name for name in EXPECTED_COLUMNS
                       if name not in catalogue_table.columns]
    if missing_columns:
        raise CatalogueError(
            f"Catalogue file is missing columns: {', '.join(missing_columns)}"
        )

    if catalogue_table.empty:
        raise CatalogueError("Catalogue file contains no titles.")

    # SQLite refers to titles by id, so each id must be unique.
    if catalogue_table["id"].duplicated().any():
        raise CatalogueError("Catalogue file contains duplicated ids.")

    # Optional columns missing from an older file are created empty (votes: 0).
    for optional_column in OPTIONAL_TEXT_COLUMNS:
        if optional_column not in catalogue_table.columns:
            catalogue_table[optional_column] = pd.array([""] * len(catalogue_table), dtype="string")
    if VOTE_COUNT_COLUMN not in catalogue_table.columns:
        catalogue_table[VOTE_COUNT_COLUMN] = 0

    # Keep only the known columns, in a fixed order, then add the computed quality score.
    catalogue_table = catalogue_table[EXPECTED_COLUMNS + OPTIONAL_COLUMNS].copy()
    catalogue_table[QUALITY_COLUMN] = calculate_quality_scores(catalogue_table)
    return catalogue_table


@st.cache_data(show_spinner=False)
def load_catalogue() -> pd.DataFrame:
    """Return the catalogue, reading the CSV only once (BNF-P1).

    st.cache_data keeps the result in memory: Streamlit re-runs the script
    after every click, but this function only reads the file the first time.
    """
    return read_catalogue_csv(CATALOGUE_CSV_PATH)


def get_title_by_id(catalogue_table: pd.DataFrame, title_id: int) -> pd.Series | None:
    """Return the catalogue row (as a Series) for a title id, or None if unknown."""
    matching_rows = catalogue_table[catalogue_table["id"] == title_id]
    if matching_rows.empty:
        return None
    return matching_rows.iloc[0]


def get_titles_by_ids(catalogue_table: pd.DataFrame, title_ids: list[int]) -> pd.DataFrame:
    """Return the catalogue rows of these ids, in the SAME ORDER as `title_ids`.

    Used by the personal space and the profiles: SQLite only keeps ids, the
    titles themselves come from the CSV. An id that is not in the catalogue
    (for example after the CSV was replaced) is skipped, so the result can
    have fewer rows than `title_ids`.
    """
    wanted_ids = list(dict.fromkeys(title_ids))   # no duplicates, order kept
    matching_rows = catalogue_table[catalogue_table["id"].isin(wanted_ids)]
    rows_by_id = matching_rows.set_index("id", drop=False)
    found_ids = [title_id for title_id in wanted_ids if title_id in rows_by_id.index]
    return rows_by_id.loc[found_ids]


# --------------------------------------------------------------------------
# Personal summary "Mon Bilan" (Increment 7, reworked in Increment 8)
# --------------------------------------------------------------------------

@dataclass
class PersonalSummary:
    """The statistics of the summary (nothing here is stored anywhere)."""
    titles_watched: int                   # titles explicitly marked Finished
    # (genre, number of finished titles with it), most frequent first, ties A to Z.
    genre_counts: list[tuple[str, int]] = field(default_factory=list)
    top_movies: list[tuple[int, int]] = field(default_factory=list)     # (id, user rating)
    top_tv_shows: list[tuple[int, int]] = field(default_factory=list)   # (id, user rating)
    titles_missing_from_catalogue: int = 0   # finished titles whose id is not in the CSV


def calculate_personal_summary(catalogue_table: pd.DataFrame, watched_ids: list[int],
                               top_rated: dict) -> PersonalSummary:
    """Build the summary of ONE user from data read in SQLite by user id.

    - watched_ids: titles marked Finished (database.get_profile_for_viewer).
      ONLY these feed the watched count and the genre distribution: ratings
      and favourites are never used as viewing history.
    - top_rated: {"movie": [(id, rating)...], "tv show": [...]} chosen from
      the user's own ratings, separately by type.
    EVERY genre of a finished title counts once. A finished title that is
    missing from the CSV still counts as watched, but has no genre.
    """
    unique_watched_ids = list(dict.fromkeys(watched_ids))
    watched_titles = catalogue_table[catalogue_table["id"].isin(unique_watched_ids)]
    genres_per_title = watched_titles["genres"].map(
        lambda genres_text: list(dict.fromkeys(split_genres(genres_text))))
    genre_counts = genres_per_title.explode().dropna().value_counts()
    ranked_genres = sorted(
        ((str(genre), int(count)) for genre, count in genre_counts.items()),
        key=lambda pair: (-pair[1], pair[0].lower()))

    return PersonalSummary(
        titles_watched=len(unique_watched_ids),
        genre_counts=ranked_genres,
        top_movies=list(top_rated.get("movie", [])),
        top_tv_shows=list(top_rated.get("tv show", [])),
        titles_missing_from_catalogue=len(unique_watched_ids) - len(watched_titles),
    )


def build_title_info(catalogue_table: pd.DataFrame) -> dict:
    """{title_id: (type, catalogue_rating)}: what database.py needs to pick top-rated titles."""
    return {int(title_id): (str(title_type).strip().lower(), float(note))
            for title_id, title_type, note in zip(
                catalogue_table["id"], catalogue_table["type"], catalogue_table["note"])}


@st.cache_data(show_spinner=False)
def load_title_info() -> dict:
    """title_info of the real catalogue, computed only once (cached)."""
    return build_title_info(load_catalogue())


# --------------------------------------------------------------------------
# Browsing (Increment 3): search, filters, pagination, posters
# All functions below READ the catalogue table and return new tables:
# they never modify the table they receive (and never the CSV file).
# --------------------------------------------------------------------------

TITLES_PER_PAGE = 12  # BNF-P2

# Values found in the CSV "type" column -> text shown in the interface.
TYPE_LABELS = {"movie": "Movie", "tv show": "TV Show"}

# Picture shown when a title has no usable poster link (BNF-E2).
# It is a small SVG image kept in the code, so no extra file is needed.
DEFAULT_POSTER_IMAGE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="342" height="513" '
    'viewBox="0 0 342 513">'
    '<rect width="342" height="513" fill="#2b2d3a"/>'
    '<text x="171" y="250" font-family="sans-serif" font-size="30" '
    'fill="#c9cbd6" text-anchor="middle">No poster</text>'
    '<text x="171" y="292" font-family="sans-serif" font-size="30" '
    'fill="#c9cbd6" text-anchor="middle">available</text>'
    '</svg>'
)


def get_type_label(raw_type) -> str:
    """Convert a CSV type ("tv show") into its display text ("TV Show")."""
    cleaned_type = str(raw_type).strip().lower()
    return TYPE_LABELS.get(cleaned_type, cleaned_type.title())


def split_genres(genres_text) -> list[str]:
    """Turn the CSV text "Drama, Crime" into the list ["Drama", "Crime"]."""
    if not isinstance(genres_text, str):
        return []
    return [genre.strip() for genre in genres_text.split(",") if genre.strip()]


def get_all_genres(catalogue_table: pd.DataFrame) -> list[str]:
    """Return every genre used in the catalogue, once each, sorted A to Z."""
    unique_genres = set()
    for genres_text in catalogue_table["genres"]:
        unique_genres.update(split_genres(genres_text))
    return sorted(unique_genres, key=str.lower)


@st.cache_data(show_spinner=False)
def load_genre_list() -> list[str]:
    """Genre list for the filter menu, computed only once (cached)."""
    return get_all_genres(load_catalogue())


def normalize_for_search(text) -> str:
    """Lower-case a text and remove accents, so "Pokémon" matches "pokemon"."""
    decomposed_text = unicodedata.normalize("NFKD", str(text))
    without_accents = "".join(
        character for character in decomposed_text
        if not unicodedata.combining(character)
    )
    return without_accents.casefold()


WORD_PATTERN = re.compile(r"[^\W_]+")   # runs of letters / digits of any alphabet


def split_into_words(text) -> list[str]:
    """Words of a text: lower case, no accents, no punctuation.

    "Spider-Man's" gives ["spider", "man", "s"]. Shared by the themes dictionary
    and by the similarity packs (recommandation.py), so both cut texts the same way.
    """
    if not isinstance(text, str):
        return []
    return WORD_PATTERN.findall(normalize_for_search(text))


def keep_letters_and_digits(text: str) -> str:
    """Remove spaces, hyphens and every other punctuation mark from a text.

    "spider-man" and "spider man" both become "spiderman", so a visitor does
    not need to know how a title is punctuated. Only used to COMPARE texts:
    the titles shown in the interface are never changed.
    """
    return "".join(character for character in text if character.isalnum())


def filter_catalogue(catalogue_table: pd.DataFrame, search_text: str = "",
                     title_type: str | None = None,
                     genre: str | None = None,
                     genres: list[str] | None = None,
                     year_range: tuple[int, int] | None = None) -> pd.DataFrame:
    """Return the titles that match the search AND the type AND the genres AND the years.

    - search_text: part of a title (optional). Case, accents, spaces, hyphens and
      other punctuation are ignored, so "spiderman" finds "Spider-Man". It is not
      a pattern. A search made only of punctuation (such as "(" or ".") has no
      letters to compare, so it is searched literally instead.
    - title_type: a CSV value such as "movie" or "tv show"; None = any type.
    - genres: a list of genres; a title must have ALL of them ("Action" does not
      match "Action Comedy"). None or [] = any genre. `genre` (one genre) is kept
      for older callers and is added to the list.
    - year_range: (first_year, last_year), both included; None = any year.
    The original order of the catalogue is kept.
    """
    filtered_table = catalogue_table

    search_text = (search_text or "").strip()
    if search_text:
        wanted_text = normalize_for_search(search_text)
        normalized_titles = filtered_table["titre"].map(normalize_for_search)
        compact_wanted_text = keep_letters_and_digits(wanted_text)
        if compact_wanted_text:
            compact_titles = normalized_titles.map(keep_letters_and_digits)
            title_matches = compact_titles.str.contains(compact_wanted_text, regex=False)
        else:
            # Only punctuation was typed: search it literally.
            title_matches = normalized_titles.str.contains(wanted_text, regex=False)
        filtered_table = filtered_table[title_matches]

    if title_type:
        wanted_type = title_type.strip().lower()
        title_types = filtered_table["type"].str.strip().str.lower()
        filtered_table = filtered_table[title_types == wanted_type]

    wanted_genres = [name.strip().lower() for name in (genres or []) if name and name.strip()]
    if genre and genre.strip():
        wanted_genres.append(genre.strip().lower())
    if wanted_genres:
        has_all_wanted_genres = pd.Series(
            [set(wanted_genres) <= {name.lower() for name in split_genres(genres_text)}
             for genres_text in filtered_table["genres"]],
            index=filtered_table.index,
            dtype=bool,
        )
        filtered_table = filtered_table[has_all_wanted_genres]

    if year_range is not None:
        first_year, last_year = min(year_range), max(year_range)
        filtered_table = filtered_table[filtered_table["annee"].between(first_year, last_year)]

    return filtered_table


def get_year_bounds(catalogue_table: pd.DataFrame) -> tuple[int, int]:
    """(oldest year, newest year) of the catalogue, for the year slider."""
    return int(catalogue_table["annee"].min()), int(catalogue_table["annee"].max())


@st.cache_data(show_spinner=False)
def load_year_bounds() -> tuple[int, int]:
    """Year bounds of the real catalogue, computed only once (cached)."""
    return get_year_bounds(load_catalogue())


def sort_by_external_rating(titles: pd.DataFrame) -> pd.DataFrame:
    """Best external (catalogue/TMDB) rating first; equal ratings are ordered by id.

    The ranking uses the QUALITY score (the rating corrected by the number of votes), so
    a title rated 9.9 by 25 people does not beat a title rated 8.9 by 16,000 people. A table
    without a quality score (a hand-made table) is ranked by the plain rating.
    """
    rating_column = QUALITY_COLUMN if QUALITY_COLUMN in titles.columns else "note"
    return titles.sort_values([rating_column, "id"], ascending=[False, True], kind="mergesort")


def pick_random_title(filtered_table: pd.DataFrame, random_state=None) -> pd.Series | None:
    """Return one title chosen at random from `filtered_table`, or None if it is empty.

    Give it the result of filter_catalogue(): the title is then drawn among ALL
    the titles that match the search, the type and the genre (not only the
    ones on the current page). Uses Pandas' sample (cahier, section 6.3).
    random_state is only for tests: a fixed number gives a repeatable draw.
    """
    if filtered_table.empty:
        return None
    return filtered_table.sample(n=1, random_state=random_state).iloc[0]


def get_page_count(total_titles: int, titles_per_page: int = TITLES_PER_PAGE) -> int:
    """Number of pages needed for `total_titles` titles (0 if there are none)."""
    return math.ceil(total_titles / titles_per_page)


def clamp_page_number(page_number: int, page_count: int) -> int:
    """Keep a page number between 1 and the last page (1 if there are no pages)."""
    return max(1, min(page_number, page_count))


def get_page(filtered_table: pd.DataFrame, page_number: int,
             titles_per_page: int = TITLES_PER_PAGE) -> pd.DataFrame:
    """Return the titles of one page (pages start at 1).

    A page number outside the valid range is moved to the nearest valid page.
    The last page may contain fewer than `titles_per_page` titles.
    """
    page_count = get_page_count(len(filtered_table), titles_per_page)
    page_number = clamp_page_number(page_number, page_count)
    first_row = (page_number - 1) * titles_per_page
    return filtered_table.iloc[first_row:first_row + titles_per_page]


def build_google_search_url(title: str, year) -> str:
    """Address of a Google search for "<title> <year>" (cahier, section 6.3).

    The text is URL-encoded, so characters such as "&" or "#" in a title
    cannot change the address.
    """
    query_text = urllib.parse.urlencode({"q": f"{title} {year}"})
    return f"https://www.google.com/search?{query_text}"


def get_poster_source(poster_value) -> str:
    """Return what st.image() should display for a title.

    The poster link from the CSV when it looks like a web address, otherwise
    the default image (missing value, empty text, or anything not http/https).
    """
    if isinstance(poster_value, str):
        poster_link = poster_value.strip()
        if poster_link.lower().startswith(("http://", "https://")) and \
                len(poster_link.split("//", 1)[1]) > 0:
            return poster_link
    return DEFAULT_POSTER_IMAGE


# --------------------------------------------------------------------------
# Homepage carousels (Increment 8)
# --------------------------------------------------------------------------

CAROUSEL_TITLE_COUNT = 12          # titles per carousel (then a "See more" card)
CAROUSEL_RANDOM_GENRES_PER_TYPE = 2

TYPE_MOVIE = "movie"
TYPE_TV_SHOW = "tv show"


@dataclass
class CarouselDefinition:
    """One homepage row."""
    key: str                  # stable identifier without spaces, e.g. "movie_top" or "tv_show_genre_1"
    heading: str              # text shown above the row
    title_type: str           # CSV value: "movie" or "tv show"
    genre: str | None         # None for a top-rated row
    titles: pd.DataFrame      # at most CAROUSEL_TITLE_COUNT rows, best external rating first


def get_eligible_genres(catalogue_table: pd.DataFrame, title_type: str,
                        minimum_titles: int = CAROUSEL_TITLE_COUNT) -> list[str]:
    """Genres that have at least `minimum_titles` titles of this type, sorted A to Z.

    A genre with fewer titles could not fill a carousel.
    """
    titles_of_type = filter_catalogue(catalogue_table, title_type=title_type)
    genre_counts: dict[str, int] = {}
    for genres_text in titles_of_type["genres"]:
        for genre_name in set(split_genres(genres_text)):
            genre_counts[genre_name] = genre_counts.get(genre_name, 0) + 1
    return sorted((name for name, count in genre_counts.items() if count >= minimum_titles),
                  key=str.lower)


def pick_daily_genres(catalogue_table: pd.DataFrame, title_type: str,
                      day: datetime.date, how_many: int = CAROUSEL_RANDOM_GENRES_PER_TYPE) -> list[str]:
    """Genres of the day for one type: the same for everybody for a whole calendar day.

    The random generator is seeded with the date and the type (not with the
    visitor), so two people opening the page on the same day get the same
    genres, and the genres change when the date changes. The genres returned
    are different from each other.
    """
    eligible_genres = get_eligible_genres(catalogue_table, title_type)
    day_generator = random.Random(f"{day.isoformat()}|{title_type}")
    return day_generator.sample(eligible_genres, min(how_many, len(eligible_genres)))


def build_homepage_carousels(catalogue_table: pd.DataFrame,
                             day: datetime.date) -> list[CarouselDefinition]:
    """The six rows: for Movies then TV Shows, top-rated + two genres of the day."""
    carousels = []
    for title_type, type_name in ((TYPE_MOVIE, "Movies"), (TYPE_TV_SHOW, "TV Shows")):
        key_prefix = title_type.replace(" ", "_")   # becomes part of widget keys / CSS classes
        top_titles = sort_by_external_rating(
            filter_catalogue(catalogue_table, title_type=title_type)
        ).head(CAROUSEL_TITLE_COUNT)
        carousels.append(CarouselDefinition(
            f"{key_prefix}_top", f"Top-Rated {type_name}", title_type, None, top_titles))
        for position, genre_name in enumerate(
                pick_daily_genres(catalogue_table, title_type, day), start=1):
            genre_titles = sort_by_external_rating(
                filter_catalogue(catalogue_table, title_type=title_type, genres=[genre_name])
            ).head(CAROUSEL_TITLE_COUNT)
            carousels.append(CarouselDefinition(
                f"{key_prefix}_genre_{position}", f"{genre_name} {type_name}",
                title_type, genre_name, genre_titles))
    return carousels


@st.cache_data(show_spinner=False)
def load_homepage_carousels(day_text: str) -> list[CarouselDefinition]:
    """Carousels of one calendar day (ISO text such as "2026-11-17"), cached.

    The date is the cache key, so the rows are computed once per day and the
    genres rotate automatically when the date changes.
    """
    return build_homepage_carousels(load_catalogue(), datetime.date.fromisoformat(day_text))


# --------------------------------------------------------------------------
# Themes dictionary (data/themes.csv)
# --------------------------------------------------------------------------
# themes.csv groups words that mean the same thing ("kingdom", "empire", "realm")
# into ONE theme, so that two synopses that use different words for the same idea
# can be recognised as similar. Columns: theme_id, theme_name, keywords
# (keywords = the words / short phrases of the theme, separated by commas).
# The file is only used when the similarity packs are BUILT (recommandation.py);
# the application itself never reads it.

THEMES_CSV_PATH = Path(__file__).resolve().parent / "data" / "themes.csv"
THEME_COLUMNS = ["theme_id", "theme_name", "keywords"]
THEME_TOKEN_PREFIX = "theme:"          # a theme is written "theme:<theme_id>" in the token lists
THEME_KEYWORD_SEPARATORS = re.compile(r"[,;|]")
MAXIMUM_REPORTED_THEME_PROBLEMS = 10


@dataclass
class ThemeDictionary:
    """The themes, ready to be used to cut a text into tokens."""
    # (word, word, ...) of a term -> tokens of ALL its themes, e.g. ("drug", "dealer") -> ("theme:12",)
    # A term written in several themes (such as "kingdom") gives one token per theme.
    term_to_themes: dict[tuple[str, ...], tuple[str, ...]]
    longest_term_length: int                  # in words: the longest phrase to look for
    theme_count: int
    term_count: int                           # different terms written in the file (plural forms not counted)
    shared_term_count: int = 0                # terms written in two themes or more


def build_plural_forms(term_words: tuple[str, ...]) -> list[tuple[str, ...]]:
    """The plural of a term (only its last word changes): "kingdom" gives "kingdoms".

    Lets the dictionary list the singular only. A word that already ends with "s"
    is left alone (it is probably a plural already).
    """
    last_word = term_words[-1]
    if len(last_word) < 3 or last_word.endswith("s"):
        return []
    if last_word.endswith("y") and last_word[-2] not in "aeiou":
        plural_word = last_word[:-1] + "ies"
    elif last_word.endswith(("x", "z", "ch", "sh")):
        plural_word = last_word + "es"
    else:
        plural_word = last_word + "s"
    return [term_words[:-1] + (plural_word,)]


def read_theme_dictionary(csv_path: Path = THEMES_CSV_PATH) -> ThemeDictionary:
    """Read and validate themes.csv. Raises CatalogueError with a clear message if it is invalid.

    - the theme name counts as one of the terms of its theme;
    - a term may belong to SEVERAL themes ("kingdom" can be in a royalty theme and in a fantasy
      theme): when it is found in a text, every one of its themes is counted;
    - plural forms are added automatically and belong to the same themes as their singular.
    """
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise CatalogueError(f"Themes file not found: {csv_path}")
    try:
        theme_table = pd.read_csv(csv_path, encoding="utf-8", dtype="string", keep_default_na=False)
    except pd.errors.EmptyDataError:
        raise CatalogueError(f"Themes file is empty: {csv_path}")
    except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as error:
        raise CatalogueError(f"Themes file could not be read: {error}")
    except OSError as error:
        raise CatalogueError(f"Themes file could not be opened: {error}")

    missing_columns = [name for name in THEME_COLUMNS if name not in theme_table.columns]
    if missing_columns:
        raise CatalogueError(f"Themes file is missing columns: {', '.join(missing_columns)}")

    themes_by_term: dict[tuple[str, ...], list[str]] = {}
    problems: list[str] = []       # only an empty theme_id is a problem now
    theme_ids: set[str] = set()

    for row_number, (theme_id, theme_name, keywords) in enumerate(
            zip(theme_table["theme_id"], theme_table["theme_name"], theme_table["keywords"]), start=2):
        theme_id = str(theme_id).strip()
        if not theme_id:
            problems.append(f"line {row_number}: theme_id is empty")
            continue
        theme_ids.add(theme_id)
        theme_token = f"{THEME_TOKEN_PREFIX}{theme_id}"
        written_terms = [str(theme_name)] + THEME_KEYWORD_SEPARATORS.split(str(keywords))
        for written_term in written_terms:
            term_words = tuple(split_into_words(written_term))
            if not term_words:
                continue
            term_themes = themes_by_term.setdefault(term_words, [])
            if theme_token not in term_themes:
                term_themes.append(theme_token)

    if problems:
        shown_problems = "; ".join(problems[:MAXIMUM_REPORTED_THEME_PROBLEMS])
        more_text = (f" (and {len(problems) - MAXIMUM_REPORTED_THEME_PROBLEMS} more)"
                     if len(problems) > MAXIMUM_REPORTED_THEME_PROBLEMS else "")
        raise CatalogueError(f"Themes file has problems: {shown_problems}{more_text}")
    if not themes_by_term:
        raise CatalogueError("Themes file contains no usable keyword.")

    term_count = len(themes_by_term)
    shared_term_count = sum(1 for term_themes in themes_by_term.values() if len(term_themes) > 1)
    for term_words, term_themes in list(themes_by_term.items()):
        for plural_words in build_plural_forms(term_words):
            plural_themes = themes_by_term.setdefault(plural_words, [])
            plural_themes.extend(theme for theme in term_themes if theme not in plural_themes)

    return ThemeDictionary(
        term_to_themes={term_words: tuple(term_themes) for term_words, term_themes in themes_by_term.items()},
        longest_term_length=max(len(term_words) for term_words in themes_by_term),
        theme_count=len(theme_ids),
        term_count=term_count,
        shared_term_count=shared_term_count,
    )


# --------------------------------------------------------------------------
# Similarity packs and "Could be related"
# --------------------------------------------------------------------------
# For every title, TWO packs of the most similar titles were computed ONCE (see
# recommandation.py) and stored in data/similarity_packs.csv: one pack of movies and
# one pack of TV shows. The application only READS that file.
# "Could be related" mixes the two packs and shows the 6 best entries.

SIMILARITY_PACKS_PATH = Path(__file__).resolve().parent / "data" / "similarity_packs.csv"
SIMILARITY_PACK_COLUMNS = ["id", "type", "rank", "similar_id", "score"]
RELATED_TITLE_COUNT = 6


def read_similarity_packs(packs_path: Path = SIMILARITY_PACKS_PATH,
                          catalogue_table: pd.DataFrame | None = None) -> dict:
    """Read and validate the packs: {title_id: {type: [(similar_id, score), ...]}}, best first.

    `type` is "movie" or "tv show": the type of the SIMILAR titles of that pack.
    Raises CatalogueError with a clear message if the file is missing, unreadable or does not
    match the catalogue (every id must exist), so the interface can show it instead of crashing.
    """
    packs_path = Path(packs_path)
    rebuild_hint = "Build it once with: python recommandation.py"
    if not packs_path.is_file():
        raise CatalogueError(f"Similarity file not found: {packs_path}. {rebuild_hint}")
    try:
        pack_table = pd.read_csv(packs_path, keep_default_na=False,
                                 dtype={"id": "int64", "type": "string", "rank": "int64",
                                        "similar_id": "int64", "score": "float64"})
    except (pd.errors.EmptyDataError, pd.errors.ParserError, ValueError, OSError) as error:
        raise CatalogueError(f"Similarity file could not be read: {error}. {rebuild_hint}")
    if list(pack_table.columns) != SIMILARITY_PACK_COLUMNS:
        raise CatalogueError(f"Similarity file has wrong columns (old format?). {rebuild_hint}")
    if not set(pack_table["type"]) <= {TYPE_MOVIE, TYPE_TV_SHOW}:
        raise CatalogueError(f"Similarity file has an unknown type. {rebuild_hint}")
    if catalogue_table is not None:
        known_ids = set(int(title_id) for title_id in catalogue_table["id"])
        if not (set(pack_table["id"]) | set(pack_table["similar_id"])) <= known_ids:
            raise CatalogueError("Similarity file does not match the catalogue (unknown ids). "
                                 + rebuild_hint)
    pack_table = pack_table.sort_values(["id", "type", "rank"])
    packs: dict = {}
    for title_id, pack_type, similar_id, score in zip(pack_table["id"], pack_table["type"],
                                                      pack_table["similar_id"], pack_table["score"]):
        packs.setdefault(int(title_id), {}).setdefault(str(pack_type), []).append(
            (int(similar_id), float(score)))
    return packs


@st.cache_data(show_spinner=False)
def load_similarity_packs() -> dict:
    """The packs of the real catalogue, read only once (cached)."""
    return read_similarity_packs(SIMILARITY_PACKS_PATH, load_catalogue())


def get_related_titles(catalogue_table: pd.DataFrame, packs: dict, title_id: int,
                       limit: int = RELATED_TITLE_COUNT) -> pd.DataFrame:
    """The titles that "could be related": the `limit` best entries of BOTH packs of this title.

    The movie pack and the TV show pack are mixed and sorted by score (equal scores by id).
    The title itself is never in a pack. A title without a pack gives an empty table.
    """
    mixed_entries = []
    for pack_entries in packs.get(int(title_id), {}).values():
        mixed_entries.extend(pack_entries)
    mixed_entries.sort(key=lambda entry: (-entry[1], entry[0]))
    best_ids = [similar_id for similar_id, _ in mixed_entries][:limit]
    return get_titles_by_ids(catalogue_table, best_ids)


def _run_self_check() -> None:
    """Quick manual check: python catalogue.py"""
    catalogue_table = read_catalogue_csv()
    print(f"[OK] CSV loaded: {len(catalogue_table)} titles, "
          f"{len(catalogue_table.columns)} columns")
    print(f"[OK] Columns: {list(catalogue_table.columns)}")
    print(f"[OK] Types: {catalogue_table['type'].value_counts().to_dict()}")
    for optional_column in OPTIONAL_TEXT_COLUMNS:
        empty_count = int((catalogue_table[optional_column].str.strip() == "").sum())
        print(f"[OK] Optional column {optional_column}: {empty_count} empty value(s)")
    titles_without_votes = int((catalogue_table[VOTE_COUNT_COLUMN] <= 0).sum())
    print(f"[OK] Optional column {VOTE_COUNT_COLUMN}: {titles_without_votes} title(s) without votes")

    first_title = get_title_by_id(catalogue_table, 1)
    print(f"[OK] get_title_by_id(1) -> {first_title['titre']} ({first_title['annee']})")
    print(f"[OK] get_title_by_id(999999) -> {get_title_by_id(catalogue_table, 999999)}")

    try:
        read_catalogue_csv(Path("does_not_exist.csv"))
    except CatalogueError as error:
        print(f"[OK] Missing file handled: {error}")


if __name__ == "__main__":
    _run_self_check()
