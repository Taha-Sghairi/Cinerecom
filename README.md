# Cinerecom

Cinerecom is a personalised movie and TV show discovery and tracking platform.
It helps users discover content through personalised recommendations while keeping track of their favourites, ratings, and viewing interests in their own personal space.

## Features

- **User accounts:** Create an account and sign in.
- **Movie and TV catalogue:** Browse and explore available titles.
- **Search:** Find titles in the catalogue.
- **Title details:** View information about individual movies and TV shows.
- **Personalised recommendations:** Receive recommendations based on your preferences and activity.
- **Similar titles:** Discover other titles related to the one you are viewing.
- **Favourites and ratings:** Save favourite titles and rate content.
- **My Space:** Access your personal information, favourites and ratings.
- **Random Mode:** Discover titles through random selection.

## Technologies

- **Python** – Main programming language.
- **Streamlit** – Web application framework.
- **SQLite** – Database for user accounts and interactions.
- **Pandas** – Data handling and catalogue processing.

## Project Structure

```text
Cinerecom/
├── .streamlit/
│   └── config.toml
├── data/
│   ├── titres.csv
│   ├── themes.csv
│   └── similarity_packs.csv
├── app.py
├── catalogue.py
├── database.py
├── recommandation.py
├── requirements.txt
└── README.md
```

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/Taha-Sghairi/Cinerecom.git
cd Cinerecom
```

### 2. Install dependencies

Make sure Python is installed, then run:

```bash
python -m pip install -r requirements.txt
```

### 3. Launch the application

```bash
streamlit run app.py
```

The application will open in your browser. If it does not open automatically, visit:

```text
http://localhost:8501
```

## Data and Database

The application uses CSV files stored in the `data/` directory for its catalogue and recommendation data.

SQLite is used to store user-related information. The database is created locally when the application initializes, so each local installation maintains its own user data.

## Running Locally

Cinerecom is designed to run locally using Streamlit. Each person running their own copy of the repository will have their own local database and accounts.
