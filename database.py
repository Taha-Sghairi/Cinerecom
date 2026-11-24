"""database.py - SQLite storage for Cinerecom (users, ratings, watchlist, favorites).

Increment 1 (Data Layer): connection helpers, table creation with all
constraints, and small reusable data-access functions.
Increment 2 (Authentication): account functions (validation, password
hashing, registration, login), placed in the "Accounts" section below.
Increment 4 (Title details and personal tracking): ratings, watchlist and
favorites functions, in the "Ratings, watchlist and favorites" section below.
Increment 5 (Personal space and profiles): reading a user's own data,
profile visibility, public-profile search and the single permission check
for viewing a profile, in the "Personal space and profiles" section below.

Increment 8 (Functional refinements): independent viewing history
("watch_history") and the single active viewing ("currently_watching"), the
favourites visibility setting, a schema migration for existing databases,
and the privacy rules of the new public profile.

Increment 8 rectifications: password policy (letter + digit), account changes
(pseudo, email, password; each asks for the current password), removing a
rating, undoing "Finished", and no average rating in the profile.

Rules followed here:
- SQLite never stores catalogue data. Tables only keep the title id
  ("titre_id"), which points to a row of data/titres.csv (cahier, section 6.4).
- Every query uses "?" placeholders and never string formatting (BNF-S2).
- Constraints in the schema block duplicates and invalid values (BNF-F1).
"""

import hashlib
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Absolute path built from this file's location (works from any folder).
DATA_DIRECTORY = Path(__file__).resolve().parent / "data"
DATABASE_PATH = DATA_DIRECTORY / "cinerecom.db"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class DatabaseError(Exception):
    """Any database problem (file locked, disk error, bad query...).

    The interface will catch it and show a message instead of crashing (BNF-F2).
    """


class DatabaseIntegrityError(DatabaseError):
    """A constraint was violated (duplicate pseudo, rating outside 1-5, ...).

    It is a separate class so that later code can show a precise message,
    for example "This pseudo is already taken".
    """


# --------------------------------------------------------------------------
# Schema (cahier des charges, Table 9)
# --------------------------------------------------------------------------

# COLLATE NOCASE makes "Taha" and "taha" count as the same pseudo/email.
CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY,
    pseudo              TEXT    NOT NULL UNIQUE COLLATE NOCASE
                                CHECK (length(trim(pseudo)) > 0),
    email               TEXT    NOT NULL UNIQUE COLLATE NOCASE
                                CHECK (length(trim(email)) > 0),
    mot_de_passe_hache  TEXT    NOT NULL CHECK (length(mot_de_passe_hache) > 0),
    sel                 TEXT    NOT NULL CHECK (length(sel) > 0),
    profil_public       INTEGER NOT NULL DEFAULT 0 CHECK (profil_public IN (0, 1)),
    favorites_public    INTEGER NOT NULL DEFAULT 0 CHECK (favorites_public IN (0, 1))
)
"""

# One rating per (user, title), as an integer from 1 to 5.
# typeof(note) = 'integer' rejects values such as 3.5.
CREATE_RATINGS_TABLE = """
CREATE TABLE IF NOT EXISTS ratings (
    user_id   INTEGER NOT NULL,
    titre_id  INTEGER NOT NULL,
    note      INTEGER NOT NULL
              CHECK (typeof(note) = 'integer' AND note BETWEEN 1 AND 5),
    PRIMARY KEY (user_id, titre_id),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
)
"""

# A title appears at most once in a user's watchlist (primary key).
CREATE_WATCHLIST_TABLE = """
CREATE TABLE IF NOT EXISTS watchlist (
    user_id   INTEGER NOT NULL,
    titre_id  INTEGER NOT NULL,
    PRIMARY KEY (user_id, titre_id),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
)
"""

# A title appears at most once in a user's favorites (primary key).
CREATE_FAVORITES_TABLE = """
CREATE TABLE IF NOT EXISTS favorites (
    user_id   INTEGER NOT NULL,
    titre_id  INTEGER NOT NULL,
    PRIMARY KEY (user_id, titre_id),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
)
"""

# Titles the user explicitly marked as Finished (Increment 8). It does NOT
# refer to the watchlist: removing a title from the watchlist never erases
# its viewing history. Ratings and favourites never write here.
CREATE_WATCH_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS watch_history (
    user_id   INTEGER NOT NULL,
    titre_id  INTEGER NOT NULL,
    PRIMARY KEY (user_id, titre_id),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
)
"""

# The title a user is watching right now (Increment 8). The PRIMARY KEY on
# user_id alone means a user can NEVER have two active titles: the database
# refuses a second row. The composite foreign key to the watchlist means the
# active title must be in the watchlist, and removing it from the watchlist
# automatically cancels the session (the row is deleted by ON DELETE CASCADE).
CREATE_CURRENTLY_WATCHING_TABLE = """
CREATE TABLE IF NOT EXISTS currently_watching (
    user_id   INTEGER PRIMARY KEY,
    titre_id  INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, titre_id) REFERENCES watchlist (user_id, titre_id)
        ON DELETE CASCADE
)
"""

# "users" first, "watchlist" before "currently_watching": tables refer to them.
ALL_TABLE_STATEMENTS = [
    CREATE_USERS_TABLE,
    CREATE_RATINGS_TABLE,
    CREATE_WATCHLIST_TABLE,
    CREATE_FAVORITES_TABLE,
    CREATE_WATCH_HISTORY_TABLE,
    CREATE_CURRENTLY_WATCHING_TABLE,
]

