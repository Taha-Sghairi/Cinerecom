"""app.py - Streamlit interface of Cinerecom (IHM).

Increments 2-7: authentication, catalogue, title details, personal space,
profiles, recommendations, random mode, summary (see project_status.md).
Increment 8 (Functional refinements):
- navigation by a row of buttons at the top (no sidebar);
- Catalogue = homepage with six carousels, a search bar that opens the Search
  page, and the random mode (signed-in users) at the bottom;
- Search page (optional text, type, several genres, year range, 4 x 3 grid);
- whole cards are clickable (a small piece of CSS, see APP_CSS);
- title details with related titles and an inline "See more" Google link;
- watchlist with Watch / Finished / cancel (one active title per user);
- profiles with avatar, favourites visibility and a public summary;
- Recommendations: a pool of up to 50 titles per history, shown five at a time;
- one reusable sign-in modal for visitors.
Increment 8 rectifications: the visitor returns to the exact page after Sign In /
Create Account (and Back goes one step back on title details),
account changes (pseudo, email, password), a small x to remove a card from a
list, Finished titles at the end of the watchlist in another colour with an
undo, confirmation messages as toasts, a Pie chart / Table view of the genres.
Increment 9 (UI/UX redesign, appearance and ergonomics only):
- Royal Green and Gold theme (.streamlit/config.toml + THEME_CSS);
- one persistent top navigation bar (Home, Search, Random Mode, Recommendations,
  Public Profiles, profile area) hidden on Sign In, Create Account, Manage Account
  and My Space, which all have a Back button to the exact page visited before;
- profile panel sliding in from the right (Manage Account, My Space, Sign Out);
- title details with five auto-saving rating stars; recommendations loader;
  watchlist order Currently Watching > Not Watched > Watched; fade-in animations.
Run with:  streamlit run app.py

This file only builds pages. All account logic (validation, hashing,
database access) lives in database.py, all catalogue logic (search, filters,
carousels, related titles) lives in catalogue.py and the recommendation
calculation lives in recommandation.py. All interface text is in English (BNF-E1).
"""

import datetime
import functools
import hashlib
import html
import secrets
import time

import altair as alt
import pandas as pd
import streamlit as st

import catalogue
import database
import recommandation

# --------------------------------------------------------------------------
# Constants: page names and session_state keys
# --------------------------------------------------------------------------

PAGE_CATALOGUE = "Catalogue"
PAGE_SEARCH = "Search"
PAGE_SIGN_IN = "Sign In"
PAGE_CREATE_ACCOUNT = "Create Account"
PAGE_MY_ACCOUNT = "Manage Account"
PAGE_MY_SPACE = "My Space"
PAGE_RECOMMENDATIONS = "Recommendations"
PAGE_PUBLIC_PROFILES = "Public Profiles"

# Buttons of the navigation row. Visitors see the private pages too: clicking
# one opens the sign-in modal instead (section J of the Increment 8 brief).
VISITOR_NAVIGATION = [PAGE_CATALOGUE, PAGE_RECOMMENDATIONS, PAGE_MY_SPACE,
                      PAGE_MY_ACCOUNT, PAGE_SIGN_IN, PAGE_CREATE_ACCOUNT]
MEMBER_NAVIGATION = [PAGE_CATALOGUE, PAGE_RECOMMENDATIONS, PAGE_MY_SPACE,
                     PAGE_PUBLIC_PROFILES, PAGE_MY_ACCOUNT]
AUTHENTICATION_REQUIRED_PAGES = {PAGE_RECOMMENDATIONS, PAGE_MY_SPACE,
                                 PAGE_MY_ACCOUNT, PAGE_PUBLIC_PROFILES}

# Increment 9. VISITOR_NAVIGATION / MEMBER_NAVIGATION above belong to the old row of buttons
# and are no longer drawn. The new navigation bar is built by show_navigation().
# Pages WITHOUT the navigation bar; each of them has a Back button that returns to the exact
# page visited before (see capture_return_point).
NAVIGATION_HIDDEN_PAGES = {PAGE_SIGN_IN, PAGE_CREATE_ACCOUNT, PAGE_MY_ACCOUNT, PAGE_MY_SPACE}
# Text buttons of the navigation bar (after the Home and Search icons and Random Mode).
NAVIGATION_TEXT_PAGES = [PAGE_RECOMMENDATIONS, PAGE_PUBLIC_PROFILES]
# Pages shown in a narrow, centred column (forms and the vertical recommendation list).
NARROW_PAGES = {PAGE_SIGN_IN, PAGE_CREATE_ACCOUNT, PAGE_MY_ACCOUNT, PAGE_RECOMMENDATIONS}

PROFILE_PANEL_KEY = "show_profile_panel"        # True = open the sliding profile panel at the next run
PROFILE_PANEL_TITLE = "Profile"
VIEW_SIGNATURE_KEY = "view_signature"           # (page, open title, open profile) of the last run
VIEW_EPOCH_KEY = "view_epoch"                   # +1 every time the view changes (fade-in, popover reset)
RECOMMENDATIONS_LOADER_SECONDS = 1.5
RECOMMENDATION_TYPES = (catalogue.TYPE_MOVIE, catalogue.TYPE_TV_SHOW)   # both pools are built together
RECOMMENDATIONS_LOADER_HTML = (
    '<div class="cine-loader"><div class="cine-loader-ring"></div>'
    '<div class="cine-loader-text">Preparing your recommendations...</div></div>')

CURRENT_USER_KEY = "current_user"      # None / missing = nobody is logged in

# Staying signed in after a browser refresh. The session is remembered with a random token kept in the
# address of the page (?session=...) and in the SERVER's memory (never in SQLite). A refresh keeps the
# address, so the user stays signed in; closing the tab and opening the application again starts
# without the token, so the user is signed out. Signing out erases the token.
SESSION_QUERY_PARAMETER = "session"
SESSION_LIFETIME_SECONDS = 8 * 60 * 60
NOTICE_KEY = "notice"                  # one message to show after a page change
CURRENT_PAGE_KEY = "current_page"      # the page shown (the navigation row sets it)
AUTH_MODAL_KEY = "show_auth_modal"     # True = open the sign-in modal at the next run
RETURN_POINT_KEY = "return_point"      # where the visitor was before Sign In / Create Account
ACCOUNT_FAILED_ATTEMPTS_KEY = "account_failed_password_attempts"
MAXIMUM_FAILED_PASSWORD_ATTEMPTS = 5   # wrong current passwords before the user is signed out
NOTICE_ICONS = {"success": "\u2705", "warning": "\u26A0\uFE0F", "error": "\u274C"}

DATABASE_ERROR_MESSAGE = "A database problem occurred. Please try again."

# Exact texts of the sign-in modal (section J).
AUTH_MODAL_TITLE = "Sign in to unlock the full experience"
AUTH_MODAL_MESSAGE = ("Create an account or sign in to rate movies, build your watchlist, "
                      "save favorites, and access personalized features.")

# Type filter shared by the Search page and the personal lists.
# Menu text -> value used by catalogue.filter_catalogue (None = no type filter).
TYPE_FILTER_OPTIONS = {"All": None, "Movies": catalogue.TYPE_MOVIE, "TV Shows": catalogue.TYPE_TV_SHOW}
TYPE_LABEL_BY_RAW_TYPE = {raw_type: label for label, raw_type in TYPE_FILTER_OPTIONS.items()
                          if raw_type is not None}

# Homepage carousels
CAROUSEL_WINDOW_SIZE = 6      # titles visible at the same time
CAROUSEL_STEP = 3             # titles revealed by one arrow click
HOME_SEARCH_BAR_LABEL = "\U0001F50D  Search movies and TV shows..."

# Search page
SEARCH_TEXT_KEY = "search_text"
SEARCH_TYPE_KEY = "search_type"
SEARCH_GENRES_KEY = "search_genres"
SEARCH_YEARS_KEY = "search_years"
SEARCH_PAGE_KEY = "search_page"                 # current result page (number box)
SEARCH_FILTERS_KEY = "search_last_filters"      # to detect a filter change
SEARCH_WIDGET_KEYS = (SEARCH_TEXT_KEY, SEARCH_TYPE_KEY, SEARCH_GENRES_KEY,
                      SEARCH_YEARS_KEY, SEARCH_PAGE_KEY)
SEARCH_RESULT_COLUMNS = 4                       # 4 titles per row x 3 rows = 12 per page
LIVE_SEARCH_DELAY = "0ms"   # "0ms" = the results update after every typed character
MAXIMUM_SEARCH_TEXT_LENGTH = 100

# Characters that Markdown would interpret; they are escaped in titles.
MARKDOWN_SPECIAL_CHARACTERS = "\\`*_{}[]()#+-.!|<>~$:&"

# Title details page
SELECTED_TITLE_KEY = "selected_title_id"                  # title whose details are shown
DETAILS_ORIGIN_KEY = "details_origin_page"               # page where the card was clicked
DETAILS_HISTORY_KEY = "details_history"                  # titles opened before this one (Back goes one step back)
BROWSING_SNAPSHOT_KEY = "browsing_snapshot"              # where the user was on the Search page
BROWSING_WIDGET_KEYS_BY_PAGE = {PAGE_SEARCH: SEARCH_WIDGET_KEYS}
RATING_CHOICES = [1, 2, 3, 4, 5]
TITLE_NOT_FOUND_MESSAGE = "This title could not be found in the catalogue."
# List name used in the database -> name shown in messages.
LIST_DISPLAY_NAMES = {"watchlist": "watchlist", "favorites": "favourites"}
# List name -> (function that adds a title, function that removes a title).
LIST_FUNCTIONS = {
    "watchlist": (database.add_to_watchlist, database.remove_from_watchlist),
    "favorites": (database.add_to_favorites, database.remove_from_favorites),
}

# Personal space and profiles
VIEWED_PROFILE_KEY = "viewed_profile_id"       # user id of the profile being viewed
SPACE_SECTION_KEY = "space_section"
PROFILE_SEARCH_KEY = "profile_search"
VISIBILITY_CHOICE_KEY = "profile_visibility_choice"
FAVORITES_VISIBILITY_CHOICE_KEY = "favorites_visibility_choice"
SPACE_SECTIONS = ("My Ratings", "My Watchlist", "My Favourites", "My Summary")
VISIBILITY_PRIVATE = "Private"
VISIBILITY_PUBLIC = "Public"
VISIBILITY_OPTIONS = [VISIBILITY_PRIVATE, VISIBILITY_PUBLIC]
PROFILE_NOT_AVAILABLE_MESSAGE = "This profile is not available."
LIST_CARDS_PER_ROW = 4
LIST_TITLES_PER_PAGE = catalogue.TITLES_PER_PAGE      # 12
SUMMARY_TOP_TITLES_PER_TYPE = 3
SUMMARY_GENRES_IN_CHART = 3              # the other genres are grouped as "Others" (4 slices at most)
SUMMARY_VIEW_OPTIONS = ("Pie chart", "Table")
SUMMARY_VIEW_KEY = "summary_view"
CHART_COLORS = ["#e0b44c", "#5b9bd5", "#e8706a", "#5fc3b0"]   # the 3 genres + one more
CHART_OTHERS_COLOR = "#9aa0a6"
# Page where the details were opened -> text of the button that goes back.
BACK_BUTTON_LABELS = {
    PAGE_CATALOGUE: "Back to Catalogue",
    PAGE_SEARCH: "Back to Search",
    PAGE_MY_SPACE: "Back to My Space",
    PAGE_PUBLIC_PROFILES: "Back to Public Profiles",
    PAGE_RECOMMENDATIONS: "Back to Recommendations",
}

# Random mode and recommendations
RANDOM_BUTTON_LABEL = "I don't know what to watch!"
RANDOM_CHOICE_KEY = "random_choice_open"          # True = the Movie / TV Show choice is shown
RECOMMENDATION_STATE_KEY = "recommendation_state"          # {raw type: state of its pool}, see the Recommendations section
RECOMMENDATION_TYPE_KEY = "recommendation_type"            # the Movies / TV Shows buttons
RECOMMENDATION_TYPE_OPTIONS = ("Movies", "TV Shows")

# --------------------------------------------------------------------------
# The only CSS of the application
# --------------------------------------------------------------------------
# Streamlit has no clickable container. Each card is a keyed container
# ("card_<scope>_<id>") that holds an invisible st.button; the CSS stretches
# that button over the whole card, so a click anywhere on the card is a normal
# Streamlit button click (handled in Python by open_title_details).

APP_CSS = """
div[class*="st-key-card_"] { position: relative; border-radius: 12px; padding: 6px;
    transition: background-color .15s ease; }
div[class*="st-key-card_"]:hover { background-color: rgba(128,128,128,.16); }
div[class*="st-key-card_"] [data-testid="stElementContainer"],
div[class*="st-key-card_"] [data-testid="stButton"] { position: static; }
div[class*="st-key-card_"] [class*="st-key-open_"] button { position: absolute; top: 0; left: 0;
    width: 100%; height: 100%; margin: 0; padding: 0; border: 0; opacity: 0;
    cursor: pointer; z-index: 5; }
div[class*="st-key-card_"] [class*="st-key-remove_"] button { position: absolute; top: 12px;
    right: 12px; width: 30px; height: 30px; min-height: 0; padding: 0; margin: 0;
    border-radius: 50%; border: 0; background: rgba(0,0,0,.72); color: #fff;
    font-size: 1rem; line-height: 1; z-index: 10; cursor: pointer; }
div[class*="st-key-card_"] [class*="st-key-remove_"] button:hover { background: #c0392b; }
div[class*="st-key-card_"] img { width: 100%; aspect-ratio: 2 / 3; object-fit: cover;
    border-radius: 10px; }
.cine-card-title { font-weight: 600; margin-top: 6px; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
.cine-card-meta { display: flex; justify-content: space-between; gap: 8px;
    font-size: .85rem; opacity: .75; }
.cine-star { color: #e0b44c; }
.cine-seemore { display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: .4rem; aspect-ratio: 2 / 3; font-weight: 600;
    font-size: 1.1rem; border: 1px dashed rgba(128,128,128,.7); border-radius: 10px; }
.cine-avatar { width: 72px; height: 72px; border-radius: 50%; background: #e0b44c;
    color: #1b1b1b; display: flex; align-items: center; justify-content: center;
    font-size: 2rem; font-weight: 700; flex: none; }
.cine-avatar-small { width: 36px; height: 36px; font-size: 1rem; }
.cine-profile-header { display: flex; align-items: center; gap: 1rem; margin: .5rem 0 1rem 0; }
.cine-profile-name { font-size: 1.8rem; font-weight: 700; }
.st-key-home_search_bar button { justify-content: flex-start; border-radius: 999px;
    padding: .6rem 1.2rem; opacity: .85; }
"""


FONT_IMPORT_CSS = "@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@500;700&display=swap');"

