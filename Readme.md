# FPL Mini-league Forfeits

Simple Streamlit app to:
- List all entries in a mini-league
- Show per-GW snapshot (asc/desc)
- Track “last place” chronology with GW overrides (None, Skip, Eject)
- Persist manual forfeit notes

## Requirements
- Python 3.12+ recommended
- Files: app.py, requirements.txt

## Setup (local)
1) python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
2) pip install -r requirements.txt
3) streamlit run app.py

## Configure
- DEFAULT_LEAGUE_ID in app.py (sidebar form only updates it on Submit)
- GW overrides in sidebar: choose Gameweek, select None/Skip/Eject, optional note, Save

## Deploy (Streamlit Community Cloud)
- Connect GitHub account
- Create app: pick repo, branch, and app.py as entrypoint
- Optional: set Python version and paste secrets if using external DB