# Increment 8 column added to the "users" table of databases created earlier.
ADD_FAVORITES_PUBLIC_COLUMN = (
    "ALTER TABLE users ADD COLUMN favorites_public INTEGER NOT NULL DEFAULT 0 "
    "CHECK (favorites_public IN (0, 1))"
)


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

def create_connection(database_path=DATABASE_PATH) -> sqlite3.Connection:
    """Open a SQLite connection. The caller must close it.

    - foreign keys are OFF by default in SQLite, so we turn them ON;
    - rows can be read by column name: row["pseudo"].
    """
    try:
        connection = sqlite3.connect(str(database_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    except sqlite3.Error as error:
        raise DatabaseError(f"Could not open the database: {error}") from error


@contextmanager
def database_connection(database_path=DATABASE_PATH):
    """Open a connection for a "with" block, then clean up automatically.

    Usage:
        with database_connection() as connection:
            fetch_all(connection, "SELECT * FROM users")

    At the end of the block: changes are saved (commit) if everything went
    well, cancelled (rollback) if an error occurred, and the connection is
    always closed. A new connection per block also avoids thread problems,
    because Streamlit re-runs the script on every user action.
    """
    connection = create_connection(database_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Data access (generic, parameterized only)
# --------------------------------------------------------------------------

def _check_parameters(parameters) -> None:
    """Parameters must be a tuple/list matching the "?" placeholders."""
    if not isinstance(parameters, (tuple, list)):
        raise DatabaseError(
            "Query parameters must be given as a tuple, e.g. (pseudo,)."
        )


def _run_query(connection: sqlite3.Connection, query: str, parameters) -> sqlite3.Cursor:
    """Execute one query and translate SQLite errors into our own errors."""
    _check_parameters(parameters)
    try:
        return connection.execute(query, parameters)
    except sqlite3.IntegrityError as error:
        raise DatabaseIntegrityError(f"Constraint violated: {error}") from error
    except sqlite3.Error as error:
        raise DatabaseError(f"Database error: {error}") from error


def execute_write(connection: sqlite3.Connection, query: str, parameters=()) -> int:
    """Run an INSERT / UPDATE / DELETE. Returns the id of the last inserted row.

    Always write the values as "?" in the query and pass them in `parameters`:
        execute_write(connection,
                      "INSERT INTO favorites (user_id, titre_id) VALUES (?, ?)",
                      (user_id, title_id))
    """
    cursor = _run_query(connection, query, parameters)
    return cursor.lastrowid


def fetch_one(connection: sqlite3.Connection, query: str, parameters=()):
    """Run a SELECT and return the first row, or None if there is no result."""
    return _run_query(connection, query, parameters).fetchone()


def fetch_all(connection: sqlite3.Connection, query: str, parameters=()) -> list:
    """Run a SELECT and return all rows as a list (empty list if no result)."""
    return _run_query(connection, query, parameters).fetchall()


def get_table_names(connection: sqlite3.Connection) -> list[str]:
    """Return the names of the tables in the database, sorted alphabetically."""
    rows = fetch_all(
        connection,
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )
    return [row["name"] for row in rows]


# --------------------------------------------------------------------------
# Initialization
# --------------------------------------------------------------------------

def migrate_schema(connection: sqlite3.Connection) -> None:
    """Bring a database created by an earlier increment up to date.

    Only ADDS what is missing: existing users, ratings, watchlists and
    favourites are never changed or deleted. New accounts and old accounts
    get favourites visibility "private" (0).
    """
    user_columns = [row["name"] for row in fetch_all(connection, "PRAGMA table_info(users)")]
    if "favorites_public" not in user_columns:
        _run_query(connection, ADD_FAVORITES_PUBLIC_COLUMN, ())


def create_tables(connection: sqlite3.Connection) -> None:
    """Create the six tables if they do not exist yet, then migrate old ones.

    Safe to call again: existing tables and their content are left untouched.
    """
    for table_statement in ALL_TABLE_STATEMENTS:
        _run_query(connection, table_statement, ())
    migrate_schema(connection)


def initialize_database(database_path=DATABASE_PATH) -> None:
    """Create data/cinerecom.db (and the data folder) with all tables.

    Safe to call at every start of the application: existing tables and
    their content are left untouched.
    """
    try:
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DatabaseError(f"Could not create the data folder: {error}") from error

    with database_connection(database_path) as connection:
        create_tables(connection)


# --------------------------------------------------------------------------
# Accounts (Increment 2): validation, password hashing, registration, login
# --------------------------------------------------------------------------

MINIMUM_PASSWORD_LENGTH = 8  # BNF-S3

# New accounts are private by default. The table default is also 0; create_user()
# still writes this value explicitly, so privacy never depends on the schema alone.
NEW_ACCOUNT_PROFIL_PUBLIC = 0

# Password hashing settings (BNF-S1).
# PBKDF2-HMAC-SHA256 from hashlib repeats the hash many times, which makes
# guessing passwords slow for an attacker. Only the hash and the salt are
# stored, never the password. Do NOT change these values once accounts exist:
# old hashes would no longer match.
PASSWORD_HASH_ALGORITHM = "sha256"
PASSWORD_HASH_ITERATIONS = 600_000
SALT_SIZE_IN_BYTES = 16

# Simple format check: something@something.something, without spaces.
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# Used only to spend the same time when a pseudo does not exist (see authenticate_user).
_UNUSED_SALT = "00" * SALT_SIZE_IN_BYTES


PASSWORD_POLICY_MESSAGE = "Password must contain at least one letter and one digit."


def validate_new_password(password: str) -> list[str]:
    """Password policy for a NEW password (registration and password change).

    At least MINIMUM_PASSWORD_LENGTH characters (BNF-S3), with at least one
    letter and one digit. Existing accounts are not affected: only the
    passwords chosen from now on must follow it.
    """
    if not password:
        return ["Password is required."]
    error_messages = []
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        error_messages.append(
            f"Password must contain at least {MINIMUM_PASSWORD_LENGTH} characters.")
    if not (any(character.isalpha() for character in password)
            and any(character.isdigit() for character in password)):
        error_messages.append(PASSWORD_POLICY_MESSAGE)
    return error_messages


def validate_registration_input(pseudo: str, email: str, password: str,
                                password_confirmation: str) -> list[str]:
    """Check the registration form (BNF-S3). Returns a list of error messages.

    An empty list means the input is valid. No database access here.
    """
    error_messages = []

    if not pseudo.strip():
        error_messages.append("Pseudo is required.")

    if not email.strip():
        error_messages.append("Email is required.")
    elif not EMAIL_PATTERN.fullmatch(email.strip()):
        error_messages.append("Email address is not valid.")

    error_messages.extend(validate_new_password(password))

    if not password_confirmation:
        error_messages.append("Password confirmation is required.")
    elif password != password_confirmation:
        error_messages.append("Passwords do not match.")

    return error_messages


def validate_login_input(pseudo: str, password: str) -> list[str]:
    """Check the login form. Returns a list of error messages (empty = valid)."""
    error_messages = []
    if not pseudo.strip():
        error_messages.append("Pseudo is required.")
    if not password:
        error_messages.append("Password is required.")
    return error_messages


def generate_salt() -> str:
    """Return a new random salt (32 hex characters). Each user gets their own."""
    return secrets.token_hex(SALT_SIZE_IN_BYTES)


def hash_password(password: str, salt: str) -> str:
    """Return the hash (64 hex characters) of a password with the given salt."""
    hashed_bytes = hashlib.pbkdf2_hmac(
        PASSWORD_HASH_ALGORITHM,
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PASSWORD_HASH_ITERATIONS,
    )
    return hashed_bytes.hex()


def verify_password(password: str, salt: str, stored_hash: str) -> bool:
    """Check a password against the stored salt and hash.

    secrets.compare_digest compares in constant time, so the answer time
    does not leak how many characters were correct.
    """
    return secrets.compare_digest(hash_password(password, salt), stored_hash)


def get_user_by_pseudo(connection: sqlite3.Connection, pseudo: str):
    """Return the user row with this pseudo (case-insensitive), or None."""
    return fetch_one(connection, "SELECT * FROM users WHERE pseudo = ?", (pseudo,))


def get_user_by_email(connection: sqlite3.Connection, email: str):
    """Return the user row with this email (case-insensitive), or None."""
    return fetch_one(connection, "SELECT * FROM users WHERE email = ?", (email,))


def get_user_by_id(connection: sqlite3.Connection, user_id: int):
    """Return the user row with this id, or None (used to restore a remembered session)."""
    return fetch_one(connection, "SELECT * FROM users WHERE id = ?", (user_id,))


def create_user(connection: sqlite3.Connection, pseudo: str, email: str,
                password: str) -> int:
    """Insert a new user with a salted password hash and a PRIVATE profile.

    Returns the new user id. It does NOT validate the input:
    use register_account() for that. Raises DatabaseIntegrityError if the
    pseudo or the email already exists.
    """
    salt = generate_salt()
    password_hash = hash_password(password, salt)
    return execute_write(
        connection,
        "INSERT INTO users (pseudo, email, mot_de_passe_hache, sel, profil_public) "
        "VALUES (?, ?, ?, ?, ?)",
        (pseudo, email, password_hash, salt, NEW_ACCOUNT_PROFIL_PUBLIC),
    )


def register_account(connection: sqlite3.Connection, pseudo: str, email: str,
                     password: str, password_confirmation: str) -> list[str]:
    """Validate the form, check duplicates, then create the account.

    Returns a list of error messages. An EMPTY list means the account was
    created. The user is NOT logged in by this function.
    """
    error_messages = validate_registration_input(
        pseudo, email, password, password_confirmation
    )
    if error_messages:
        return error_messages

    clean_pseudo = pseudo.strip()
    clean_email = email.strip()

    if get_user_by_pseudo(connection, clean_pseudo) is not None:
        error_messages.append("This pseudo is already taken.")
    if get_user_by_email(connection, clean_email) is not None:
        error_messages.append("This email is already registered.")
    if error_messages:
        return error_messages

    try:
        create_user(connection, clean_pseudo, clean_email, password)
    except DatabaseIntegrityError:
        # Safety net: the UNIQUE constraints still protect us if two people
        # register the same pseudo at the same moment.
        return ["This pseudo or email is already in use."]
    return []


def authenticate_user(connection: sqlite3.Connection, pseudo: str, password: str):
    """Return the user row if pseudo and password are correct, otherwise None.

    The same result (None) is returned for an unknown pseudo and for a wrong
    password, so the caller cannot tell an attacker which one was wrong.
    """
    user_row = get_user_by_pseudo(connection, pseudo.strip())

    if user_row is None:
        # Hash anyway, so an unknown pseudo takes as long as a wrong password.
        hash_password(password, _UNUSED_SALT)
        return None

    if verify_password(password, user_row["sel"], user_row["mot_de_passe_hache"]):
        return user_row
    return None


# --------------------------------------------------------------------------
# Account changes (Increment 8 rectifications)
# --------------------------------------------------------------------------
# Every change asks for the CURRENT password, so someone who finds a session
# left open cannot take the account over. The user id comes from the session.

WRONG_CURRENT_PASSWORD_MESSAGE = "Your current password is incorrect."


def verify_user_password(connection: sqlite3.Connection, user_id: int, password: str) -> bool:
    """True if `password` is the current password of this user."""
    _check_user_id(user_id)
    user_row = fetch_one(connection, "SELECT sel, mot_de_passe_hache FROM users WHERE id = ?",
                         (user_id,))
    if user_row is None:
        return False
    return verify_password(password, user_row["sel"], user_row["mot_de_passe_hache"])


def change_pseudo(connection: sqlite3.Connection, user_id: int, current_password: str,
                  new_pseudo: str) -> list[str]:
    """Change the pseudo. Returns error messages (an EMPTY list = changed)."""
    if not verify_user_password(connection, user_id, current_password):
        return [WRONG_CURRENT_PASSWORD_MESSAGE]
    clean_pseudo = new_pseudo.strip()
    if not clean_pseudo:
        return ["Pseudo is required."]
    taken_by = fetch_one(connection, "SELECT id FROM users WHERE pseudo = ? AND id != ?",
                         (clean_pseudo, user_id))
    if taken_by is not None:
        return ["This pseudo is already taken."]
    try:
        execute_write(connection, "UPDATE users SET pseudo = ? WHERE id = ?",
                      (clean_pseudo, user_id))
    except DatabaseIntegrityError:
        return ["This pseudo is already taken."]
    return []


def change_email(connection: sqlite3.Connection, user_id: int, current_password: str,
                 new_email: str) -> list[str]:
    """Change the email. Returns error messages (an EMPTY list = changed)."""
    if not verify_user_password(connection, user_id, current_password):
        return [WRONG_CURRENT_PASSWORD_MESSAGE]
    clean_email = new_email.strip()
    if not clean_email:
        return ["Email is required."]
    if not EMAIL_PATTERN.fullmatch(clean_email):
        return ["Email address is not valid."]
    taken_by = fetch_one(connection, "SELECT id FROM users WHERE email = ? AND id != ?",
                         (clean_email, user_id))
    if taken_by is not None:
        return ["This email is already registered."]
    try:
        execute_write(connection, "UPDATE users SET email = ? WHERE id = ?",
                      (clean_email, user_id))
    except DatabaseIntegrityError:
        return ["This email is already registered."]
    return []


def change_password(connection: sqlite3.Connection, user_id: int, current_password: str,
                    new_password: str, new_password_confirmation: str) -> list[str]:
    """Change the password (new salt, new hash). Returns error messages (EMPTY = changed)."""
    if not verify_user_password(connection, user_id, current_password):
        return [WRONG_CURRENT_PASSWORD_MESSAGE]
    error_messages = validate_new_password(new_password)
    if not error_messages and new_password == current_password:
        error_messages.append("The new password must be different from the current one.")
    if not new_password_confirmation:
        error_messages.append("Password confirmation is required.")
    elif new_password != new_password_confirmation:
        error_messages.append("Passwords do not match.")
    if error_messages:
        return error_messages
    new_salt = generate_salt()
    execute_write(connection,
                  "UPDATE users SET mot_de_passe_hache = ?, sel = ? WHERE id = ?",
                  (hash_password(new_password, new_salt), new_salt, user_id))
    return []


# --------------------------------------------------------------------------
# Ratings, watchlist and favorites (Increment 4)
# --------------------------------------------------------------------------
# These functions only know a title by its id ("titre_id", the id column of
# data/titres.csv). Titles and posters are never stored in SQLite.
# Every function takes the user_id explicitly and filters on it, so one user
# can never read or change another user's ratings or lists.

MINIMUM_RATING = 1
MAXIMUM_RATING = 5


def _check_title_id(title_id) -> None:
    """A title id is a whole number of 1 or more (bool and text are refused)."""
    if isinstance(title_id, bool) or not isinstance(title_id, int) or title_id < 1:
        raise ValueError("A title id must be a whole number of 1 or more.")


def _check_rating(rating) -> None:
    """A rating is a whole number from 1 to 5 (bool, decimals and text are refused)."""
    if (isinstance(rating, bool) or not isinstance(rating, int)
            or not MINIMUM_RATING <= rating <= MAXIMUM_RATING):
        raise ValueError(
            f"Rating must be a whole number from {MINIMUM_RATING} to {MAXIMUM_RATING}."
        )


def set_rating(connection: sqlite3.Connection, user_id: int, title_id: int,
               rating: int) -> None:
    """Create the user's rating of a title, or change it if it already exists.

    A user has at most ONE rating per title: the primary key
    (user_id, titre_id) allows a single row, and ON CONFLICT turns a second
    rating into an update of that row.
    """
    _check_title_id(title_id)
    _check_rating(rating)
    execute_write(
        connection,
        "INSERT INTO ratings (user_id, titre_id, note) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, titre_id) DO UPDATE SET note = excluded.note",
        (user_id, title_id, rating),
    )


def get_user_rating(connection: sqlite3.Connection, user_id: int, title_id: int):
    """Return this user's rating (1-5) of the title, or None if not rated."""
    row = fetch_one(
        connection,
        "SELECT note FROM ratings WHERE user_id = ? AND titre_id = ?",
        (user_id, title_id),
    )
    return None if row is None else row["note"]


def remove_rating(connection: sqlite3.Connection, user_id: int, title_id: int) -> bool:
    """Delete the user's rating of a title. False if there was none.

    Watchlist, favourites and viewing history are not touched.
    """
    _check_user_id(user_id)
    _check_title_id(title_id)
    cursor = _run_query(connection,
                        "DELETE FROM ratings WHERE user_id = ? AND titre_id = ?",
                        (user_id, title_id))
    return cursor.rowcount == 1


def get_rating_counts(connection: sqlite3.Connection) -> dict[int, int]:
    """{title_id: number of Cinerecom users who rated it}, for the recommendation scoring.

    Only the NUMBER of votes is returned (never who voted, never the values).
    """
    rows = fetch_all(connection,
                     "SELECT titre_id, COUNT(*) AS rating_count FROM ratings GROUP BY titre_id")
    return {row["titre_id"]: row["rating_count"] for row in rows}


def get_average_rating(connection: sqlite3.Connection, title_id: int):
    """Return (average, number_of_ratings) given by ALL Cinerecom users.

    The average is None when nobody has rated the title yet.
    """
    row = fetch_one(
        connection,
        "SELECT AVG(note) AS average_rating, COUNT(*) AS rating_count "
        "FROM ratings WHERE titre_id = ?",
        (title_id,),
    )
    return row["average_rating"], row["rating_count"]


# Watchlist and favorites work the same way, so they share the code below.
# The SQL texts are fixed constants (a table name cannot be a "?" parameter);
# only the values are passed as parameters.
_LIST_QUERIES = {
    "watchlist": {
        "add": "INSERT OR IGNORE INTO watchlist (user_id, titre_id) VALUES (?, ?)",
        "remove": "DELETE FROM watchlist WHERE user_id = ? AND titre_id = ?",
        "exists": "SELECT 1 FROM watchlist WHERE user_id = ? AND titre_id = ?",
        "all": "SELECT titre_id FROM watchlist WHERE user_id = ? ORDER BY rowid DESC",
    },
    "favorites": {
        "add": "INSERT OR IGNORE INTO favorites (user_id, titre_id) VALUES (?, ?)",
        "remove": "DELETE FROM favorites WHERE user_id = ? AND titre_id = ?",
        "exists": "SELECT 1 FROM favorites WHERE user_id = ? AND titre_id = ?",
        "all": "SELECT titre_id FROM favorites WHERE user_id = ? ORDER BY rowid DESC",
    },
}


def _add_to_list(connection, list_name: str, user_id: int, title_id: int) -> bool:
    """True if the title was added, False if it was already in the list."""
    _check_title_id(title_id)
    cursor = _run_query(connection, _LIST_QUERIES[list_name]["add"], (user_id, title_id))
    return cursor.rowcount == 1


def _remove_from_list(connection, list_name: str, user_id: int, title_id: int) -> bool:
    """True if the title was removed, False if it was not in the list."""
    cursor = _run_query(connection, _LIST_QUERIES[list_name]["remove"], (user_id, title_id))
    return cursor.rowcount == 1


def _is_in_list(connection, list_name: str, user_id: int, title_id: int) -> bool:
    row = fetch_one(connection, _LIST_QUERIES[list_name]["exists"], (user_id, title_id))
    return row is not None


def add_to_watchlist(connection, user_id: int, title_id: int) -> bool:
    """Add a title to the user's watchlist. False if it was already there."""
    return _add_to_list(connection, "watchlist", user_id, title_id)


def remove_from_watchlist(connection, user_id: int, title_id: int) -> bool:
    """Remove a title from the user's watchlist. False if it was not there."""
    return _remove_from_list(connection, "watchlist", user_id, title_id)


def is_in_watchlist(connection, user_id: int, title_id: int) -> bool:
    return _is_in_list(connection, "watchlist", user_id, title_id)


def add_to_favorites(connection, user_id: int, title_id: int) -> bool:
    """Add a title to the user's favorites. False if it was already there."""
    return _add_to_list(connection, "favorites", user_id, title_id)


def remove_from_favorites(connection, user_id: int, title_id: int) -> bool:
    """Remove a title from the user's favorites. False if it was not there."""
    return _remove_from_list(connection, "favorites", user_id, title_id)


def is_in_favorites(connection, user_id: int, title_id: int) -> bool:
    return _is_in_list(connection, "favorites", user_id, title_id)


# --------------------------------------------------------------------------
# Viewing state: Watch / Finished / cancel (Increment 8)
# --------------------------------------------------------------------------
# Rules (enforced here and by the schema, not only by the interface):
# - only titles of the user's watchlist can be watched;
# - a user has at most ONE active title (currently_watching.user_id is the key);
# - "Finished" writes watch_history and clears the active title;
# - cancelling (the X button) clears the active title and writes NOTHING in
#   watch_history, so a cancelled title is never counted as watched;
# - a finished title cannot be started again (it is counted once);
# - ratings and favourites never touch these two tables.

WATCH_STARTED = "started"
WATCH_ALREADY_WATCHING = "already_watching"
WATCH_NOT_IN_WATCHLIST = "not_in_watchlist"
WATCH_ALREADY_FINISHED = "already_finished"


def get_currently_watching(connection: sqlite3.Connection, user_id: int):
    """Return the id of the title this user is watching now, or None."""
    _check_user_id(user_id)
    row = fetch_one(connection,
                    "SELECT titre_id FROM currently_watching WHERE user_id = ?", (user_id,))
    return None if row is None else row["titre_id"]


def is_finished(connection: sqlite3.Connection, user_id: int, title_id: int) -> bool:
    """True if the user marked this title as Finished."""
    row = fetch_one(connection,
                    "SELECT 1 FROM watch_history WHERE user_id = ? AND titre_id = ?",
                    (user_id, title_id))
    return row is not None


def get_user_watched(connection: sqlite3.Connection, user_id: int) -> list[int]:
    """Ids of the titles ONE user marked as Finished, newest first."""
    _check_user_id(user_id)
    rows = fetch_all(connection,
                     "SELECT titre_id FROM watch_history WHERE user_id = ? ORDER BY rowid DESC",
                     (user_id,))
    return [row["titre_id"] for row in rows]


def start_watching(connection: sqlite3.Connection, user_id: int, title_id: int) -> str:
    """Make a watchlist title the active one. Returns one of the WATCH_* codes."""
    _check_user_id(user_id)
    _check_title_id(title_id)
    if is_finished(connection, user_id, title_id):
        return WATCH_ALREADY_FINISHED
    if not _is_in_list(connection, "watchlist", user_id, title_id):
        return WATCH_NOT_IN_WATCHLIST
    if get_currently_watching(connection, user_id) is not None:
        return WATCH_ALREADY_WATCHING
    try:
        execute_write(connection,
                      "INSERT INTO currently_watching (user_id, titre_id) VALUES (?, ?)",
                      (user_id, title_id))
    except DatabaseIntegrityError:
        # Safety net (two tabs at once): the primary key refuses a second active title.
        return WATCH_ALREADY_WATCHING
    return WATCH_STARTED


def finish_watching(connection: sqlite3.Connection, user_id: int, title_id: int) -> bool:
    """Mark the ACTIVE title as watched and clear the active title.

    False if this title is not the user's active title. Both writes belong to
    the same transaction (database_connection commits or rolls back together).
    """
    _check_user_id(user_id)
    _check_title_id(title_id)
    if get_currently_watching(connection, user_id) != title_id:
        return False
    _run_query(connection, "DELETE FROM currently_watching WHERE user_id = ?", (user_id,))
    _run_query(connection,
               "INSERT OR IGNORE INTO watch_history (user_id, titre_id) VALUES (?, ?)",
               (user_id, title_id))
    return True


UNDO_REOPENED = "reopened"
UNDO_NOT_FINISHED = "not_finished"
UNDO_NOT_IN_WATCHLIST = "not_in_watchlist"
UNDO_ANOTHER_ACTIVE = "another_active"


def undo_finished(connection: sqlite3.Connection, user_id: int, title_id: int) -> str:
    """Undo a mistaken "Finished": the title is no longer watched and is active again.

    It goes back to the "Finished + X" state (currently_watching), exactly as
    if the user had pressed Watch. Refused when another title is active (one
    active title per user), when the title is not finished, or when it is no
    longer in the watchlist (add it again first). Returns one of the UNDO_* codes.
    """
    _check_user_id(user_id)
    _check_title_id(title_id)
    if not is_finished(connection, user_id, title_id):
        return UNDO_NOT_FINISHED
    if not _is_in_list(connection, "watchlist", user_id, title_id):
        return UNDO_NOT_IN_WATCHLIST
    if get_currently_watching(connection, user_id) is not None:
        return UNDO_ANOTHER_ACTIVE
    _run_query(connection, "DELETE FROM watch_history WHERE user_id = ? AND titre_id = ?",
               (user_id, title_id))
    _run_query(connection, "INSERT INTO currently_watching (user_id, titre_id) VALUES (?, ?)",
               (user_id, title_id))
    return UNDO_REOPENED


def cancel_watching(connection: sqlite3.Connection, user_id: int, title_id: int) -> bool:
    """Cancel the viewing session (the X button). Nothing is added to the history.

    False if this title is not the user's active title.
    """
    _check_user_id(user_id)
    _check_title_id(title_id)
    cursor = _run_query(connection,
                        "DELETE FROM currently_watching WHERE user_id = ? AND titre_id = ?",
                        (user_id, title_id))
    return cursor.rowcount == 1


# --------------------------------------------------------------------------
# Personal space and profiles (Increment 5)
# --------------------------------------------------------------------------
# Privacy rules (cahier, section 2.4 and BNF-S4), enforced HERE and not only
# in the interface:
# - a user reads their own ratings / watchlist / favorites by USER ID;
# - the watchlist and the email are NEVER part of what another user can see;
# - a private profile can only be found or opened by its owner.
# get_profile_for_viewer() is the single door for viewing a profile and
# search_public_profiles() is the single door for finding one.
# Lists are returned newest first (rowid order: a row keeps its rowid when a
# rating is modified, and a removed then re-added title gets a new one).

MAXIMUM_SEARCH_RESULTS = 50   # profiles returned by one search


def _check_user_id(user_id) -> None:
    """A user id is a whole number of 1 or more (bool and text are refused)."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("A user id must be a whole number of 1 or more.")


def _is_valid_id(value) -> bool:
    """True for a usable id. Used by the permission checks, which answer
    "not allowed" for a bad id instead of raising an error."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def get_user_ratings(connection: sqlite3.Connection, user_id: int) -> list[tuple[int, int]]:
    """All ratings of ONE user as [(title_id, rating), ...], newest first."""
    _check_user_id(user_id)
    rows = fetch_all(
        connection,
        "SELECT titre_id, note FROM ratings WHERE user_id = ? ORDER BY rowid DESC",
        (user_id,),
    )
    return [(row["titre_id"], row["note"]) for row in rows]


def _get_list_title_ids(connection, list_name: str, user_id: int) -> list[int]:
    _check_user_id(user_id)
    rows = fetch_all(connection, _LIST_QUERIES[list_name]["all"], (user_id,))
    return [row["titre_id"] for row in rows]


def get_user_watchlist(connection, user_id: int) -> list[int]:
    """Title ids of ONE user's watchlist, newest first."""
    return _get_list_title_ids(connection, "watchlist", user_id)


def get_user_favorites(connection, user_id: int) -> list[int]:
    """Title ids of ONE user's favorites, newest first."""
    return _get_list_title_ids(connection, "favorites", user_id)


def is_profile_public(connection: sqlite3.Connection, user_id: int) -> bool:
    """True if this user's profile is public. Unknown users count as not public."""
    row = fetch_one(connection, "SELECT profil_public FROM users WHERE id = ?", (user_id,))
    return row is not None and row["profil_public"] == 1


def set_profile_visibility(connection: sqlite3.Connection, user_id: int,
                           is_public: bool) -> bool:
    """Make a user's own profile public (True) or private (False).

    Returns True if the user exists (the change was saved), False otherwise.
    The caller must pass the id of the SIGNED-IN user, never an id typed by
    someone: the interface takes it from the session.
    """
    _check_user_id(user_id)
    if not isinstance(is_public, bool):
        raise ValueError("Profile visibility must be True (public) or False (private).")
    cursor = _run_query(
        connection,
        "UPDATE users SET profil_public = ? WHERE id = ?",
        (1 if is_public else 0, user_id),
    )
    return cursor.rowcount == 1


def _user_exists(connection: sqlite3.Connection, user_id) -> bool:
    if not _is_valid_id(user_id):
        return False
    return fetch_one(connection, "SELECT 1 FROM users WHERE id = ?", (user_id,)) is not None


def search_public_profiles(connection: sqlite3.Connection, viewer_id,
                           search_text: str = "") -> list[tuple[int, str]]:
    """Find PUBLIC profiles whose pseudo contains `search_text`.

    Returns [(user_id, pseudo), ...] sorted by pseudo, at most
    MAXIMUM_SEARCH_RESULTS. An empty search lists public profiles.
    - Only a signed-in (existing) viewer gets results: otherwise [].
    - Private profiles are never returned, and neither is the viewer
      (people use their own space for that). Only the id and the pseudo
      leave this function: never the email.
    """
    if not _user_exists(connection, viewer_id):
        return []
    # "%" and "_" typed by the user must be searched literally, not as wildcards.
    escaped_text = (str(search_text).strip()
                    .replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
    rows = fetch_all(
        connection,
        "SELECT id, pseudo FROM users "
        "WHERE profil_public = 1 AND id != ? AND pseudo LIKE ? ESCAPE '\\' "
        "ORDER BY pseudo COLLATE NOCASE LIMIT ?",
        (viewer_id, f"%{escaped_text}%", MAXIMUM_SEARCH_RESULTS),
    )
    return [(row["id"], row["pseudo"]) for row in rows]


def set_favorites_visibility(connection: sqlite3.Connection, user_id: int,
                             is_public: bool) -> bool:
    """Show (True) or hide (False) the user's favourites on their PUBLIC profile.

    Independent from the profile visibility. Returns True if the user exists.
    """
    _check_user_id(user_id)
    if not isinstance(is_public, bool):
        raise ValueError("Favourites visibility must be True (public) or False (private).")
    cursor = _run_query(
        connection,
        "UPDATE users SET favorites_public = ? WHERE id = ?",
        (1 if is_public else 0, user_id),
    )
    return cursor.rowcount == 1


def is_favorites_public(connection: sqlite3.Connection, user_id: int) -> bool:
    """True if this user shares their favourites on their public profile."""
    row = fetch_one(connection, "SELECT favorites_public FROM users WHERE id = ?", (user_id,))
    return row is not None and row["favorites_public"] == 1


# The two title types of the catalogue (values of the CSV "type" column).
TITLE_TYPE_MOVIE = "movie"
TITLE_TYPE_TV_SHOW = "tv show"
TOP_RATED_PER_TYPE = 3


def select_top_rated_titles(user_ratings: list[tuple[int, int]], title_info: dict,
                            per_type: int = TOP_RATED_PER_TYPE) -> dict:
    """The user's best-rated titles, separately for movies and TV shows.

    user_ratings: [(title_id, rating), ...]
    title_info:   {title_id: (type, catalogue_rating)} built from the CSV.
    Sorted by the USER's rating (highest first); equal ratings are ordered by
    the catalogue rating, then by id. A title missing from title_info is skipped.
    Returns {"movie": [(id, rating), ...], "tv show": [...]}, at most per_type each.
    """
    top_rated = {TITLE_TYPE_MOVIE: [], TITLE_TYPE_TV_SHOW: []}
    ranked_ratings = sorted(
        user_ratings,
        key=lambda pair: (-pair[1], -(title_info.get(pair[0], ("", 0.0))[1]), pair[0]),
    )
    for title_id, rating in ranked_ratings:
        info = title_info.get(title_id)
        if info is None:
            continue
        title_type = info[0]
        if title_type in top_rated and len(top_rated[title_type]) < per_type:
            top_rated[title_type].append((title_id, rating))
    return top_rated


def get_profile_for_viewer(connection: sqlite3.Connection, viewer_id,
                           profile_user_id, title_info: dict | None = None):
    """The ONLY way to read a profile: returns what `viewer_id` may see, or None.

    None means "not available" and is the same answer for an unknown id, a
    private profile, a visitor (viewer_id None) or an invalid id, so the
    answer never reveals whether a private profile exists.
    Allowed: the owner (always, even if private) and any signed-in user when
    the profile is public.

    title_info ({id: (type, catalogue_rating)}) comes from the CSV: it lets
    this function choose the top-rated movies and TV shows here, so the full
    list of ratings never has to leave it.

    The result has: pseudo, summary inputs (watched ids, top
    three movies and top three TV shows), the title being watched, and the
    favourites ONLY when the owner looks at them or has made them public
    (otherwise "favorites" is None). It NEVER contains the full ratings list,
    the watchlist or the email, even for the owner: the owner reads those
    through get_user_ratings() / get_user_watchlist() / the session.
    """
    if not _user_exists(connection, viewer_id) or not _is_valid_id(profile_user_id):
        return None
    profile_row = fetch_one(
        connection,
        "SELECT id, pseudo, profil_public, favorites_public FROM users WHERE id = ?",
        (profile_user_id,),
    )
    if profile_row is None:
        return None
    is_own_profile = viewer_id == profile_user_id
    if not is_own_profile and profile_row["profil_public"] != 1:
        return None

    favorites_are_shared = is_own_profile or profile_row["favorites_public"] == 1
    return {
        "id": profile_row["id"],
        "pseudo": profile_row["pseudo"],
        "is_own": is_own_profile,
        "is_public": profile_row["profil_public"] == 1,
        "favorites_public": profile_row["favorites_public"] == 1,
        "watched_ids": get_user_watched(connection, profile_user_id),
        "top_rated": select_top_rated_titles(
            get_user_ratings(connection, profile_user_id), title_info or {}),
        "currently_watching": get_currently_watching(connection, profile_user_id),
        "favorites": (get_user_favorites(connection, profile_user_id)
                      if favorites_are_shared else None),
    }


# --------------------------------------------------------------------------
# Manual self-check:  python database.py
# --------------------------------------------------------------------------

def _expect_rejected(description: str, action) -> None:
    """Check that `action` is refused by a database constraint."""
    try:
        action()
    except DatabaseIntegrityError:
        print(f"[OK] Rejected as expected: {description}")
    else:
        raise AssertionError(f"NOT rejected (constraint missing): {description}")


def _run_self_check() -> None:
    # 1) Create the real database file and list its tables.
    initialize_database()
    initialize_database()  # second call must not fail or erase anything
    with database_connection() as connection:
        table_names = get_table_names(connection)
    print(f"[OK] {DATABASE_PATH.name} initialized, tables: {table_names}")
    assert table_names == ["currently_watching", "favorites", "ratings", "users",
                           "watch_history", "watchlist"]

    # 2) Test the constraints on a throw-away in-memory database, so the real
    #    file stays empty. The hash and salt below are fake test values only.
    connection = create_connection(":memory:")
    create_tables(connection)

    user_id = execute_write(
        connection,
        "INSERT INTO users (pseudo, email, mot_de_passe_hache, sel) VALUES (?, ?, ?, ?)",
        ("alice", "alice@example.com", "fake_hash", "fake_salt"),
    )
    user_row = fetch_one(connection, "SELECT * FROM users WHERE id = ?", (user_id,))
    assert user_row["profil_public"] == 0
    print("[OK] Valid user inserted (profil_public defaults to 0 = private)")

    insert_user = ("INSERT INTO users (pseudo, email, mot_de_passe_hache, sel) "
                   "VALUES (?, ?, ?, ?)")
    _expect_rejected("duplicate pseudo (different case)", lambda: execute_write(
        connection, insert_user, ("ALICE", "other@example.com", "h", "s")))
    _expect_rejected("duplicate email", lambda: execute_write(
        connection, insert_user, ("bob", "alice@example.com", "h", "s")))
    _expect_rejected("empty pseudo", lambda: execute_write(
        connection, insert_user, ("   ", "bob@example.com", "h", "s")))
    _expect_rejected("profil_public = 5", lambda: execute_write(
        connection,
        "INSERT INTO users (pseudo, email, mot_de_passe_hache, sel, profil_public) "
        "VALUES (?, ?, ?, ?, ?)", ("bob", "bob@example.com", "h", "s", 5)))

    insert_rating = "INSERT INTO ratings (user_id, titre_id, note) VALUES (?, ?, ?)"
    execute_write(connection, insert_rating, (user_id, 1, 5))
    print("[OK] Valid rating inserted")
    _expect_rejected("rating = 0", lambda: execute_write(connection, insert_rating, (user_id, 2, 0)))
    _expect_rejected("rating = 6", lambda: execute_write(connection, insert_rating, (user_id, 2, 6)))
    _expect_rejected("rating = 3.5", lambda: execute_write(connection, insert_rating, (user_id, 2, 3.5)))
    _expect_rejected("second rating for the same title", lambda: execute_write(
        connection, insert_rating, (user_id, 1, 4)))
    _expect_rejected("rating for a user that does not exist", lambda: execute_write(
        connection, insert_rating, (999, 1, 3)))

    for table_name in ("watchlist", "favorites"):
        insert_entry = f"INSERT INTO {table_name} (user_id, titre_id) VALUES (?, ?)"  # fixed names, no user input
        execute_write(connection, insert_entry, (user_id, 7))
        print(f"[OK] Valid {table_name} entry inserted")
        _expect_rejected(f"same title twice in {table_name}", lambda: execute_write(
            connection, insert_entry, (user_id, 7)))

    # Deleting a user removes their ratings, watchlist and favorites.
    execute_write(connection, "DELETE FROM users WHERE id = ?", (user_id,))
    for table_name in ("ratings", "watchlist", "favorites"):
        remaining_rows = fetch_all(connection, f"SELECT * FROM {table_name}")  # fixed names
        assert remaining_rows == [], f"{table_name} not cleaned after user deletion"
    print("[OK] Deleting a user also deletes their ratings, watchlist and favorites")

    # A bad call is reported as DatabaseError, not as a crash.
    try:
        fetch_all(connection, "SELECT * FROM table_that_does_not_exist")
    except DatabaseError as error:
        print(f"[OK] SQL error handled: {error}")
    try:
        fetch_one(connection, "SELECT * FROM users WHERE pseudo = ?", "alice")
    except DatabaseError as error:
        print(f"[OK] Wrong parameter format handled: {error}")

    connection.close()
    print("\nAll database checks passed.")


if __name__ == "__main__":
    _run_self_check()