# Increment 9: the Royal Green and Gold theme (colours themselves are in .streamlit/config.toml).
THEME_CSS = r"""
:root { --cine-deep: #06281a; --cine-green: #0b3b26; --cine-green-soft: #145238;
    --cine-gold: #d4af37; --cine-gold-soft: #ecd58f; --cine-cream: #f4ebd0; }

/* ---- Frame: own header instead of Streamlit's toolbar, centred content on every page ---- */
header[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"],
footer { display: none !important; }
.stApp { background: radial-gradient(1200px 700px at 50% -8%, #11482f 0%, #082e1e 48%, #041d12 100%) fixed; }
[data-testid="stMainBlockContainer"] { max-width: 1240px; margin: 0 auto; padding: 1rem 2rem 4rem 2rem; }
h1, h2, h3 { font-family: "Playfair Display", Georgia, "Times New Roman", serif;
    color: var(--cine-gold-soft); letter-spacing: .01em; }
[data-testid="stHeading"] h1, [data-testid="stHeading"] h2 { text-align: center; }
[data-testid="stHeading"] h2 { padding-bottom: .5rem; margin-bottom: .4rem; position: relative; }
[data-testid="stHeading"] h2::after { content: ""; display: block; width: 72px; height: 2px;
    margin: .45rem auto 0 auto; background: linear-gradient(90deg, transparent, var(--cine-gold), transparent); }
[data-testid="stCaptionContainer"] { color: rgba(244,235,208,.72); }
[data-testid="stHeaderActionElements"] { display: none; }   /* the hidden anchor link pushes titles off-centre */
/* Wide pages: counters, captions, metrics and small bold headings are centred (cards keep their own text). */
div[class*="st-key-page_view_"][class*="_wide"] [data-testid="stCaptionContainer"],
div[class*="st-key-page_view_"][class*="_wide"] > [data-testid="stElementContainer"] [data-testid="stHeading"] h3,
div[class*="st-key-page_view_"][class*="_wide"] [data-testid="stMetric"] { text-align: center; }
div[class*="st-key-page_view_"] div[class*="st-key-card_"] [data-testid="stCaptionContainer"],
div[class*="st-key-page_view_"] .st-key-details_tracking [data-testid="stCaptionContainer"] { text-align: left; }
div[class*="st-key-page_view_"][class*="_wide"] [data-testid="stMetric"] { display: flex; flex-direction: column; align-items: center; }
div[class*="st-key-page_view_"][class*="_wide"] [data-testid="stMetricLabel"],
div[class*="st-key-page_view_"][class*="_wide"] [data-testid="stMetricValue"] { justify-content: center; }
div[class*="st-key-page_view_"][class*="_wide"] [data-testid="stMarkdownContainer"] p:has(> strong:only-child) { text-align: center; }
.st-key-details_synopsis [data-testid="stMarkdownContainer"] p { text-align: left !important; }
.cine-profile-header { justify-content: center; }

/* ---- Page container: narrow / medium / wide, centred, fades in when the view changes ---- */
@keyframes cine-fade-a { from { opacity: 0; } to { opacity: 1; } }
@keyframes cine-fade-b { from { opacity: 0; } to { opacity: 1; } }
div[class*="st-key-page_view_0_"] { animation: cine-fade-a .45s ease-out backwards; }
div[class*="st-key-page_view_1_"] { animation: cine-fade-b .45s ease-out backwards; }
div[class*="st-key-page_view_"][class*="_narrow"] { max-width: 560px; margin-left: auto; margin-right: auto; }
div[class*="st-key-page_view_"][class*="_medium"] { max-width: 800px; margin-left: auto; margin-right: auto; }
div[class*="st-key-page_view_"][class*="_wide"] { max-width: 100%; }

/* ---- Buttons: gold outline, gold fill on hover, gold gradient for the primary ones ---- */
button[data-testid^="stBaseButton-"], button[data-testid="stPopoverButton"] {
    border: 1px solid rgba(212,175,55,.7); background: rgba(11,59,38,.85); color: var(--cine-gold-soft);
    border-radius: 12px; transition: background-color .18s ease, color .18s ease, transform .18s ease,
    box-shadow .18s ease; }
button[data-testid^="stBaseButton-"]:hover, button[data-testid="stPopoverButton"]:hover {
    background: var(--cine-gold); color: var(--cine-deep); border-color: var(--cine-gold-soft);
    box-shadow: 0 4px 14px rgba(212,175,55,.28); }
button[data-testid^="stBaseButton-"]:active { transform: translateY(1px); }
button[data-testid^="stBaseButton-primary"] { background: linear-gradient(135deg, #ecd58f 0%, #d4af37 55%, #b8902b 100%);
    color: var(--cine-deep); border-color: var(--cine-gold-soft); font-weight: 600; }
button[data-testid^="stBaseButton-primary"]:hover { filter: brightness(1.08); }
button[data-testid^="stBaseButton-"]:disabled { opacity: .45; }
button[data-testid^="stBaseButton-"] p { color: inherit; }
button[data-testid^="stBaseButton-primary"] p { color: var(--cine-deep); }
[data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="select"] > div { border-color: rgba(212,175,55,.45); }
[data-testid="stForm"] { border: 1px solid rgba(212,175,55,.45); border-radius: 16px;
    background: rgba(11,59,38,.55); padding: 1.4rem 1.4rem 1rem 1.4rem; }
[data-testid="stExpander"] { border: 1px solid rgba(212,175,55,.4); border-radius: 12px; }
[data-testid="stAlert"] { border-radius: 12px; }

/* ---- Navigation bar ---- */
/* Streamlit wraps a keyed container in a layout wrapper: THAT element must be the sticky one. */
div[data-testid="stLayoutWrapper"]:has(> .st-key-top_navigation) { position: sticky; top: .6rem; z-index: 990; }
.st-key-top_navigation { margin-bottom: 1.6rem; padding: .55rem 1rem; background: rgba(6,40,26,.94); backdrop-filter: blur(8px);
    border: 1px solid rgba(212,175,55,.55); border-radius: 18px;
    box-shadow: 0 8px 28px rgba(0,0,0,.4); }
.st-key-top_navigation [data-testid="stHorizontalBlock"] { flex-wrap: nowrap; gap: .6rem; }
.st-key-top_navigation button { height: 2.9rem; white-space: nowrap; }
.st-key-nav_catalogue button, .st-key-nav_search button { padding: 0; }
.st-key-nav_catalogue button p, .st-key-nav_search button p { font-size: 1.7rem; line-height: 1; }
.st-key-nav_profile_avatar, .st-key-nav_sign_in_button { width: 100%; display: flex; justify-content: flex-end; }
.st-key-nav_profile_avatar button { width: 3.2rem; height: 3.2rem; padding: 0; border-radius: 50%;
    background: linear-gradient(135deg, #ecd58f, #d4af37); border: 2px solid var(--cine-gold-soft);
    color: var(--cine-deep); font-size: 1.35rem; font-weight: 700; }
.st-key-nav_profile_avatar button p { color: var(--cine-deep); font-size: 1.35rem; font-weight: 700; }
.st-key-nav_sign_in_button button { width: 3.2rem; height: 3.2rem; padding: 0; border-radius: 8px;
    background: linear-gradient(135deg, #ecd58f, #d4af37); color: var(--cine-deep); font-weight: 700;
    border: 2px solid var(--cine-gold-soft); }
.st-key-nav_sign_in_button button p { color: var(--cine-deep); font-size: .72rem; line-height: 1.05;
    white-space: normal; text-align: center; max-width: 2.4rem; }

/* ---- Cards, carousels ---- */
div[class*="st-key-card_"]:hover { background-color: rgba(212,175,55,.12);
    box-shadow: inset 0 0 0 1px rgba(212,175,55,.45); }
div[class*="st-key-card_"] img { box-shadow: 0 6px 18px rgba(0,0,0,.45); }
.cine-card-title { color: var(--cine-cream); }
.cine-seemore { border-color: rgba(212,175,55,.8); color: var(--cine-gold-soft); }
[data-testid="stHeading"] h3 { margin-top: 1.4rem; }
div[class*="st-key-carousel_"] button { width: 2.6rem; height: 2.6rem; padding: 0; border-radius: 50%; }
div[class*="st-key-carousel_previous_"] [data-testid="stButton"] { display: flex; justify-content: flex-end; }
div[class*="st-key-carousel_next_"] [data-testid="stButton"] { display: flex; justify-content: flex-start; }
div[class*="st-key-carousel_"] button p { font-size: 1.5rem; line-height: 1; }

/* ---- Title details ---- */
.cine-details-title { font-family: "Playfair Display", Georgia, serif; font-size: 2.4rem; line-height: 1.15;
    margin: 0 0 .7rem 0; padding: 0; color: var(--cine-gold-soft); text-align: left; }
.cine-chips { display: flex; flex-wrap: wrap; gap: .45rem; margin-bottom: 1.1rem; }
.cine-chip { border: 1px solid rgba(212,175,55,.65); border-radius: 999px; padding: .18rem .8rem;
    font-size: .82rem; color: var(--cine-gold-soft); background: rgba(212,175,55,.08); }
.cine-chip-type { background: var(--cine-gold); color: var(--cine-deep); font-weight: 600; }
.st-key-details_synopsis { padding: 1rem 1.2rem; border-left: 3px solid var(--cine-gold);
    background: rgba(11,59,38,.6); border-radius: 0 12px 12px 0; margin-bottom: 1rem; }
.st-key-details_synopsis p { line-height: 1.7; font-size: 1.02rem; }
/* Title row: the title with both ratings next to it */
.cine-title-row { display: flex; align-items: center; flex-wrap: wrap; gap: .4rem 1.1rem; margin-bottom: .7rem; }
.cine-title-row .cine-details-title { margin: 0; }
.cine-catalogue-rating { border: 1px solid rgba(212,175,55,.75); border-radius: 999px; padding: .1rem .85rem;
    font-size: 1.2rem; font-weight: 700; color: var(--cine-gold-soft); background: rgba(212,175,55,.1); }
.cine-user-rating { display: inline-flex; align-items: center; gap: .05rem; }
.cine-star-fixed { font-size: 1.6rem; line-height: 1; color: transparent; -webkit-text-fill-color: transparent;
    background: linear-gradient(90deg, var(--cine-gold) var(--fill), rgba(244,235,208,.3) var(--fill));
    -webkit-background-clip: text; background-clip: text; }
.cine-votes { margin-left: .5rem; font-size: 1rem; color: rgba(244,235,208,.85); }
.st-key-details_tracking { padding: .9rem 1rem 1rem 1rem; margin-top: .6rem; border-radius: 16px;
    background: rgba(4,29,18,.9); border: 1px solid rgba(212,175,55,.4); }
.cine-rating-label { font-size: .8rem; opacity: .7; }
.cine-rating-value { font-family: "Playfair Display", Georgia, serif; font-size: 1.5rem; color: var(--cine-cream); }

/* ---- Recommendations loader ---- */
@keyframes cine-spin { to { transform: rotate(360deg); } }
@keyframes cine-pulse { 0%, 100% { opacity: .45; } 50% { opacity: 1; } }
/* A full-screen layer under the navigation bar: nothing of the previous page shows through. */
.cine-loader { position: fixed; inset: 0; z-index: 980; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 1.1rem;
    background: radial-gradient(1200px 700px at 50% -8%, #11482f 0%, #082e1e 48%, #041d12 100%); }
.cine-loader-ring { width: 64px; height: 64px; border-radius: 50%; border: 4px solid rgba(212,175,55,.25);
    border-top-color: var(--cine-gold); animation: cine-spin .9s linear infinite; }
.cine-loader-text { color: var(--cine-gold-soft); font-family: "Playfair Display", Georgia, serif;
    font-size: 1.15rem; animation: cine-pulse 1.4s ease-in-out infinite; }

/* ---- Recommendations: centred refresh button ---- */
.st-key-refresh_recommendations_button [data-testid="stButton"] { display: flex; justify-content: center; }

/* ---- My Space and personal lists: centred selectors (their containers shrink-wrap by default) ---- */
.st-key-space_section, .st-key-search_type, .st-key-search_genres, [class*="st-key-list_page_"][class*="_type"] { width: 100%; }
.st-key-space_section [role="radiogroup"], .st-key-search_type [role="radiogroup"],
[class*="st-key-list_page_"][class*="_type"] [role="radiogroup"] { justify-content: center; }
.st-key-space_section [data-testid="stWidgetLabel"], .st-key-search_type [data-testid="stWidgetLabel"],
.st-key-search_genres [data-testid="stWidgetLabel"],
[class*="st-key-list_page_"][class*="_type"] [data-testid="stWidgetLabel"] { justify-content: center; }
.st-key-search_genres [data-testid="stButtonGroup"] { justify-content: center; }
.st-key-space_section label p { font-weight: 600; }

/* ---- Pop-ups: fade-in (the profile panel slides in natively from the right) ---- */
/* Opacity only: the popover is placed with a transform, which an animated transform would override
   (the board would appear in a corner and then jump under its button). */
@keyframes cine-pop { from { opacity: 0; } to { opacity: 1; } }
[data-testid="stDialog"] { background: rgba(2,16,10,.72) !important; backdrop-filter: blur(3px); }
[data-testid="stDialog"] section[role="dialog"] { background: linear-gradient(180deg, #0c3d27 0%, #072a1b 100%);
    border: 1px solid rgba(212,175,55,.55); }
[data-testid="stDialog"] section[role="dialog"] h2 { text-align: left; }
[data-testid="stDialog"] section[role="dialog"] h2::after { display: none; }
[data-testid="stPopoverBody"] { border: 1px solid rgba(212,175,55,.55); animation: cine-pop .22s ease-out; }

/* ---- My Space and personal lists: bigger, clearer buttons ---- */
.st-key-space_section button, [class*="st-key-list_page_"][class*="_type"] button {
    min-height: 3.1rem; padding: .4rem 1.2rem; font-size: 1.05rem; font-weight: 600; }
/* the selected option is solid gold, so the current choice is obvious at a glance */
.st-key-space_section button[aria-checked="true"], [class*="st-key-list_page_"][class*="_type"] button[aria-checked="true"] {
    background: linear-gradient(135deg, #ecd58f 0%, #d4af37 55%, #b8902b 100%); border-color: var(--cine-gold-soft); }
.st-key-space_section button[aria-checked="true"] p, [class*="st-key-list_page_"][class*="_type"] button[aria-checked="true"] p {
    color: var(--cine-deep); font-weight: 700; }
[class*="st-key-list_page_"][class*="_previous"] button, [class*="st-key-list_page_"][class*="_next"] button {
    width: 100%; min-height: 3rem; font-size: 1.05rem; font-weight: 600; }
[class*="st-key-watch_"] button, [class*="st-key-finish_"] button, [class*="st-key-cancel_"] button,
[class*="st-key-undo_"] button { min-height: 2.9rem; font-size: 1rem; font-weight: 600; }
div[class*="st-key-card_"] [class*="st-key-remove_"] button { width: 40px; height: 40px; top: 10px; right: 10px;
    font-size: 1.3rem; font-weight: 700; border: 2px solid rgba(255,255,255,.85);
    background: rgba(0,0,0,.82); box-shadow: 0 2px 8px rgba(0,0,0,.55); }
div[class*="st-key-card_"] [class*="st-key-remove_"] button:hover { background: #c0392b; border-color: #fff; }

/* ---- the script that scrolls to the top holds no content: it is hidden ---- */
div[data-testid="stLayoutWrapper"]:has(> .st-key-scroll_reset), .st-key-scroll_reset { display: none; }

@media (prefers-reduced-motion: reduce) {
    div[class*="st-key-page_view_"], [data-testid="stPopoverBody"], .cine-loader-ring, .cine-loader-text {
        animation: none !important; }
}
"""


# Finished titles of the watchlist get the opposite colours of the page: white
# cards on a dark page, dark cards on a light page.
WATCHED_CARD_CSS_FOR_DARK_PAGE = """
div[class*="st-key-card_watched_"], div[class*="st-key-card_watched_"]:hover { background-color: #efe3bd; }
div[class*="st-key-card_watched_"] .cine-card-title, div[class*="st-key-card_watched_"] .cine-card-meta,
div[class*="st-key-card_watched_"] [data-testid="stCaptionContainer"] { color: #0b2a1b; }
"""
WATCHED_CARD_CSS_FOR_LIGHT_PAGE = """
div[class*="st-key-card_watched_"], div[class*="st-key-card_watched_"]:hover { background-color: #1b1b1b; }
div[class*="st-key-card_watched_"] .cine-card-title, div[class*="st-key-card_watched_"] .cine-card-meta,
div[class*="st-key-card_watched_"] [data-testid="stCaptionContainer"] { color: #f2f2f2; }
"""


def get_theme_type() -> str | None:
    """"light" or "dark" (the theme chosen in the app), or None if it is not known yet."""
    try:
        return st.context.theme.type
    except Exception:
        return None


def build_watched_card_css(theme_type: str | None) -> str:
    """CSS of the Finished cards for the current theme.

    When the theme is not known (first run), the browser's own light/dark
    preference decides.
    """
    if theme_type == "dark":
        return WATCHED_CARD_CSS_FOR_DARK_PAGE
    if theme_type == "light":
        return WATCHED_CARD_CSS_FOR_LIGHT_PAGE
    return (f"@media (prefers-color-scheme: dark) {{ {WATCHED_CARD_CSS_FOR_DARK_PAGE} }}"
            f"@media (prefers-color-scheme: light) {{ {WATCHED_CARD_CSS_FOR_LIGHT_PAGE} }}")


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------

def open_database_connection():
    """Open the database for a "with" block (commit/close handled for us).

    database.DATABASE_PATH is read at call time (not copied at import), so the
    automated tests can point it to a temporary database.
    """
    return database.database_connection(database.DATABASE_PATH)


# --------------------------------------------------------------------------
# Session management (st.session_state)
# --------------------------------------------------------------------------

def get_current_user():
    """Return the logged-in user as a dict (id, pseudo, email), or None."""
    return st.session_state.get(CURRENT_USER_KEY)


def is_logged_in() -> bool:
    """True if a user is logged in during this session."""
    return get_current_user() is not None


def login_user(user_row) -> None:
    """Start the session. We keep only what the interface needs:
    never the password hash or the salt."""
    st.session_state[CURRENT_USER_KEY] = {
        "id": user_row["id"],
        "pseudo": user_row["pseudo"],
        "email": user_row["email"],
    }


@st.cache_resource
def get_session_store() -> dict:
    """The remembered sessions, shared by every browser tab of the running server.

    {hash of the token: (user id, expiry time)}. It lives in the server's memory only: it is never
    written to SQLite or to a file, and it is empty again when the server restarts.
    """
    return {}


def hash_session_token(token: str) -> str:
    """Only the hash of a token is kept in memory (BNF-S1 spirit: no secret is stored as it is)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def remember_session(user_id: int) -> None:
    """After a sign-in: create a token and put it in the address of the page.

    A browser refresh keeps the address, so the user stays signed in (see restore_remembered_session).
    """
    session_store = get_session_store()
    now = time.time()
    for token_hash in [stored_hash for stored_hash, (_, expiry) in session_store.items() if expiry < now]:
        session_store.pop(token_hash, None)               # forget the expired sessions
    token = secrets.token_urlsafe(32)
    session_store[hash_session_token(token)] = (user_id, now + SESSION_LIFETIME_SECONDS)
    st.query_params[SESSION_QUERY_PARAMETER] = token


def forget_session() -> None:
    """Erase the remembered session of this browser tab (server memory and address)."""
    token = st.query_params.get(SESSION_QUERY_PARAMETER)
    if token:
        get_session_store().pop(hash_session_token(token), None)
    if SESSION_QUERY_PARAMETER in st.query_params:
        del st.query_params[SESSION_QUERY_PARAMETER]


def restore_remembered_session() -> None:
    """At the start of every run: nobody is signed in but the address holds a valid token (the page
    was refreshed): sign that user in again. An unknown, expired or invalid token is erased."""
    if is_logged_in():
        return
    token = st.query_params.get(SESSION_QUERY_PARAMETER)
    if not token:
        return
    entry = get_session_store().get(hash_session_token(token))
    user_row = None
    if entry is not None and entry[1] >= time.time():
        try:
            with open_database_connection() as connection:
                user_row = database.get_user_by_id(connection, entry[0])
        except database.DatabaseError:
            return                                        # keep the token: the database may answer next time
    if user_row is None:
        forget_session()
        return
    login_user(user_row)


def logout_user() -> None:
    """End the session: erase the remembered session and everything stored for this visitor."""
    forget_session()
    st.session_state.clear()


def require_login() -> dict:
    """Guard for protected pages: return the user, or show an error and stop.

    Every page that needs an account must call this on its first line.
    """
    current_user = get_current_user()
    if current_user is None:
        st.error("You must be signed in to see this page.")
        st.stop()
    return current_user


# --------------------------------------------------------------------------
# Small interface helpers
# --------------------------------------------------------------------------

def set_notice(level: str, message: str) -> None:
    """Remember a message to display once, after the next page change."""
    st.session_state[NOTICE_KEY] = (level, message)


def show_pending_notice() -> None:
    """Display the remembered message (if any) as a toast, then forget it.

    A toast appears in the corner of the screen wherever the user has scrolled,
    so a confirmation never sends them back to the top of the page.
    """
    notice = st.session_state.pop(NOTICE_KEY, None)
    if notice is not None:
        level, message = notice
        st.toast(message, icon=NOTICE_ICONS.get(level, NOTICE_ICONS["error"]),
                 duration="long" if level == "error" else "short")


def remember_widget_value(widget_key: str) -> None:
    """on_change callback: keep a copy of a widget's value.

    Streamlit forgets a widget's value while the widget is not drawn (for
    example while a title's details page is shown). The copy lets us put the
    value back when the user returns.
    """
    st.session_state[f"{widget_key}_saved"] = st.session_state[widget_key]


def restore_widget_value(widget_key: str) -> None:
    """Put the remembered value back, BEFORE the widget is created."""
    saved_value = st.session_state.get(f"{widget_key}_saved")
    if widget_key not in st.session_state and saved_value is not None:
        st.session_state[widget_key] = saved_value


def handle_remembered_radio_change(widget_key: str, page_key_to_reset: str | None = None) -> None:
    """on_change callback of a remembered selector: keep its value, optionally go back to page 1.

    Clicking the selected button again would clear it: the previous choice is put back instead.
    """
    if st.session_state.get(widget_key) is None:
        saved_value = st.session_state.get(f"{widget_key}_saved")
        if saved_value is not None:
            st.session_state[widget_key] = saved_value
        return
    remember_widget_value(widget_key)
    if page_key_to_reset is not None:
        st.session_state[page_key_to_reset] = 1


def show_remembered_radio(label: str, options, widget_key: str,
                          page_key_to_reset: str | None = None) -> str:
    """A row of big buttons (one is always selected) that keeps its choice when the user comes back."""
    options = list(options)
    if widget_key not in st.session_state:       # Streamlit forgets a widget that was not drawn
        saved_value = st.session_state.get(f"{widget_key}_saved")
        st.session_state[widget_key] = saved_value if saved_value in options else options[0]
        st.session_state[f"{widget_key}_saved"] = st.session_state[widget_key]
    selected = st.segmented_control(label, options, key=widget_key, width="stretch",
                                    label_visibility="collapsed",
                                    on_change=handle_remembered_radio_change,
                                    args=(widget_key, page_key_to_reset))
    return selected if selected in options else st.session_state.get(f"{widget_key}_saved", options[0])


def escape_markdown(text: str) -> str:
    """Put a backslash before characters that Markdown would interpret.

    Some titles contain *, _, [, $ or # (for example "[REC]"), which would
    otherwise change the formatting. The displayed text stays identical.
    """
    return "".join(
        "\\" + character if character in MARKDOWN_SPECIAL_CHARACTERS else character
        for character in text
    )


def escape_html(text) -> str:
    """Make a text safe to put inside the small HTML pieces of the cards and avatars."""
    return html.escape(str(text), quote=True)


def get_avatar_letter(pseudo: str) -> str:
    """The first letter of the pseudo, in capitals ("?" for an empty pseudo)."""
    cleaned_pseudo = str(pseudo).strip()
    return cleaned_pseudo[:1].upper() if cleaned_pseudo else "?"


def show_avatar_header(pseudo: str, tooltip: str | None = None) -> None:
    """Avatar (first letter of the pseudo) next to the pseudo. `tooltip` shows when the cursor is above it."""
    title_attribute = f' title="{escape_html(tooltip)}"' if tooltip else ""
    st.html(f'<div class="cine-profile-header"{title_attribute}><div class="cine-avatar">'
            f'{escape_html(get_avatar_letter(pseudo))}</div>'
            f'<div class="cine-profile-name">{escape_html(pseudo)}</div></div>')


# --------------------------------------------------------------------------
# Navigation and the reusable sign-in modal
# --------------------------------------------------------------------------

def close_title_details() -> None:
    """Close the title details page completely (and forget the titles opened before it)."""
    st.session_state.pop(SELECTED_TITLE_KEY, None)
    st.session_state.pop(DETAILS_ORIGIN_KEY, None)
    st.session_state.pop(DETAILS_HISTORY_KEY, None)


def go_back_from_details() -> None:
    """Callback of the Back button: one step back.

    From a title opened through "Could be related" it returns to the previous
    title; from the first title it returns to the page where the card was clicked.
    """
    previous_titles = st.session_state.get(DETAILS_HISTORY_KEY)
    if previous_titles:
        st.session_state[SELECTED_TITLE_KEY] = previous_titles.pop()
    else:
        close_title_details()


def capture_return_point() -> None:
    """Remember exactly where the visitor is, before Sign In / Create Account replace the page.

    Kept: the page, the open title details (with the titles opened before it and
    the page where they were opened) and the saved Search state. Carousel
    positions and the My Space sections are plain session values that survive anyway.
    A second call while the Sign In / Create Account pages are shown keeps the first point.
    """
    page_name = st.session_state.get(CURRENT_PAGE_KEY, PAGE_CATALOGUE)
    # Already on a page without navigation (Sign In, Manage Account, ...): keep the first point.
    # A title opened from My Space is a different page: it IS remembered.
    if page_name in NAVIGATION_HIDDEN_PAGES and st.session_state.get(SELECTED_TITLE_KEY) is None:
        return
    snapshot = st.session_state.get(BROWSING_SNAPSHOT_KEY)
    widget_keys = BROWSING_WIDGET_KEYS_BY_PAGE.get(page_name)
    if snapshot is None and widget_keys and st.session_state.get(SELECTED_TITLE_KEY) is None:
        snapshot = {key: st.session_state[key] for key in widget_keys if key in st.session_state}
    st.session_state[RETURN_POINT_KEY] = {
        "page": page_name,
        "title_id": st.session_state.get(SELECTED_TITLE_KEY),
        "origin": st.session_state.get(DETAILS_ORIGIN_KEY),
        "history": list(st.session_state.get(DETAILS_HISTORY_KEY) or []),
        "snapshot": snapshot,
    }


def restore_return_point() -> bool:
    """Go back to the remembered point. False if there is none."""
    point = st.session_state.pop(RETURN_POINT_KEY, None)
    if point is None:
        return False
    request_page(point["page"])
    if point["snapshot"]:
        st.session_state[BROWSING_SNAPSHOT_KEY] = point["snapshot"]
    if point["title_id"] is not None:
        st.session_state[SELECTED_TITLE_KEY] = point["title_id"]
        st.session_state[DETAILS_ORIGIN_KEY] = point["origin"] or point["page"]
        st.session_state[DETAILS_HISTORY_KEY] = point["history"]
    return True


def go_back_from_authentication() -> None:
    """Callback of the Back button of the Sign In / Create Account pages."""
    if not restore_return_point():
        request_page(PAGE_CATALOGUE)


def request_page(page_name: str) -> None:
    """Open a page at the next run (used by the navigation, buttons and forms).

    It also closes an open title details page or profile, and forgets the
    saved Search state, so a page always opens fresh. Before the Sign In or
    Create Account page replaces the current page, the visitor's exact
    position is remembered (see capture_return_point).
    """
    if page_name in NAVIGATION_HIDDEN_PAGES:
        capture_return_point()
    st.session_state[CURRENT_PAGE_KEY] = page_name
    close_title_details()
    st.session_state.pop(VIEWED_PROFILE_KEY, None)
    st.session_state.pop(BROWSING_SNAPSHOT_KEY, None)


def request_auth_modal() -> None:
    """Ask for the sign-in modal at the next run (one modal for every restricted action)."""
    st.session_state[AUTH_MODAL_KEY] = True


def navigate_to(page_name: str) -> None:
    """Callback of a navigation button.

    A visitor who clicks a page that needs an account gets the modal and stays
    where they were. Coming back to Recommendations from another page shows the
    pool again from its beginning, slightly shuffled.
    """
    if page_name in AUTHENTICATION_REQUIRED_PAGES and not is_logged_in():
        request_auth_modal()
        return
    came_from_another_page = st.session_state.get(CURRENT_PAGE_KEY) != PAGE_RECOMMENDATIONS
    request_page(page_name)
    if page_name == PAGE_RECOMMENDATIONS and came_from_another_page:
        start_new_recommendation_pass()


def sign_out() -> None:
    """Callback of the Sign Out button."""
    logout_user()
    set_notice("success", "You have been signed out.")
    request_page(PAGE_SIGN_IN)


@st.dialog(AUTH_MODAL_TITLE)
def show_auth_modal() -> None:
    """The sign-in modal shown to a visitor who uses an account-only feature.

    Sign In and Create Account open the matching form; Close (or the cross)
    dismisses the modal and leaves the visitor on the page they were on.
    """
    st.write(AUTH_MODAL_MESSAGE)
    if st.button("Sign In", key="modal_sign_in_button", type="primary", width="stretch"):
        request_page(PAGE_SIGN_IN)
        st.rerun()
    if st.button("Create Account", key="modal_create_account_button", width="stretch"):
        request_page(PAGE_CREATE_ACCOUNT)
        st.rerun()
    if st.button("Close", key="modal_close_button", width="stretch"):
        st.rerun()


def request_profile_panel() -> None:
    """Callback of the avatar: ask for the sliding profile panel at the next run."""
    st.session_state[PROFILE_PANEL_KEY] = True


def build_profile_panel_dialog():
    """Decorator of the profile panel: a dialog that slides in from the RIGHT.

    The right-hand position exists since Streamlit 1.65 (see requirements.txt); an older
    version falls back to a centred dialog instead of crashing (BNF-F2).
    """
    try:
        return st.dialog(PROFILE_PANEL_TITLE, width="small", position="right")
    except TypeError:
        return st.dialog(PROFILE_PANEL_TITLE, width="small")


@build_profile_panel_dialog()
def show_profile_panel() -> None:
    """The sliding panel opened by the avatar: Manage Account, My Space and Sign Out.

    Every button navigates (or signs out) in a callback; the next run does not draw the panel
    again, which closes it. The cross, Escape and a click outside also close it.
    """
    current_user = get_current_user()
    if current_user is None:
        return
    show_avatar_header(current_user["pseudo"])
    # A dialog is a fragment: a click inside it only reruns the dialog, so the callback is followed
    # by a rerun of the WHOLE application (as in show_auth_modal), which also closes the panel.
    if st.button("Manage Account", key="panel_manage_account", icon=":material/manage_accounts:",
                 width="stretch", on_click=navigate_to, args=(PAGE_MY_ACCOUNT,)):
        st.rerun()
    if st.button("My Space", key="panel_my_space", icon=":material/video_library:",
                 width="stretch", on_click=navigate_to, args=(PAGE_MY_SPACE,)):
        st.rerun()
    # Sign Out is NOT done in a callback: a callback would erase the session BEFORE this panel runs again,
    # the panel would then stop at "current_user is None" above and never reach st.rerun(), so the page
    # behind would stay signed in. Here the sign out happens first, then the whole application reruns.
    if st.button("Sign Out", key="sign_out_button", icon=":material/logout:", width="stretch"):
        sign_out()
        st.rerun()


def show_profile_area(current_user) -> None:
    """Far right of the bar: a square Sign In button for a visitor, the round avatar for a user."""
    if current_user is None:
        st.button("Sign In", key="nav_sign_in_button", help="Sign in or create an account",
                  on_click=navigate_to, args=(PAGE_SIGN_IN,))
    else:
        st.button(escape_markdown(get_avatar_letter(current_user["pseudo"])), key="nav_profile_avatar",
                  help=f"Signed in as {escape_markdown(current_user['pseudo'])}",
                  on_click=request_profile_panel)


def show_brand_header() -> None:
    """The application name, shown on the pages that have no navigation bar."""
    st.title("\U0001F3AC Cinerecom")


def show_navigation(current_page: str) -> None:
    """The persistent top bar: Home and Search icons, Random Mode, Recommendations,
    Public Profiles and the profile area (the current page is highlighted).

    A visitor who clicks a page that needs an account gets the sign-in modal (navigate_to).
    The bar is not drawn on the pages of NAVIGATION_HIDDEN_PAGES (see main).
    """
    current_user = get_current_user()

    def button_type(page_name: str) -> str:
        return "primary" if page_name == current_page else "secondary"

    with st.container(key="top_navigation"):
        (home_column, search_column, random_column, recommendations_column,
         profiles_column, _, profile_column) = st.columns(
            [1, 1, 2.4, 2.8, 2.8, 3.2, 1.4], vertical_alignment="center")
        home_column.button(":material/home:", key="nav_catalogue", help="Home",
                           type=button_type(PAGE_CATALOGUE), width="stretch",
                           on_click=navigate_to, args=(PAGE_CATALOGUE,))
        search_column.button(":material/search:", key="nav_search", help="Search",
                             type=button_type(PAGE_SEARCH), width="stretch", on_click=open_search)
        with random_column:
            show_random_mode()
        for column, page_name in zip((recommendations_column, profiles_column), NAVIGATION_TEXT_PAGES):
            column.button(page_name, key=f"nav_{page_name.lower().replace(' ', '_')}",
                          type=button_type(page_name), width="stretch",
                          on_click=navigate_to, args=(page_name,))
        with profile_column:
            show_profile_area(current_user)


# --------------------------------------------------------------------------
# Pages: sign in, create account, my account
# --------------------------------------------------------------------------

def finish_sign_in(user_row) -> None:
    """Start the session, then return to the exact page the visitor came from."""
    login_user(user_row)
    remember_session(user_row["id"])
    set_notice("success", "You are now signed in.")
    if not restore_return_point():
        request_page(PAGE_MY_ACCOUNT)
    st.rerun()


def show_sign_in_page() -> None:
    """Login form: pseudo + password.

    Back returns to the page the visitor was on. After a successful sign-in the
    visitor lands on that same page (same title, same search, same position).
    """
    st.header("Sign In")
    st.button("Back", key="auth_back_button", on_click=go_back_from_authentication)

    with st.form("sign_in_form"):
        pseudo = st.text_input("Pseudo", key="sign_in_pseudo")
        password = st.text_input("Password", type="password", key="sign_in_password")
        submitted = st.form_submit_button("Sign In", key="sign_in_submit")

    if submitted:
        input_errors = database.validate_login_input(pseudo, password)
        if input_errors:
            for error_message in input_errors:
                st.error(error_message)
        else:
            try:
                with open_database_connection() as connection:
                    user_row = database.authenticate_user(connection, pseudo, password)
            except database.DatabaseError:
                st.error(DATABASE_ERROR_MESSAGE)
                return

            if user_row is None:
                # Same message for unknown pseudo and wrong password.
                st.error("Incorrect pseudo or password.")
            else:
                finish_sign_in(user_row)

    st.button("No account yet? Create one", key="go_to_create_account",
              on_click=request_page, args=(PAGE_CREATE_ACCOUNT,))


def show_create_account_page() -> None:
    """Registration form: pseudo, email, password, confirmation."""
    st.header("Create Account")
    st.button("Back", key="auth_back_button", on_click=go_back_from_authentication)

    with st.form("create_account_form"):
        pseudo = st.text_input("Pseudo", key="register_pseudo")
        email = st.text_input("Email", key="register_email")
        password = st.text_input("Password (at least 8 characters, with a letter and a digit)", type="password",
                                 key="register_password")
        password_confirmation = st.text_input("Confirm password", type="password",
                                              key="register_password_confirmation")
        submitted = st.form_submit_button("Create Account", key="register_submit")

    if submitted:
        try:
            with open_database_connection() as connection:
                error_messages = database.register_account(
                    connection, pseudo, email, password, password_confirmation
                )
        except database.DatabaseError:
            st.error(DATABASE_ERROR_MESSAGE)
            return

        if error_messages:
            for error_message in error_messages:
                st.error(error_message)
        else:
            # Success: the user is NOT logged in. We send them to the login page.
            set_notice("success", "Account created successfully. Please sign in.")
            request_page(PAGE_SIGN_IN)
            st.rerun()

    st.button("Already registered? Sign in", key="go_to_sign_in",
              on_click=request_page, args=(PAGE_SIGN_IN,))


def save_profile_settings() -> None:
    """Callback of the "Save settings" button: profile and favourites visibility.

    The user id comes from the SESSION, never from the page, so nobody can
    change another user's settings.
    """
    current_user = get_current_user()
    if current_user is None:
        request_auth_modal()
        return
    chosen_visibility = st.session_state.get(VISIBILITY_CHOICE_KEY)
    chosen_favorites_visibility = st.session_state.get(FAVORITES_VISIBILITY_CHOICE_KEY)
    if (chosen_visibility not in VISIBILITY_OPTIONS
            or chosen_favorites_visibility not in VISIBILITY_OPTIONS):
        set_notice("error", "Choose Public or Private first.")
        return
    try:
        with open_database_connection() as connection:
            saved = database.set_profile_visibility(
                connection, current_user["id"], chosen_visibility == VISIBILITY_PUBLIC)
            saved = database.set_favorites_visibility(
                connection, current_user["id"],
                chosen_favorites_visibility == VISIBILITY_PUBLIC) and saved
    except database.DatabaseError:
        set_notice("error", DATABASE_ERROR_MESSAGE)
        return
    if saved:
        set_notice("success", f"Your profile is now {chosen_visibility} and your "
                              f"favourites are {chosen_favorites_visibility}.")
    else:
        set_notice("error", "Your account could not be found.")


def show_my_account_page() -> None:
    """Protected page: avatar, pseudo, email, current viewing and Profile Settings."""
    current_user = require_login()
    st.header("Manage Account")
    st.button("Back", key="account_back_button", on_click=go_back_from_authentication)
    show_avatar_header(current_user["pseudo"])
    # st.text shows the values as plain text (no formatting of user input).
    st.text(f"Pseudo: {current_user['pseudo']}")
    st.text(f"Email: {current_user['email']}")

    try:
        with open_database_connection() as connection:
            is_public = database.is_profile_public(connection, current_user["id"])
            favorites_are_public = database.is_favorites_public(connection, current_user["id"])
            watching_id = database.get_currently_watching(connection, current_user["id"])
    except database.DatabaseError:
        st.error(DATABASE_ERROR_MESSAGE)
        return

    watching_row = find_catalogue_title(watching_id) if watching_id is not None else None
    if watching_row is not None:
        st.text(f"Currently watching: {watching_row['titre']} ({watching_row['annee']})")

    st.subheader("Profile Settings")
    current_label = VISIBILITY_PUBLIC if is_public else VISIBILITY_PRIVATE
    current_favorites_label = VISIBILITY_PUBLIC if favorites_are_public else VISIBILITY_PRIVATE
    st.radio("Profile visibility", VISIBILITY_OPTIONS,
             index=VISIBILITY_OPTIONS.index(current_label), horizontal=True,
             key=VISIBILITY_CHOICE_KEY,
             help="Public: other signed-in users can find your profile and see your pseudo, avatar, "
                  "summary, top-rated titles and what you are watching. Private: only you can see it. "
                  "Your email, your watchlist and your full list of ratings are never shown to other users.")
    st.radio("Favourites visibility", VISIBILITY_OPTIONS,
             index=VISIBILITY_OPTIONS.index(current_favorites_label), horizontal=True,
             key=FAVORITES_VISIBILITY_CHOICE_KEY,
             help="Your favourites are shown on your public profile only if they are Public here too.")
    st.button("Save settings", key="save_visibility_button", on_click=save_profile_settings)
    show_account_settings(current_user)


# --------------------------------------------------------------------------
# Account settings: change pseudo, email, password (each asks for the current password)
# --------------------------------------------------------------------------

def handle_account_change(error_messages: list[str], success_message: str) -> bool:
    """Show the result of an account change. True if it succeeded.

    Wrong current passwords are counted: after MAXIMUM_FAILED_PASSWORD_ATTEMPTS
    of them the user is signed out (somebody guessing at a session left open).
    """
    if not error_messages:
        st.session_state[ACCOUNT_FAILED_ATTEMPTS_KEY] = 0
        set_notice("success", success_message)
        return True
    if database.WRONG_CURRENT_PASSWORD_MESSAGE in error_messages:
        failed_attempts = st.session_state.get(ACCOUNT_FAILED_ATTEMPTS_KEY, 0) + 1
        st.session_state[ACCOUNT_FAILED_ATTEMPTS_KEY] = failed_attempts
        attempts_left = MAXIMUM_FAILED_PASSWORD_ATTEMPTS - failed_attempts
        if attempts_left <= 0:
            logout_user()
            set_notice("error", "Too many wrong passwords. You have been signed out.")
            request_page(PAGE_SIGN_IN)
            st.rerun()
        plural_ending = "" if attempts_left == 1 else "s"
        st.error(f"{database.WRONG_CURRENT_PASSWORD_MESSAGE} {attempts_left} attempt{plural_ending} "
                 "left before you are signed out.")
        return False
    for error_message in error_messages:
        st.error(error_message)
    return False


def show_account_settings(current_user: dict) -> None:
    """Three forms: change pseudo, change email, change password."""
    st.subheader("Account Settings", help="Every change asks for your current password.")
    user_id = current_user["id"]

    with st.expander("Change pseudo"):
        with st.form("change_pseudo_form", clear_on_submit=True):
            new_pseudo = st.text_input("New pseudo", key="new_pseudo")
            current_password = st.text_input("Current password", type="password",
                                             key="pseudo_current_password")
            submitted = st.form_submit_button("Change pseudo", key="change_pseudo_submit")
        if submitted:
            try:
                with open_database_connection() as connection:
                    errors = database.change_pseudo(connection, user_id, current_password, new_pseudo)
            except database.DatabaseError:
                st.error(DATABASE_ERROR_MESSAGE)
                return
            if handle_account_change(errors, "Your pseudo was changed."):
                st.session_state[CURRENT_USER_KEY]["pseudo"] = new_pseudo.strip()
                st.rerun()

    with st.expander("Change email"):
        with st.form("change_email_form", clear_on_submit=True):
            new_email = st.text_input("New email", key="new_email")
            current_password = st.text_input("Current password", type="password",
                                             key="email_current_password")
            submitted = st.form_submit_button("Change email", key="change_email_submit")
        if submitted:
            try:
                with open_database_connection() as connection:
                    errors = database.change_email(connection, user_id, current_password, new_email)
            except database.DatabaseError:
                st.error(DATABASE_ERROR_MESSAGE)
                return
            if handle_account_change(errors, "Your email was changed."):
                st.session_state[CURRENT_USER_KEY]["email"] = new_email.strip()
                st.rerun()

    with st.expander("Change password"):
        with st.form("change_password_form", clear_on_submit=True):
            current_password = st.text_input("Current password", type="password",
                                             key="password_current")
            new_password = st.text_input("New password (at least 8 characters, with a letter "
                                         "and a digit)", type="password", key="new_password")
            new_password_confirmation = st.text_input("Confirm new password", type="password",
                                                      key="new_password_confirmation")
            submitted = st.form_submit_button("Change password", key="change_password_submit")
        if submitted:
            try:
                with open_database_connection() as connection:
                    errors = database.change_password(
                        connection, user_id, current_password, new_password,
                        new_password_confirmation)
            except database.DatabaseError:
                st.error(DATABASE_ERROR_MESSAGE)
                return
            if handle_account_change(errors, "Your password was changed."):
                st.rerun()


# --------------------------------------------------------------------------
# Title cards (a click anywhere on the card opens the details)
# --------------------------------------------------------------------------

def open_title_details(title_id: int) -> None:
    """Callback of a card click: open the details of a title.

    It remembers the page where the click happened, so the Back button
    returns to it. From the Search page it also remembers the search, filters
    and page (the widgets are not drawn while the details are shown).
    """
    origin_page = st.session_state.get(CURRENT_PAGE_KEY, PAGE_CATALOGUE)
    widget_keys = BROWSING_WIDGET_KEYS_BY_PAGE.get(origin_page)
    # Only when no details are open yet: from a related title the widgets no longer exist.
    if widget_keys and st.session_state.get(SELECTED_TITLE_KEY) is None:
        st.session_state[BROWSING_SNAPSHOT_KEY] = {
            key: st.session_state[key] for key in widget_keys if key in st.session_state
        }
    previous_title_id = st.session_state.get(SELECTED_TITLE_KEY)
    if previous_title_id is not None and previous_title_id != title_id:
        st.session_state.setdefault(DETAILS_HISTORY_KEY, []).append(previous_title_id)
    st.session_state[SELECTED_TITLE_KEY] = title_id
    st.session_state[DETAILS_ORIGIN_KEY] = origin_page


def restore_browsing_snapshot() -> None:
    """Put the saved Search state back (before the widgets exist)."""
    snapshot = st.session_state.pop(BROWSING_SNAPSHOT_KEY, None)
    if snapshot:
        for key, value in snapshot.items():
            st.session_state[key] = value


def find_catalogue_title(title_id):
    """The catalogue row of a title, or None (unknown id or unreadable catalogue)."""
    try:
        return catalogue.get_title_by_id(catalogue.load_catalogue(), title_id)
    except catalogue.CatalogueError:
        return None


def build_card_text_html(title_row, rating_text: str | None = None) -> str:
    """Title line + "year | genre" and star rating line (layout of the reference screenshots).

    rating_text replaces the catalogue rating (for example "4/5" for the user's own rating).
    """
    first_genres = catalogue.split_genres(title_row["genres"])
    left_text = f"{title_row['annee']} | {first_genres[0]}" if first_genres else str(title_row["annee"])
    shown_rating = rating_text if rating_text is not None else f"{title_row['note']:.1f}"
    return (f'<div class="cine-card-title" title="{escape_html(title_row["titre"])}">'
            f'{escape_html(title_row["titre"])}</div>'
            f'<div class="cine-card-meta"><span>{escape_html(left_text)}</span>'
            f'<span><span class="cine-star">\u2605</span> {escape_html(shown_rating)}</span></div>')


def show_title_card(title_row, scope: str, rating_text: str | None = None,
                    extra_caption: str | None = None, remove_callback=None,
                    is_watched: bool = False) -> None:
    """One card: poster, title, year | genre and rating. The whole card is clickable.

    `scope` makes the widget keys unique when the same title appears twice on
    one page (for example in two carousels). remove_callback(title_id) adds a
    small x in the top right corner. is_watched gives the card the Finished colours.
    """
    title_id = int(title_row["id"])
    key_prefix = "card_watched" if is_watched else "card"
    with st.container(key=f"{key_prefix}_{scope}_{title_id}"):
        st.image(catalogue.get_poster_source(title_row["affiche"]), width="stretch")
        st.html(build_card_text_html(title_row, rating_text))
        if extra_caption:
            st.caption(extra_caption)
        # Invisible button: the CSS stretches it over the whole card.
        st.button("Open details", key=f"open_{scope}_{title_id}",
                  on_click=open_title_details, args=(title_id,))
        if remove_callback is not None:
            st.button("\u2715", key=f"remove_{scope}_{title_id}", help="Remove from this list",
                      on_click=remove_callback, args=(title_id,))


def show_title_grid(title_table: pd.DataFrame, scope: str, columns_per_row: int,
                    rating_text_by_id: dict | None = None, caption_by_id: dict | None = None,
                    watch_state: dict | None = None, remove_callback=None,
                    editable_ratings: dict | None = None) -> None:
    """Cards in rows of `columns_per_row`. watch_state adds the Watch controls (watchlist).

    editable_ratings {title_id: rating} adds clickable stars under the cards (My Ratings).
    """
    rating_texts = rating_text_by_id or {}
    captions = caption_by_id or {}
    title_rows = [title_row for _, title_row in title_table.iterrows()]
    for first_in_row in range(0, len(title_rows), columns_per_row):
        row_title_rows = title_rows[first_in_row:first_in_row + columns_per_row]
        empty_slots = columns_per_row - len(row_title_rows)
        if empty_slots:
            # A partial row is centred: spacer columns on both sides keep every card the same width.
            side_weight = empty_slots / 2
            row_columns = st.columns([side_weight] + [1] * len(row_title_rows) + [side_weight])[1:-1]
        else:
            row_columns = st.columns(columns_per_row)
        for column, title_row in zip(row_columns, row_title_rows):
            title_id = int(title_row["id"])
            with column:
                show_title_card(title_row, scope, rating_texts.get(title_id), captions.get(title_id),
                                remove_callback,
                                is_watched=watch_state is not None and title_id in watch_state["finished"])
                if watch_state is not None:
                    show_watch_controls(title_id, watch_state)
                if editable_ratings is not None and title_id in editable_ratings:
                    stars_key = space_rating_widget_key(title_id)
                    # The saved rating is only the starting value: once the widget has a value
                    # (even one we set ourselves) no default is given, as Streamlit requires.
                    st.feedback("stars", key=stars_key,
                                default=None if stars_key in st.session_state
                                else editable_ratings[title_id] - 1,
                                on_change=update_rating_from_space, args=(title_id,))


# --------------------------------------------------------------------------
# Catalogue page = homepage (carousels, search bar, random mode)
# --------------------------------------------------------------------------

def open_search(title_type: str | None = None, genre: str | None = None) -> None:
    """Callback: open the Search page, with a type and a genre preselected if given.

    Every Search widget is forgotten first, so the page starts clean: no text,
    all years, page 1. The preselection is written into the widget keys
    BEFORE the widgets are created (Streamlit allows that).
    """
    request_page(PAGE_SEARCH)
    for widget_key in SEARCH_WIDGET_KEYS + (SEARCH_FILTERS_KEY,):
        st.session_state.pop(widget_key, None)
    if title_type is not None:
        st.session_state[SEARCH_TYPE_KEY] = TYPE_LABEL_BY_RAW_TYPE[title_type]
    if genre is not None:
        st.session_state[SEARCH_GENRES_KEY] = [genre]


def shift_carousel(offset_key: str, step: int) -> None:
    """Callback of a carousel arrow."""
    st.session_state[offset_key] = max(0, st.session_state.get(offset_key, 0) + step)


def show_see_more_card(carousel: catalogue.CarouselDefinition) -> None:
    """Last card of a carousel: opens the Search page with its type (and genre)."""
    with st.container(key=f"card_seemore_{carousel.key}"):
        st.html('<div class="cine-seemore"><span>See more</span><span>\u2192</span></div>')
        st.button("See more", key=f"open_seemore_{carousel.key}", on_click=open_search,
                  args=(carousel.title_type, carousel.genre))


def show_carousel(carousel: catalogue.CarouselDefinition) -> None:
    """One horizontal row: 6 titles visible, arrows move by 3, 12 titles then "See more".

    The titles are already sorted by external rating (catalogue.py).
    """
    title_rows = [title_row for _, title_row in carousel.titles.iterrows()]
    item_count = len(title_rows) + 1          # + the See more card
    offset_key = f"carousel_offset_{carousel.key}"
    last_offset = 0
    if item_count > CAROUSEL_WINDOW_SIZE:
        steps_needed = -(-(item_count - CAROUSEL_WINDOW_SIZE) // CAROUSEL_STEP)   # ceiling
        last_offset = steps_needed * CAROUSEL_STEP
    offset = max(0, min(st.session_state.get(offset_key, 0), last_offset))
    st.session_state[offset_key] = offset

    heading_column, previous_column, next_column = st.columns([16, 1, 1], vertical_alignment="center")
    heading_column.subheader(carousel.heading)
    previous_column.button("\u2039", key=f"carousel_previous_{carousel.key}", width="stretch",
                           on_click=shift_carousel, args=(offset_key, -CAROUSEL_STEP),
                           disabled=offset <= 0)
    next_column.button("\u203a", key=f"carousel_next_{carousel.key}", width="stretch",
                       on_click=shift_carousel, args=(offset_key, CAROUSEL_STEP),
                       disabled=offset >= last_offset)

    visible_items = (title_rows + [None])[offset:offset + CAROUSEL_WINDOW_SIZE]   # None = See more
    for column, item in zip(st.columns(CAROUSEL_WINDOW_SIZE), visible_items):
        with column:
            if item is None:
                show_see_more_card(carousel)
            else:
                show_title_card(item, f"home_{carousel.key}")


def pick_random_title_of_type(raw_type: str) -> None:
    """Callback of the Movie / TV Show choice: open a completely random title of that type.

    The draw covers the WHOLE catalogue of that type: the carousel genres and
    any earlier search filter are ignored.
    """
    if not is_logged_in():
        request_auth_modal()
        return
    st.session_state.pop(RANDOM_CHOICE_KEY, None)
    try:
        type_titles = catalogue.filter_catalogue(catalogue.load_catalogue(), title_type=raw_type)
    except catalogue.CatalogueError:
        set_notice("error", "The catalogue could not be loaded.")
        return
    random_title = catalogue.pick_random_title(type_titles)
    if random_title is None:
        set_notice("error", "No title of this type is available.")
        return
    open_title_details(int(random_title["id"]))


def toggle_random_choice() -> None:
    """Callback of the random button: visitors get the modal, users get the Movie / TV Show choice."""
    if not is_logged_in():
        request_auth_modal()
        return
    st.session_state[RANDOM_CHOICE_KEY] = not st.session_state.get(RANDOM_CHOICE_KEY, False)


def show_random_mode() -> None:
    """Random mode, drawn inside the navigation bar.

    A signed-in user gets a popover with the Movie / TV Show choice (the draw covers the whole
    catalogue of that type, see pick_random_title_of_type). The popover key contains the view
    number, so the popover is closed again as soon as another page or title opens. A visitor gets
    a plain button that opens the sign-in modal (toggle_random_choice).
    """
    if is_logged_in():
        view_epoch = st.session_state.get(VIEW_EPOCH_KEY, 0)
        with st.popover("Random Mode", key=f"nav_random_popover_{view_epoch}", width="stretch"):
            st.markdown(f"**{RANDOM_BUTTON_LABEL}**")
            st.write("What do you feel like watching?")
            movie_column, tv_show_column = st.columns(2)
            movie_column.button("Movie", key="random_movie_button", width="stretch",
                                on_click=pick_random_title_of_type, args=(catalogue.TYPE_MOVIE,))
            tv_show_column.button("TV Show", key="random_tv_show_button", width="stretch",
                                  on_click=pick_random_title_of_type, args=(catalogue.TYPE_TV_SHOW,))
    else:
        st.button("Random Mode", key="random_title_button", help=RANDOM_BUTTON_LABEL,
                  width="stretch", on_click=toggle_random_choice)


def show_catalogue_page() -> None:
    """Homepage: six carousels. Open to visitors and users.

    The search and the random mode are in the navigation bar (show_navigation).
    """
    try:
        carousels = catalogue.load_homepage_carousels(datetime.date.today().isoformat())
    except catalogue.CatalogueError as error:
        st.error(f"The catalogue could not be loaded. {error}")
        return
    for carousel in carousels:
        show_carousel(carousel)


# --------------------------------------------------------------------------
# Search page
# --------------------------------------------------------------------------

def show_page_controls(current_page: int, page_count: int, page_key: str, key_prefix: str) -> None:
    """Previous / Go to page / Next."""
    previous_column, page_column, next_column = st.columns([1, 2, 1])
    previous_column.button("Previous", key=f"{key_prefix}_previous",
                           on_click=change_page, args=(page_key, -1),
                           disabled=current_page <= 1)
    # The number box uses the page key itself, so typing a page number works.
    page_column.number_input("Go to page", min_value=1, max_value=page_count,
                             step=1, format="%d", key=page_key)
    next_column.button("Next", key=f"{key_prefix}_next",
                       on_click=change_page, args=(page_key, 1),
                       disabled=current_page >= page_count)
    st.caption(f"Page {current_page} of {page_count}")


def change_page(page_key: str, step: int) -> None:
    """Callback of a Previous / Next button."""
    st.session_state[page_key] = st.session_state.get(page_key, 1) + step


def show_search_page() -> None:
    """Search: optional text + type + several genres + year range, 4 x 3 grid, pagination.

    No text and no filter shows the whole catalogue.
    """
    st.header("Search")
    restore_browsing_snapshot()   # after "Back to Search"

    try:
        catalogue_table = catalogue.load_catalogue()   # cached: CSV read once
        genre_names = catalogue.load_genre_list()
        oldest_year, newest_year = catalogue.load_year_bounds()
    except catalogue.CatalogueError as error:
        st.error(f"The catalogue could not be loaded. {error}")
        return

    search_text = st.text_input("Search by title (optional)", key=SEARCH_TEXT_KEY,
                                placeholder="Type a title...", live=LIVE_SEARCH_DELAY)
    search_text = search_text[:MAXIMUM_SEARCH_TEXT_LENGTH]
    selected_type_label = st.radio("Type", list(TYPE_FILTER_OPTIONS), horizontal=True,
                                   key=SEARCH_TYPE_KEY)
    selected_genres = st.pills("Genres", genre_names, selection_mode="multi", key=SEARCH_GENRES_KEY,
                               help="A title must have all the selected genres.")
    st.session_state.setdefault(SEARCH_YEARS_KEY, (oldest_year, newest_year))
    selected_years = st.slider("Release year", min_value=oldest_year, max_value=newest_year,
                               key=SEARCH_YEARS_KEY)

    filtered_table = catalogue.filter_catalogue(
        catalogue_table, search_text, TYPE_FILTER_OPTIONS[selected_type_label],
        genres=list(selected_genres or []), year_range=tuple(selected_years))

    # A new search or filter always starts again at page 1.
    current_filters = (search_text, selected_type_label, tuple(selected_genres or []),
                       tuple(selected_years))
    if st.session_state.get(SEARCH_FILTERS_KEY) != current_filters:
        st.session_state[SEARCH_FILTERS_KEY] = current_filters
        st.session_state[SEARCH_PAGE_KEY] = 1

    total_titles = len(filtered_table)
    page_count = catalogue.get_page_count(total_titles)
    if page_count == 0:
        st.info("No titles match your search and filters.")
        return

    current_page = catalogue.clamp_page_number(st.session_state.get(SEARCH_PAGE_KEY, 1), page_count)
    st.session_state[SEARCH_PAGE_KEY] = current_page
    page_table = catalogue.get_page(filtered_table, current_page)
    first_shown = (current_page - 1) * catalogue.TITLES_PER_PAGE + 1
    last_shown = first_shown + len(page_table) - 1
    plural_ending = "" if total_titles == 1 else "s"
    st.caption(f"Showing {first_shown}\u2013{last_shown} of {total_titles:,} title{plural_ending}")

    show_title_grid(page_table, "search", SEARCH_RESULT_COLUMNS)
    show_page_controls(current_page, page_count, SEARCH_PAGE_KEY, "search")


# --------------------------------------------------------------------------
# Title details page
# Visitors can read it. Rating and lists need an account: the callbacks below
# check the session themselves and open the sign-in modal for a visitor.
# --------------------------------------------------------------------------

def rating_widget_key(title_id: int) -> str:
    return f"rating_choice_{title_id}"


def save_rating(title_id: int) -> None:
    """Callback of the rating button of the details page."""
    save_user_rating(title_id, st.session_state.get(rating_widget_key(title_id)))


def space_rating_widget_key(title_id: int) -> str:
    return f"space_rating_{title_id}"


def update_rating_from_space(title_id: int) -> None:
    """Callback of the stars under a card of My Ratings: change the rating without opening the title.

    The stars give 0 to 4 (a star number minus one). Clicking the active star again would clear the
    widget: the saved rating is put back (use the x to remove a rating).
    """
    stars = st.session_state.get(space_rating_widget_key(title_id))
    if stars is None:
        current_user = get_current_user()
        if current_user is not None:
            try:
                with open_database_connection() as connection:
                    saved_rating = database.get_user_rating(connection, current_user["id"], title_id)
            except database.DatabaseError:
                saved_rating = None
            if saved_rating is not None:
                st.session_state[space_rating_widget_key(title_id)] = saved_rating - 1
        return
    save_user_rating(title_id, stars + 1)


def save_user_rating(title_id: int, rating) -> None:
    """Create the signed-in user's rating of a title or change it (a notice tells the result)."""
    current_user = get_current_user()
    if current_user is None:
        request_auth_modal()
        return
    if rating is None:
        set_notice("error", "Choose a rating from 1 to 5 stars first.")
        return
    if find_catalogue_title(title_id) is None:
        set_notice("error", TITLE_NOT_FOUND_MESSAGE)
        return

    try:
        with open_database_connection() as connection:
            previous_rating = database.get_user_rating(connection, current_user["id"], title_id)
            database.set_rating(connection, current_user["id"], title_id, rating)
    except ValueError:
        set_notice("error", "Choose a rating from 1 to 5 stars first.")
        return
    except database.DatabaseError:
        set_notice("error", DATABASE_ERROR_MESSAGE)
        return

    star_word = "star" if rating == 1 else "stars"
    if previous_rating is None:
        set_notice("success", f"Your rating was saved: {rating} {star_word}.")
    else:
        set_notice("success", f"Your rating was updated to {rating} {star_word}.")


def change_list(list_name: str, action: str, title_id: int) -> None:
    """Callback of the watchlist / favourites buttons.

    list_name is "watchlist" or "favorites"; action is "add" or "remove".
    """
    list_label = LIST_DISPLAY_NAMES[list_name]
    current_user = get_current_user()
    if current_user is None:
        request_auth_modal()
        return
    if find_catalogue_title(title_id) is None:
        set_notice("error", TITLE_NOT_FOUND_MESSAGE)
        return

    add_function, remove_function = LIST_FUNCTIONS[list_name]
    action_function = add_function if action == "add" else remove_function
    try:
        with open_database_connection() as connection:
            changed = action_function(connection, current_user["id"], title_id)
    except database.DatabaseError:
        set_notice("error", DATABASE_ERROR_MESSAGE)
        return

    if action == "add":
        if changed:
            set_notice("success", f"Added to your {list_label}.")
        else:   # no duplicate is created
            set_notice("warning", f"This title is already in your {list_label}.")
    else:
        if changed:
            set_notice("success", f"Removed from your {list_label}.")
        else:
            set_notice("warning", f"This title is not in your {list_label}.")


def build_star_rating_html(average_rating, rating_count: int) -> str:
    """Five fixed (not clickable) stars filled according to the average rating of Cinerecom users,
    followed by "(N votes)". A star is filled only for the part that the average reaches: 3.6 gives
    three full stars and a star filled at 60%. The explanation is a tooltip (shown on hover)."""
    average = 0.0 if average_rating is None else max(0.0, min(5.0, float(average_rating)))
    stars = "".join(
        f'<span class="cine-star-fixed" style="--fill: {max(0.0, min(1.0, average - position)) * 100:.0f}%">\u2605</span>'
        for position in range(5))
    vote_word = "vote" if rating_count == 1 else "votes"
    tooltip = (f"Cinerecom users' rating: {average:.1f} out of 5" if rating_count
               else "No Cinerecom user has rated this title yet")
    return (f'<span class="cine-user-rating" title="{escape_html(tooltip)}">{stars}'
            f'<span class="cine-votes">({rating_count} {vote_word})</span></span>')


def build_title_heading_html(title_row, average_rating, rating_count: int) -> str:
    """The title with both ratings right next to it: the catalogue rating as a plain number, then the
    stars of the Cinerecom users. Nothing is labelled on the page (the tooltips explain them)."""
    return ('<div class="cine-title-row">'
            f'<h1 class="cine-details-title">{escape_html(title_row["titre"])}</h1>'
            f'<span class="cine-catalogue-rating" title="Catalogue rating (out of 10)">'
            f'{title_row["note"]:.1f}</span>'
            f'{build_star_rating_html(average_rating, rating_count)}</div>')


def update_rating_from_details(title_id: int) -> None:
    """Callback of the five stars of the details page: the rating is saved AT ONCE (no button).

    The stars give 0 to 4 (a star number minus one). A visitor gets the sign-in modal and the stars
    go back to empty. Clicking the active star again clears the widget: the saved rating is put back
    (a rating is removed with the x of My Space).
    """
    stars_key = rating_widget_key(title_id)
    stars = st.session_state.get(stars_key)
    current_user = get_current_user()
    if current_user is None:
        st.session_state[stars_key] = None   # assigning (not deleting) is what clears the stars on screen
        request_auth_modal()
        return
    if stars is None:
        try:
            with open_database_connection() as connection:
                saved_rating = database.get_user_rating(connection, current_user["id"], title_id)
        except database.DatabaseError:
            saved_rating = None
        if saved_rating is not None:
            st.session_state[stars_key] = saved_rating - 1
        return
    save_user_rating(title_id, stars + 1)


def show_tracking_controls(title_id: int, user_rating, in_watchlist: bool,
                           in_favorites: bool) -> None:
    """Under the poster: "Your rating" with five stars that save at once, then Watchlist and Favorite.

    Visitors see the same controls: using them opens the sign-in modal.
    """
    with st.container(key="details_tracking"):
        is_visitor = get_current_user() is None
        label_column, stars_column = st.columns([5, 6], vertical_alignment="center")
        rating_text = "Not rated" if user_rating is None else f"{user_rating} / 5"
        label_column.html(f'<div class="cine-rating-label">Your rating</div>'
                          f'<div class="cine-rating-value">{rating_text}</div>')
        stars_key = rating_widget_key(title_id)
        # The saved rating is only the starting value: once the widget has a value (even one we
        # set ourselves) no default is given, as Streamlit requires.
        starting_stars = None if user_rating is None else user_rating - 1
        with stars_column:
            st.feedback("stars", key=stars_key,
                        default=None if stars_key in st.session_state else starting_stars,
                        on_change=update_rating_from_details, args=(title_id,))

        watchlist_column, favorites_column = st.columns(2)
        watchlist_action = "remove" if in_watchlist else "add"
        watchlist_column.button(
            "Watchlist", key="watchlist_button", icon=":material/bookmark:",
            type="primary" if in_watchlist else "secondary", width="stretch",
            help=("Sign in to keep a watchlist" if is_visitor else
                  "Remove from your watchlist" if in_watchlist else "Add to your watchlist"),
            on_click=change_list, args=("watchlist", watchlist_action, title_id))
        favorites_action = "remove" if in_favorites else "add"
        favorites_column.button(
            "Favorite", key="favorites_button", icon=":material/favorite:",
            type="primary" if in_favorites else "secondary", width="stretch",
            help=("Sign in to save favourites" if is_visitor else
                  "Remove from your favourites" if in_favorites else "Add to your favourites"),
            on_click=change_list, args=("favorites", favorites_action, title_id))


def show_compact_title_card(title_row, scope: str, caption: str | None = None) -> None:
    """A small horizontal card (poster on the left, text on the right); the whole card is clickable.

    `caption` is an optional small line under the text (for example why a title is recommended).
    """
    title_id = int(title_row["id"])
    with st.container(key=f"card_{scope}_{title_id}"):
        poster_column, text_column = st.columns([1, 2], vertical_alignment="center")
        poster_column.image(catalogue.get_poster_source(title_row["affiche"]), width="stretch")
        text_column.html(build_card_text_html(title_row))
        if caption:
            text_column.caption(caption)
        st.button("Open details", key=f"open_{scope}_{title_id}",
                  on_click=open_title_details, args=(title_id,))


def show_related_titles(title_id: int) -> None:
    """Right-hand column: "Could be related", the (at most) 5 best titles of the stored similarity pack."""
    st.subheader("Could be related")
    try:
        related_table = catalogue.get_related_titles(
            catalogue.load_catalogue(), catalogue.load_similarity_packs(), title_id)
    except catalogue.CatalogueError as error:
        st.caption(str(error))
        return
    if related_table.empty:
        st.caption("Nothing found.")
        return
    for _, related_row in related_table.iterrows():
        show_compact_title_card(related_row, "related")


def build_title_chips_html(title_row) -> str:
    """Type, year and genres of a title as small chips (every value is HTML-escaped)."""
    chips = [f'<span class="cine-chip cine-chip-type">'
             f'{escape_html(catalogue.get_type_label(title_row["type"]))}</span>',
             f'<span class="cine-chip">{escape_html(title_row["annee"])}</span>']
    chips += [f'<span class="cine-chip">{escape_html(genre_name)}</span>'
              for genre_name in catalogue.split_genres(title_row["genres"])]
    return f'<div class="cine-chips">{"".join(chips)}</div>'


def show_title_details_page() -> None:
    """Poster + rating stars and lists (left), description (middle), related titles (right)."""
    title_id = st.session_state.get(SELECTED_TITLE_KEY)
    st.header("Title Details")
    # The widget key stays "back_to_catalogue"; only the text depends on the origin.
    origin_page = st.session_state.get(DETAILS_ORIGIN_KEY, PAGE_CATALOGUE)
    back_label = ("Back to previous title" if st.session_state.get(DETAILS_HISTORY_KEY)
                  else BACK_BUTTON_LABELS.get(origin_page, "Back to Catalogue"))
    st.button(back_label, key="back_to_catalogue", on_click=go_back_from_details)

    title_row = find_catalogue_title(title_id)
    if title_row is None:
        st.error(TITLE_NOT_FOUND_MESSAGE)
        return

    current_user = get_current_user()
    user_rating, in_watchlist, in_favorites = None, False, False
    try:
        with open_database_connection() as connection:
            average_rating, rating_count = database.get_average_rating(connection, title_id)
            if current_user is not None:
                user_rating = database.get_user_rating(connection, current_user["id"], title_id)
                in_watchlist = database.is_in_watchlist(connection, current_user["id"], title_id)
                in_favorites = database.is_in_favorites(connection, current_user["id"], title_id)
    except database.DatabaseError:
        st.error(DATABASE_ERROR_MESSAGE)
        return

    poster_column, information_column, related_column = st.columns([3, 5, 3])
    with poster_column:
        st.image(catalogue.get_poster_source(title_row["affiche"]), width="stretch")
        show_tracking_controls(title_id, user_rating, in_watchlist, in_favorites)
    with information_column:
        st.html(build_title_heading_html(title_row, average_rating, rating_count))
        st.html(build_title_chips_html(title_row))
        with st.container(key="details_synopsis"):
            synopsis = escape_markdown(str(title_row["synopsis"]).strip())
            google_url = catalogue.build_google_search_url(title_row["titre"], title_row["annee"])
            # Markdown links open in a new browser tab; the link ends the description.
            st.markdown(f"{synopsis} [See more]({google_url})" if synopsis else f"[See more]({google_url})")
    with related_column:
        show_related_titles(title_id)


# --------------------------------------------------------------------------
# Watch / Finished / cancel (watchlist cards)
# Rules are enforced in database.py: one active title per user, cancel never
# counts as watched, Finished writes the independent watch history.
# --------------------------------------------------------------------------

WATCH_START_MESSAGES = {
    database.WATCH_STARTED: ("success", "Enjoy! This title is now your current viewing."),
    database.WATCH_ALREADY_WATCHING: ("warning", "You are already watching another title. "
                                                 "Press Finished or X on it first."),
    database.WATCH_NOT_IN_WATCHLIST: ("warning", "Only titles of your watchlist can be watched."),
    database.WATCH_ALREADY_FINISHED: ("warning", "You have already finished this title."),
}


def run_watch_action(action_function, success_message: str, failure_message: str,
                     title_id: int) -> None:
    """Shared body of the Finished and X callbacks (session user, database call, notice)."""
    current_user = get_current_user()
    if current_user is None:
        request_auth_modal()
        return
    try:
        with open_database_connection() as connection:
            done = action_function(connection, current_user["id"], title_id)
    except database.DatabaseError:
        set_notice("error", DATABASE_ERROR_MESSAGE)
        return
    set_notice("success" if done else "warning", success_message if done else failure_message)


def start_watching_title(title_id: int) -> None:
    """Callback of a Watch button."""
    current_user = get_current_user()
    if current_user is None:
        request_auth_modal()
        return
    try:
        with open_database_connection() as connection:
            status = database.start_watching(connection, current_user["id"], title_id)
    except database.DatabaseError:
        set_notice("error", DATABASE_ERROR_MESSAGE)
        return
    level, message = WATCH_START_MESSAGES[status]
    set_notice(level, message)


def finish_watching_title(title_id: int) -> None:
    """Callback of the Finished button."""
    run_watch_action(database.finish_watching, "Marked as watched.",
                     "This title is not your current viewing.", title_id)


def cancel_watching_title(title_id: int) -> None:
    """Callback of the X button: the viewing is cancelled and NOT counted as watched."""
    run_watch_action(database.cancel_watching, "Viewing cancelled. It is not counted as watched.",
                     "This title is not your current viewing.", title_id)


def undo_finished_title(title_id: int) -> None:
    """Callback of the pressable "Watched" button: the title was marked by mistake.

    It goes back to "Finished + X" (the active title). Watch is then available
    again if the user presses X.
    """
    current_user = get_current_user()
    if current_user is None:
        request_auth_modal()
        return
    try:
        with open_database_connection() as connection:
            status = database.undo_finished(connection, current_user["id"], title_id)
    except database.DatabaseError:
        set_notice("error", DATABASE_ERROR_MESSAGE)
        return
    messages = {
        database.UNDO_REOPENED: ("success", "Back to your current viewing: press Finished when "
                                            "you are done, or X to cancel."),
        database.UNDO_ANOTHER_ACTIVE: ("warning", "Finish or cancel your current viewing first."),
        database.UNDO_NOT_IN_WATCHLIST: ("warning", "Add this title to your watchlist first."),
        database.UNDO_NOT_FINISHED: ("warning", "This title is not marked as watched."),
    }
    set_notice(*messages[status])


def show_watch_controls(title_id: int, watch_state: dict) -> None:
    """Under a watchlist card: Watch, or Finished + X for the active title.

    watch_state = {"currently_watching": id or None, "finished": set of ids}.
    While a title is active, the Watch buttons of the others are disabled. A
    finished title shows a pressable "Watched" button that undoes the mistake.
    """
    active_id = watch_state["currently_watching"]
    if active_id == title_id:
        finished_column, cancel_column = st.columns([3, 2])
        finished_column.button("Finished", key=f"finish_{title_id}", type="primary",
                               icon=":material/check_circle:", width="stretch",
                               help="Mark this title as watched",
                               on_click=finish_watching_title, args=(title_id,))
        cancel_column.button("Cancel", key=f"cancel_{title_id}", icon=":material/close:", width="stretch",
                             help="Cancel this viewing (not counted as watched)",
                             on_click=cancel_watching_title, args=(title_id,))
    elif title_id in watch_state["finished"]:
        st.button("\u2713 Watched", key=f"undo_{title_id}", disabled=active_id is not None,
                  width="stretch", on_click=undo_finished_title, args=(title_id,),
                  help="Marked by mistake? Press to undo."
                       if active_id is None else "Finish or cancel your current viewing first.")
    else:
        st.button("Watch", key=f"watch_{title_id}", icon=":material/play_arrow:",
                  disabled=active_id is not None, width="stretch",
                  help="Start watching this title" if active_id is None
                       else "Finish or cancel your current viewing first.",
                  on_click=start_watching_title, args=(title_id,))


# --------------------------------------------------------------------------
# Personal lists (ratings, watchlist, favourites): All / Movies / TV Shows, 12 per page
# --------------------------------------------------------------------------

def show_title_list(title_ids: list[int], page_key: str, empty_message: str,
                    rating_text_by_id: dict | None = None, caption_by_id: dict | None = None,
                    columns_per_row: int = LIST_CARDS_PER_ROW,
                    watch_state: dict | None = None, remove_callback=None,
                    editable_ratings: dict | None = None) -> None:
    """Show title ids as cards, 12 per page, with an All / Movies / TV Shows filter.

    SQLite only gives ids: the titles come from the CSV catalogue.
    """
    try:
        catalogue_table = catalogue.load_catalogue()
    except catalogue.CatalogueError as error:
        st.error(f"The catalogue could not be loaded. {error}")
        return

    title_table = catalogue.get_titles_by_ids(catalogue_table, title_ids)
    if title_table.empty:
        st.info(empty_message)
        return

    selected_type_label = show_remembered_radio(
        "Show", list(TYPE_FILTER_OPTIONS), f"{page_key}_type", page_key_to_reset=page_key)
    shown_table = catalogue.filter_catalogue(
        title_table, title_type=TYPE_FILTER_OPTIONS[selected_type_label])
    if shown_table.empty:
        st.info(f"No {selected_type_label} in this list.")
        return

    page_count = catalogue.get_page_count(len(shown_table), LIST_TITLES_PER_PAGE)
    current_page = catalogue.clamp_page_number(st.session_state.get(page_key, 1), page_count)
    st.session_state[page_key] = current_page
    page_table = catalogue.get_page(shown_table, current_page, LIST_TITLES_PER_PAGE)

    plural_ending = "" if len(shown_table) == 1 else "s"
    st.caption(f"{len(shown_table):,} title{plural_ending}")
    unknown_count = len(set(title_ids)) - len(title_table)
    if unknown_count > 0:
        st.caption(f"{unknown_count} saved title(s) are no longer in the catalogue "
                   "and are not shown.")

    show_title_grid(page_table, page_key, columns_per_row, rating_text_by_id, caption_by_id,
                    watch_state, remove_callback, editable_ratings)

    if page_count > 1:
        previous_column, page_column, next_column = st.columns([1, 2, 1])
        previous_column.button("Previous", key=f"{page_key}_previous",
                               on_click=change_page, args=(page_key, -1),
                               disabled=current_page <= 1)
        page_column.caption(f"Page {current_page} of {page_count}")
        next_column.button("Next", key=f"{page_key}_next",
                           on_click=change_page, args=(page_key, 1),
                           disabled=current_page >= page_count)


# --------------------------------------------------------------------------
# Summary ("Mon Bilan"): watched count, genre chart, top-rated titles, average rating
# --------------------------------------------------------------------------

def build_genre_chart_frame(genre_counts: list[tuple[str, int]]) -> pd.DataFrame:
    """The 3 most watched genres plus one "Others" slice (so 4 slices at most).

    Columns: genre, titles, percent (share of the counted genres) and label
    (the text written on the slice; empty for a very thin slice).
    """
    top_genres = genre_counts[:SUMMARY_GENRES_IN_CHART]
    others_count = sum(count for _, count in genre_counts[SUMMARY_GENRES_IN_CHART:])
    chart_rows = [{"genre": genre, "titles": count} for genre, count in top_genres]
    if others_count > 0:
        chart_rows.append({"genre": "Others", "titles": others_count})
    frame = pd.DataFrame(chart_rows, columns=["genre", "titles"])
    frame["percent"] = 100 * frame["titles"] / frame["titles"].sum() if len(frame) else []
    frame["label"] = frame["percent"].map(lambda value: f"{value:.0f}%" if value >= 6 else "")
    return frame


def build_genre_table_frame(genre_counts: list[tuple[str, int]]) -> pd.DataFrame:
    """EVERY genre watched (not only the top ones): genre, titles, share in percent."""
    frame = pd.DataFrame(genre_counts, columns=["Genre", "Titles"])
    frame["Share (%)"] = (100 * frame["Titles"] / frame["Titles"].sum()).round(1)
    return frame


def build_genre_chart(frame: pd.DataFrame) -> alt.Chart:
    """Donut chart: percentages written on the slices and a large legend underneath."""
    genre_order = list(frame["genre"])
    colors = [CHART_OTHERS_COLOR if genre == "Others" else CHART_COLORS[position]
              for position, genre in enumerate(genre_order)]
    base = alt.Chart(frame).encode(
        theta=alt.Theta("titles:Q", stack=True),
        order=alt.Order("titles:Q", sort="descending"),
        color=alt.Color(
            "genre:N", sort=genre_order, scale=alt.Scale(domain=genre_order, range=colors),
            legend=alt.Legend(title=None, orient="bottom", labelFontSize=17, symbolSize=500,
                              columns=2, labelLimit=300)),
        tooltip=[alt.Tooltip("genre:N", title="Genre"),
                 alt.Tooltip("titles:Q", title="Finished titles"),
                 alt.Tooltip("percent:Q", title="Share (%)", format=".1f")])
    slices = base.mark_arc(innerRadius=70, outerRadius=150)
    # The labels must not inherit the slice colour (they would be invisible).
    labels = alt.Chart(frame).mark_text(
        radius=110, size=18, fontWeight="bold", color="#111111").encode(
        theta=alt.Theta("titles:Q", stack=True),
        order=alt.Order("titles:Q", sort="descending"),
        text=alt.Text("label:N"))
    return (slices + labels).properties(height=380)


def show_genre_summary(genre_counts: list[tuple[str, int]]) -> None:
    """Genres of the finished titles, as a Pie chart or as a Table (large switch)."""
    selected_view = st.segmented_control(
        "View", SUMMARY_VIEW_OPTIONS, default=SUMMARY_VIEW_OPTIONS[0], key=SUMMARY_VIEW_KEY,
        width="stretch", label_visibility="collapsed",
        help=f"Pie chart: the {SUMMARY_GENRES_IN_CHART} most watched genres, the others grouped in Others. "
             "Table: every genre watched; a title with several genres counts in each of them.")
    if selected_view == "Table":
        st.dataframe(build_genre_table_frame(genre_counts), hide_index=True, width="stretch")
    else:
        st.altair_chart(build_genre_chart(build_genre_chart_frame(genre_counts)), width="stretch")


def show_top_rated_titles(heading: str, top_rated: list[tuple[int, int]], scope: str,
                          is_own: bool) -> None:
    """Up to three cards of the user's best-rated titles of one type."""
    st.markdown(f"**{heading}**")
    if not top_rated:
        st.caption("No rated title of this type yet.")
        return
    try:
        title_table = catalogue.get_titles_by_ids(
            catalogue.load_catalogue(), [title_id for title_id, _ in top_rated])
    except catalogue.CatalogueError as error:
        st.error(f"The catalogue could not be loaded. {error}")
        return
    rating_prefix = "Your rating" if is_own else "Rated"
    show_title_grid(title_table, scope, SUMMARY_TOP_TITLES_PER_TYPE,
                    rating_text_by_id={title_id: f"{rating}/5" for title_id, rating in top_rated},
                    caption_by_id={title_id: f"{rating_prefix}: {rating} / 5"
                                   for title_id, rating in top_rated})


def show_summary(profile: dict) -> None:
    """The summary of a profile dictionary (database.get_profile_for_viewer).

    Watched count and genres come ONLY from titles marked Finished. The top
    three movies / TV shows come from the user's ratings.
    """
    try:
        catalogue_table = catalogue.load_catalogue()
    except catalogue.CatalogueError as error:
        st.error(f"The catalogue could not be loaded. {error}")
        return
    summary = catalogue.calculate_personal_summary(
        catalogue_table, profile["watched_ids"], profile["top_rated"])

    st.metric("Titles Watched", summary.titles_watched,
              help="Counts only the titles marked Finished.")

    st.markdown("**Genres of watched titles**")
    if summary.genre_counts:
        show_genre_summary(summary.genre_counts)
        if summary.titles_missing_from_catalogue > 0:
            st.caption(f"{summary.titles_missing_from_catalogue} finished title(s) are no longer "
                       "in the catalogue: they count as watched, but not in the chart.")
    elif profile["is_own"]:
        st.info("You have not finished any title yet. Press Finished on a title of your "
                "watchlist to see your statistics here.")
    else:
        st.info("No finished titles yet.")

    movies_column, tv_shows_column = st.columns(2)
    with movies_column:
        show_top_rated_titles("Top-rated movies", summary.top_movies,
                              f"summary_{profile['id']}_movies", profile["is_own"])
    with tv_shows_column:
        show_top_rated_titles("Top-rated TV shows", summary.top_tv_shows,
                              f"summary_{profile['id']}_tv_shows", profile["is_own"])


def load_title_info_or_empty() -> dict:
    """{id: (type, catalogue rating)} for the privacy layer ({} if the catalogue is unreadable)."""
    try:
        return catalogue.load_title_info()
    except catalogue.CatalogueError:
        return {}


# --------------------------------------------------------------------------
# My Space
# --------------------------------------------------------------------------

def remove_my_rating(title_id: int) -> None:
    """Callback of the x on a rated title: delete the user's rating (the title stays in other lists)."""
    current_user = get_current_user()
    if current_user is None:
        request_auth_modal()
        return
    try:
        with open_database_connection() as connection:
            removed = database.remove_rating(connection, current_user["id"], title_id)
    except database.DatabaseError:
        set_notice("error", DATABASE_ERROR_MESSAGE)
        return
    set_notice("success" if removed else "warning",
               "Your rating was removed." if removed else "You had not rated this title.")


def put_finished_titles_last(title_ids: list[int], finished_ids: set) -> list[int]:
    """Watchlist order: titles not finished first, finished titles last (each group keeps its order)."""
    return ([title_id for title_id in title_ids if title_id not in finished_ids]
            + [title_id for title_id in title_ids if title_id in finished_ids])


def order_watchlist_titles(title_ids: list[int], watch_state: dict) -> list[int]:
    """Watchlist order: 1. Currently Watching, 2. Not Watched, 3. Watched.

    The order is calculated from the database state at every run, so a title moves to its
    place by itself as soon as its status changes (Watch, Finished, X, undo).
    """
    active_id = watch_state["currently_watching"]
    other_ids = [title_id for title_id in title_ids if title_id != active_id]
    ordered_ids = put_finished_titles_last(other_ids, watch_state["finished"])
    return ([active_id] if active_id in title_ids else []) + ordered_ids


def show_my_space_page() -> None:
    """The signed-in user's OWN ratings, watchlist, favourites and summary.

    The user id comes from the session. There is no way to ask for another
    user's data from this page.
    """
    current_user = require_login()
    user_id = current_user["id"]
    st.header("My Space")
    st.button("Back", key="space_back_button", on_click=go_back_from_authentication)
    selected_section = show_remembered_radio("Section", SPACE_SECTIONS, SPACE_SECTION_KEY)

    try:
        with open_database_connection() as connection:
            if selected_section == "My Ratings":
                user_ratings = database.get_user_ratings(connection, user_id)
            elif selected_section == "My Watchlist":
                watchlist_ids = database.get_user_watchlist(connection, user_id)
                watch_state = {
                    "currently_watching": database.get_currently_watching(connection, user_id),
                    "finished": set(database.get_user_watched(connection, user_id)),
                }
            elif selected_section == "My Favourites":
                favorite_ids = database.get_user_favorites(connection, user_id)
            else:
                profile = database.get_profile_for_viewer(
                    connection, user_id, user_id, load_title_info_or_empty())
    except database.DatabaseError:
        st.error(DATABASE_ERROR_MESSAGE)
        return

    if selected_section == "My Ratings":
        show_title_list(
            [title_id for title_id, _ in user_ratings], "list_page_my_ratings",
            "You have not rated any title yet. Open a title from the Catalogue to rate it.",
            rating_text_by_id={title_id: f"{rating}/5" for title_id, rating in user_ratings},
            remove_callback=remove_my_rating, editable_ratings=dict(user_ratings))
    elif selected_section == "My Watchlist":
        show_title_list(order_watchlist_titles(watchlist_ids, watch_state),
                        "list_page_my_watchlist",
                        "Your watchlist is empty. Add titles from their details page.",
                        watch_state=watch_state,
                        remove_callback=functools.partial(change_list, "watchlist", "remove"))
    elif selected_section == "My Favourites":
        show_title_list(favorite_ids, "list_page_my_favorites",
                        "You have no favourites yet. Add titles from their details page.",
                        remove_callback=functools.partial(change_list, "favorites", "remove"))
    elif profile is None:
        st.error("Your account could not be found.")
    else:
        show_summary(profile)


# --------------------------------------------------------------------------
# Public profiles
# --------------------------------------------------------------------------

def open_profile(profile_user_id: int) -> None:
    """Callback of a "View profile" button."""
    st.session_state[VIEWED_PROFILE_KEY] = profile_user_id


def close_profile() -> None:
    """Callback of the "Back to Public Profiles" button."""
    st.session_state.pop(VIEWED_PROFILE_KEY, None)


def show_profile_view(current_user: dict, profile_user_id) -> None:
    """One profile: avatar, pseudo, current viewing, summary and (if shared) favourites.

    NEVER the watchlist, the email or the full ratings list. What may be shown
    is decided by database.get_profile_for_viewer each time this page is
    drawn: a profile made private after it was opened is refused.
    """
    st.button("Back to Public Profiles", key="back_to_profile_search", on_click=close_profile)
    try:
        with open_database_connection() as connection:
            profile = database.get_profile_for_viewer(
                connection, current_user["id"], profile_user_id, load_title_info_or_empty())
    except database.DatabaseError:
        st.error(DATABASE_ERROR_MESSAGE)
        return
    if profile is None:
        # Same message for an unknown id and a private profile.
        st.error(PROFILE_NOT_AVAILABLE_MESSAGE)
        return

    own_profile_tooltip = None
    if profile["is_own"]:
        own_profile_tooltip = ("Your own profile. It is Public: other signed-in users see what is on this "
                               "page (your favourites only if you made them Public)." if profile["is_public"]
                               else "Your own profile. It is Private: only you can see this page.")
    show_avatar_header(profile["pseudo"], own_profile_tooltip)

    if profile["currently_watching"] is not None:
        watching_row = find_catalogue_title(profile["currently_watching"])
        if watching_row is not None:
            st.subheader("Currently watching")
            show_title_grid(watching_row.to_frame().T, f"profile_{profile['id']}_watching", 4)

    st.subheader("Summary")
    show_summary(profile)

    st.subheader("Favourites")
    if profile["favorites"] is None:
        st.info("This user keeps their favourites private.")
    else:
        show_title_list(profile["favorites"], f"list_page_profile_{profile['id']}_favorites",
                        "No favourites yet.")


def show_public_profiles_page() -> None:
    """Find public profiles by pseudo and open one (signed-in users only)."""
    current_user = require_login()
    st.header("Public Profiles")

    viewed_profile_id = st.session_state.get(VIEWED_PROFILE_KEY)
    if viewed_profile_id is not None:
        show_profile_view(current_user, viewed_profile_id)
        return

    restore_widget_value(PROFILE_SEARCH_KEY)
    search_text = st.text_input("Search by pseudo", key=PROFILE_SEARCH_KEY,
                                placeholder="Type a pseudo...",
                                on_change=remember_widget_value, args=(PROFILE_SEARCH_KEY,))
    st.button("View my own profile", key="view_my_profile_button",
              on_click=open_profile, args=(current_user["id"],))

    try:
        with open_database_connection() as connection:
            found_profiles = database.search_public_profiles(
                connection, current_user["id"], search_text[:MAXIMUM_SEARCH_TEXT_LENGTH])
    except database.DatabaseError:
        st.error(DATABASE_ERROR_MESSAGE)
        return

    if not found_profiles:
        st.info("No public profiles match your search.")
        return
    st.caption(f"{len(found_profiles)} public profile(s) shown "
               f"(at most {database.MAXIMUM_SEARCH_RESULTS}).")
    for profile_user_id, pseudo in found_profiles:
        avatar_column, name_column, button_column = st.columns([1, 6, 2], vertical_alignment="center")
        avatar_column.html(f'<div class="cine-avatar cine-avatar-small">'
                           f'{escape_html(get_avatar_letter(pseudo))}</div>')
        name_column.markdown(f"**{escape_markdown(pseudo)}**")
        button_column.button("View profile", key=f"view_profile_{profile_user_id}",
                             on_click=open_profile, args=(profile_user_id,))


# --------------------------------------------------------------------------
# Recommendations (signed-in users only)
# The calculation is in recommandation.py: EVERY title of the user's history (ratings, favourites,
# finished titles, watchlist) votes for the titles of its similarity pack, which gives a POOL of up
# to 50 titles of the chosen type. The pool depends only on the history, so it is kept in
# st.session_state (nothing is stored in SQLite) and rebuilt ONLY when the history changes (its
# "signature" changes). It is shown five titles at a time: the Refresh button moves five further.
# When the whole pool has been shown, or when the user comes back to the page from another page,
# the pool starts again from its beginning with a small shuffle (inside blocks of ten titles, so the
# best ones stay near the top): it looks new, but it is the same pool.
# One state per type (Movies / TV Shows):
#   {"signature", "pool_ids", "mode", "filler_count", "reasons", "pass_number", "page_index"}
# --------------------------------------------------------------------------

def start_new_recommendation_pass() -> None:
    """The user came back to the page: every pool starts again from its beginning, slightly shuffled."""
    for state in st.session_state.get(RECOMMENDATION_STATE_KEY, {}).values():
        state["pass_number"] += 1
        state["page_index"] = 0


def show_next_recommendations(raw_type: str) -> None:
    """Callback of the Refresh button: the next five titles of the pool.

    After the last ones, the pool starts again (a new pass, shuffled a little).
    """
    state = st.session_state.get(RECOMMENDATION_STATE_KEY, {}).get(raw_type)
    if state is None:
        return
    state["page_index"] += 1
    if state["page_index"] * recommandation.RECOMMENDATIONS_PER_PAGE >= len(state["pool_ids"]):
        state["pass_number"] += 1
        state["page_index"] = 0


def read_recommendation_history(user_id: int) -> dict:
    """Everything the user did (read in SQLite by user id), as the keyword arguments of recommandation.py."""
    with open_database_connection() as connection:
        return {
            "favorite_ids": database.get_user_favorites(connection, user_id),
            "user_ratings": database.get_user_ratings(connection, user_id),
            "watchlist_ids": database.get_user_watchlist(connection, user_id),
            "finished_ids": database.get_user_watched(connection, user_id),
        }


def build_reason_caption(catalogue_table: pd.DataFrame, reason_ids: list[int]) -> str | None:
    """"Since you liked A, B" (the history titles that made a title a candidate), or None."""
    reason_names = []
    for reason_id in reason_ids:
        reason_row = catalogue.get_title_by_id(catalogue_table, reason_id)
        if reason_row is not None:
            reason_names.append(escape_markdown(str(reason_row["titre"])))
    return f"Since you liked {', '.join(reason_names)}" if reason_names else None


def show_recommendations_loader() -> None:
    """Animated loader, shown every time the recommendations are (re)computed: the first visit of a
    session (so after a refresh, after the application was closed, after a sign out and a new sign
    in) and after ANY change of the user's space (rating, favourite, watchlist, finished title).

    The pools themselves are built almost instantly, so the loader lasts a fixed short time
    (RECOMMENDATIONS_LOADER_SECONDS) and is then removed. It is drawn by CSS only.
    """
    loader_placeholder = st.empty()
    loader_placeholder.html(RECOMMENDATIONS_LOADER_HTML)
    time.sleep(RECOMMENDATIONS_LOADER_SECONDS)
    loader_placeholder.empty()


def build_recommendation_state(catalogue_table: pd.DataFrame, packs: dict, raw_type: str,
                               signature: str, history: dict) -> dict:
    """The stored state of one type: its pool, built for the history that has this signature."""
    pool = recommandation.build_recommendation_pool(catalogue_table, packs, raw_type, **history)
    return {"signature": signature, "pool_ids": pool.title_ids, "mode": pool.mode,
            "filler_count": pool.filler_count, "reasons": pool.reasons,
            "pass_number": 0, "page_index": 0}


RECOMMENDATION_MODE_NOTES = {
    recommandation.MODE_TOP_RATED: "You have no history yet: rate titles or add favourites to get "
                                   "recommendations made for you. Until then, these are the best-rated titles.",
    recommandation.MODE_MIXED: "There were not enough similar titles, so a few best-rated titles "
                               "complete the list.",
}


def show_recommendations_page() -> None:
    """Movies / TV Shows buttons, a refresh button and five titles listed vertically (no ranking)."""
    current_user = require_login()
    st.header("Recommendations")

    selected_label = st.segmented_control(
        "Recommend me", RECOMMENDATION_TYPE_OPTIONS, default=RECOMMENDATION_TYPE_OPTIONS[0],
        key=RECOMMENDATION_TYPE_KEY, width="stretch", label_visibility="collapsed")
    raw_type = TYPE_FILTER_OPTIONS[selected_label or RECOMMENDATION_TYPE_OPTIONS[0]]

    try:
        catalogue_table = catalogue.load_catalogue()
        packs = catalogue.load_similarity_packs()
    except catalogue.CatalogueError as error:
        st.error(f"The recommendations could not be loaded. {error}")
        return
    try:
        history = read_recommendation_history(current_user["id"])
    except database.DatabaseError:
        st.error(DATABASE_ERROR_MESSAGE)
        return

    # The pools are kept while the history is the one they were built for. Anything new in the user's
    # space (or a new session) changes the signature: both pools are rebuilt, behind the loader.
    signatures = {title_type: recommandation.build_history_signature(title_type, **history)
                  for title_type in RECOMMENDATION_TYPES}
    states = st.session_state.setdefault(RECOMMENDATION_STATE_KEY, {})
    if any(states.get(title_type) is None or states[title_type]["signature"] != signatures[title_type]
           for title_type in RECOMMENDATION_TYPES):
        show_recommendations_loader()
        for title_type in RECOMMENDATION_TYPES:
            states[title_type] = build_recommendation_state(
                catalogue_table, packs, title_type, signatures[title_type], history)
    state = states[raw_type]

    mode_note = RECOMMENDATION_MODE_NOTES.get(state["mode"])
    st.button("Refresh recommendations", key="refresh_recommendations_button",
              on_click=show_next_recommendations, args=(raw_type,),
              help="Show the next five recommendations." + (f" {mode_note}" if mode_note else ""))

    arranged_ids = recommandation.arrange_pool_for_display(
        state["pool_ids"], state["signature"], state["pass_number"])
    page_ids = recommandation.get_recommendation_page(arranged_ids, state["page_index"])
    if not page_ids:
        st.info("There are no titles to recommend right now.")
        return

    first_shown = state["page_index"] * recommandation.RECOMMENDATIONS_PER_PAGE + 1
    st.caption(f"Recommendations {first_shown}\u2013{first_shown + len(page_ids) - 1} of {len(arranged_ids)}")

    recommended_table = catalogue.get_titles_by_ids(catalogue_table, page_ids)
    # The page itself is a narrow centred column (see NARROW_PAGES): the list stays vertical.
    for _, title_row in recommended_table.iterrows():
        show_compact_title_card(
            title_row, "recommendations",
            build_reason_caption(catalogue_table, state["reasons"].get(int(title_row["id"]), [])))


# --------------------------------------------------------------------------
# Main: page selection
# --------------------------------------------------------------------------

def resolve_current_page() -> str:
    """The page to draw. A page that needs an account falls back to the Catalogue for a visitor."""
    page_name = st.session_state.get(CURRENT_PAGE_KEY, PAGE_CATALOGUE)
    if page_name in AUTHENTICATION_REQUIRED_PAGES and not is_logged_in():
        page_name = PAGE_CATALOGUE
    if page_name in (PAGE_SIGN_IN, PAGE_CREATE_ACCOUNT) and is_logged_in():
        page_name = PAGE_MY_ACCOUNT
    st.session_state[CURRENT_PAGE_KEY] = page_name
    return page_name


def update_view_epoch(selected_page: str) -> int:
    """Count the view changes (another page, title or profile).

    The number alternates the fade-in animation of the page container and renews the key of the
    Random Mode popover (which closes it). Reruns that do not change the view leave it alone, so
    clicking a star or a button never replays the fade-in.
    """
    view_signature = (selected_page, st.session_state.get(SELECTED_TITLE_KEY),
                      st.session_state.get(VIEWED_PROFILE_KEY))
    if st.session_state.get(VIEW_SIGNATURE_KEY) != view_signature:
        st.session_state[VIEW_SIGNATURE_KEY] = view_signature
        st.session_state[VIEW_EPOCH_KEY] = st.session_state.get(VIEW_EPOCH_KEY, 0) + 1
    return st.session_state[VIEW_EPOCH_KEY]


SCROLL_TO_TOP_SCRIPT = (
    "<script>(function () {"
    " var goToTop = function () {"
    "  ['[data-testid=\"stMain\"]', 'section.main', '[data-testid=\"stAppViewContainer\"]'].forEach(function (selector) {"
    "   var element = document.querySelector(selector); if (element) { element.scrollTop = 0; } });"
    "  window.scrollTo(0, 0); };"
    " goToTop(); setTimeout(goToTop, 80); setTimeout(goToTop, 300);"
    "})(); /* view __VIEW__ */</script>")


def scroll_to_top(view_epoch: int) -> None:
    """Put the page back at its top. The script text contains the view number: it changes only when
    the view changes (another page, title or profile), and Streamlit runs the script again only
    when its text changes. So the page starts at the top after a view change and never jumps
    during a click on a star or a button."""
    with st.container(key="scroll_reset"):          # hidden by the CSS (it only holds the script)
        st.html(SCROLL_TO_TOP_SCRIPT.replace("__VIEW__", str(view_epoch)), unsafe_allow_javascript=True)


def get_page_width_class(selected_page: str) -> str:
    """"narrow" (forms, recommendations), "medium" (profile search) or "wide" (everything else)."""
    if st.session_state.get(SELECTED_TITLE_KEY) is not None:
        return "wide"
    if selected_page in NARROW_PAGES:
        return "narrow"
    if selected_page == PAGE_PUBLIC_PROFILES and st.session_state.get(VIEWED_PROFILE_KEY) is None:
        return "medium"
    return "wide"


def main() -> None:
    st.set_page_config(page_title="Cinerecom", page_icon="\U0001F3AC", layout="wide",
                       initial_sidebar_state="collapsed")
    st.html(f"<style>{FONT_IMPORT_CSS}</style>"
            f"<style>{APP_CSS}{THEME_CSS}{build_watched_card_css(get_theme_type())}</style>")

    try:
        database.initialize_database(database.DATABASE_PATH)
    except database.DatabaseError:
        st.error(DATABASE_ERROR_MESSAGE)
        st.stop()
    restore_remembered_session()      # a refreshed page keeps the user signed in

    page_functions = {
        PAGE_CATALOGUE: show_catalogue_page,
        PAGE_SEARCH: show_search_page,
        PAGE_SIGN_IN: show_sign_in_page,
        PAGE_CREATE_ACCOUNT: show_create_account_page,
        PAGE_MY_ACCOUNT: show_my_account_page,
        PAGE_MY_SPACE: show_my_space_page,
        PAGE_PUBLIC_PROFILES: show_public_profiles_page,
        PAGE_RECOMMENDATIONS: show_recommendations_page,
    }
    selected_page = resolve_current_page()
    view_epoch = update_view_epoch(selected_page)
    title_details_open = st.session_state.get(SELECTED_TITLE_KEY) is not None

    # The bar is hidden on Sign In, Create Account, Manage Account and My Space. A title opened
    # from My Space is a page of its own and has the bar (its Back button returns to My Space).
    if selected_page in NAVIGATION_HIDDEN_PAGES and not title_details_open:
        show_brand_header()
    else:
        show_navigation(selected_page)
    show_pending_notice()   # toast: visible wherever the user has scrolled

    # An open title details page replaces the page it was opened from; the
    # navigation buttons and the Back button close it. The container is centred
    # and fades in again every time the view changes (see update_view_epoch).
    with st.container(key=f"page_view_{view_epoch % 2}_{get_page_width_class(selected_page)}"):
        scroll_to_top(view_epoch)
        if title_details_open:
            show_title_details_page()
        else:
            page_functions[selected_page]()

    # The single sign-in modal, asked for by any restricted action of a visitor.
    if st.session_state.pop(AUTH_MODAL_KEY, False):
        show_auth_modal()
    # The sliding profile panel, asked for by the avatar.
    if st.session_state.pop(PROFILE_PANEL_KEY, False):
        show_profile_panel()


if __name__ == "__main__":
    main()
