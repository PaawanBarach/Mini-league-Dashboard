# app.py
import time
import sqlite3
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="FPL Mini‑league Forfeits", layout="wide")

API = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FPL-Forfeits/1.3)"}

# ------------- Defaults -------------
# Set your default league here. It only changes when you submit the form.
DEFAULT_LEAGUE_ID = 1415574 

# ------------- Persistence (SQLite) -------------
@st.cache_resource
def get_db():
    conn = sqlite3.connect("forfeits.db", check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forfeits (
            league_id INTEGER NOT NULL,
            entry     INTEGER NOT NULL,
            forfeits  TEXT,
            PRIMARY KEY (league_id, entry)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS overrides (
            league_id INTEGER NOT NULL,
            event     INTEGER NOT NULL,
            action    TEXT NOT NULL CHECK(action IN ('none','skip','eject')),
            note      TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (league_id, event)
        )
    """)
    conn.commit()
    return conn

def load_forfeits(conn, league_id: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT entry, forfeits FROM forfeits WHERE league_id = ?",
        conn, params=(league_id,)
    )
    if df.empty:
        df = pd.DataFrame(columns=["entry","forfeits"])
    return df

def save_forfeits(conn, league_id: int, df: pd.DataFrame):
    if df.empty:
        return
    rows = [(int(league_id), int(r.entry), str(r.forfeits)) for r in df.itertuples(index=False)]
    conn.executemany(
        "INSERT OR REPLACE INTO forfeits (league_id, entry, forfeits) VALUES (?, ?, ?)",
        rows
    )
    conn.commit()

def load_overrides(conn, league_id: int) -> dict:
    df = pd.read_sql_query(
        "SELECT event, action, note FROM overrides WHERE league_id = ?",
        conn, params=(league_id,)
    )
    return {int(r.event): {"action": r.action, "note": (r.note or "")}
            for r in df.itertuples(index=False)}

def set_override(conn, league_id: int, event: int, action: str, note: str = ""):
    conn.execute(
        "INSERT OR REPLACE INTO overrides (league_id, event, action, note) VALUES (?, ?, ?, ?)",
        (league_id, event, action, note)
    )
    conn.commit()

def clear_override(conn, league_id: int, event: int):
    conn.execute(
        "DELETE FROM overrides WHERE league_id = ? AND event = ?",
        (league_id, event)
    )
    conn.commit()

# ------------- FPL fetch (cached) -------------
@st.cache_data(ttl=300)
def fetch_classic_league(league_id: int, page: int = 1):
    url = f"{API}/leagues-classic/{league_id}/standings/?page_standings={page}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=300)
def fetch_entry_history(entry_id: int):
    url = f"{API}/entry/{entry_id}/history/"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()

def get_all_entries(league_id: int):
    entries = []
    page = 1
    league_info = None
    while True:
        data = fetch_classic_league(league_id, page=page)
        if league_info is None:
            league_info = data.get("league", {})
        for r in data["standings"]["results"]:
            entries.append({
                "entry": r["entry"],
                "entry_name": r["entry_name"],
                "player_name": r["player_name"]
            })
        if not data["standings"]["has_next"]:
            break
        page += 1
        time.sleep(0.2)
    return pd.DataFrame(entries), league_info

def build_gw_points(entries_df: pd.DataFrame):
    rows = []
    for entry_id in entries_df["entry"]:
        hist = fetch_entry_history(entry_id)
        for c in hist.get("current", []):
            rows.append({
                "entry": entry_id,
                "event": c["event"],            # GW number
                "gw_points": c["points"],       # single-GW points
                "total_points": c["total_points"]  # cumulative
            })
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["entry","event","gw_points","total_points"])

# ------------- Core logic -------------
def compute_last_by_gw(gw_points_df: pd.DataFrame, gw_from: int, gw_to: int, overrides: dict):
    """
    Returns DataFrame with columns:
      event, min_points, last_entries (list[int]), reason (None|'skipped'|'ejected'|'no-data')
    """
    out = []
    for gw in range(gw_from, gw_to + 1):
        sub = gw_points_df[gw_points_df["event"] == gw]
        if sub.empty:
            out.append({"event": gw, "min_points": None, "last_entries": [], "reason": "no-data"})
            continue
        min_pts = sub["gw_points"].min()
        last_entries = sub[sub["gw_points"] == min_pts]["entry"].tolist()

        # Apply override
        reason = None
        ov = overrides.get(gw)
        if ov:
            act = ov["action"]
            if act == "eject":
                last_entries, reason = [], "ejected"
            elif act == "skip":
                last_entries, reason = [], "skipped"
            else:
                reason = None  # 'none' means normal forfeits

        out.append({"event": gw, "min_points": min_pts, "last_entries": last_entries, "reason": reason})
    return pd.DataFrame(out)

def merge_overview(entries_df, last_df, forfeits_df):
    # Count only GWs that resulted in forfeits (exclude skipped/ejected/no-data)
    last_expanded = []
    for _, row in last_df.iterrows():
        if row.get("reason") in ("ejected", "skipped", "no-data"):
            continue
        for e in row["last_entries"]:
            last_expanded.append({"entry": e, "event": row["event"]})
    last_map = pd.DataFrame(last_expanded) if last_expanded else pd.DataFrame(columns=["entry","event"])
    times_last = last_map.groupby("entry")["event"].count().rename("times_last")
    last_gws = last_map.groupby("entry")["event"].apply(lambda s: sorted(s.tolist())).rename("last_gws")

    ov = entries_df.merge(times_last, on="entry", how="left").merge(last_gws, on="entry", how="left")
    ov["times_last"] = ov["times_last"].fillna(0).astype(int)
    ov["last_gws"] = ov["last_gws"].apply(lambda x: x if isinstance(x, list) else [])

    if forfeits_df is not None and not forfeits_df.empty:
        ov = ov.merge(forfeits_df, on="entry", how="left")
    else:
        ov["forfeits"] = ""
    return ov

# ------------- Sidebar: league + overrides (submit-only forms) -------------
st.sidebar.header("Config")

# League ID form: default value, only changes on submit
with st.sidebar.form("league_form", clear_on_submit=False):
    lid_default = str(st.session_state.get("league_id", DEFAULT_LEAGUE_ID))
    lid_str = st.text_input("Mini‑league ID", value=lid_default, help="Digits only", max_chars=10)
    if st.form_submit_button("Load league"):
        if lid_str.isdigit():
            st.session_state.league_id = int(lid_str)
        else:
            st.warning("League ID must be digits.")
            st.stop()

if "league_id" not in st.session_state:
    st.session_state.league_id = DEFAULT_LEAGUE_ID

league_id = st.session_state.league_id

# ------------- Load data -------------
entries_df, league_info = get_all_entries(league_id)
league_title = str(league_info.get("name") or f"League {league_id}")
if entries_df.empty:
    st.warning("No entries found for this league.")
    st.stop()

gw_points_df = build_gw_points(entries_df)
if gw_points_df.empty:
    st.warning("No gameweek data yet.")
    st.stop()

start_event = int(league_info.get("start_event", max(1, gw_points_df["event"].min())))
latest_event = int(gw_points_df["event"].max())
gw_options = list(range(start_event, latest_event + 1))

# ------------- DB state -------------
db = get_db()
forfeits_df = load_forfeits(db, league_id)
overrides = load_overrides(db, league_id)

# ------------- UI -------------
st.title(f"{league_title} Forfeits")

# Overrides form: select a GW and mark None/Skip/Eject with optional note
st.sidebar.subheader("GW overrides")
with st.sidebar.form("override_form", clear_on_submit=False):
    ov_gw = st.selectbox("Gameweek", options=gw_options, index=len(gw_options)-1)
    ov_action = st.radio("Action", ["None", "Skip", "Eject"], horizontal=True)
    ov_note = st.text_input("Note (optional)")
    if st.form_submit_button("Save/Update override"):
        if ov_action == "None":
            set_override(db, league_id, int(ov_gw), "none", ov_note)
        elif ov_action == "Skip":
            set_override(db, league_id, int(ov_gw), "skip", ov_note)
        else:
            set_override(db, league_id, int(ov_gw), "eject", ov_note)
        overrides = load_overrides(db, league_id)
        st.success("Override saved")

# Compute last-by-GW with overrides applied
last_df = compute_last_by_gw(gw_points_df, start_event, latest_event, overrides)

tabs = st.tabs(["Overview", "Gameweek", "Chronology"])

# Overview tab: editable forfeits notes, persisted to DB
with tabs[0]:
    st.caption("Edit forfeits; click Save to persist to the database.")
    ov = merge_overview(entries_df, last_df, forfeits_df)
    shown = ov[["entry","entry_name","player_name","times_last","last_gws","forfeits"]].sort_values(
        ["times_last","entry_name"], ascending=[False, True]
    ).reset_index(drop=True)
    edited = st.data_editor(
        shown,
        key="forfeits_editor",
        num_rows="fixed",
        column_config={
            "last_gws": st.column_config.ListColumn("Last GWs", width="small"),
            "forfeits": st.column_config.TextColumn("Forfeits (notes)", width="large"),
        },
        use_container_width=True,
    )
    if st.button("Save forfeits"):
        to_save = edited[["entry","forfeits"]].copy()
        save_forfeits(db, league_id, to_save)
        forfeits_df = load_forfeits(db, league_id)
        st.success("Saved")

# Gameweek tab: dropdown + asc/desc in a form to avoid reruns on change
with tabs[1]:
    with st.form("snapshot_form", clear_on_submit=False):
        col1, col2 = st.columns([1,1])
        with col1:
            gw = st.selectbox("Gameweek", options=gw_options, index=len(gw_options)-1)
        with col2:
            order = st.radio("Sort order", ["Descending (highest first)", "Ascending (lowest first)"],
                             index=0, horizontal=True)
        st.form_submit_button("Apply")

    ascending = order.startswith("Ascending")
    sub = gw_points_df[gw_points_df["event"] == gw].merge(entries_df, on="entry", how="left")
    sub = sub.sort_values(["gw_points","entry_name"], ascending=[ascending, True])
    st.dataframe(sub[["entry_name","player_name","gw_points"]], use_container_width=True)

# Chronology tab: GWx – names or None with reason
with tabs[2]:
    label_map = {row.entry: f'{row.entry_name} ({row.player_name})' for row in entries_df.itertuples()}
    rows = []
    for _, r in last_df.sort_values("event").iterrows():
        if r["reason"] == "ejected":
            tag = "None (ejected)"
        elif r["reason"] == "skipped":
            tag = "None (skipped)"
        elif r["reason"] == "no-data":
            tag = "None"
        elif not r["last_entries"]:
            tag = "None"
        else:
            names = [label_map.get(e, str(e)) for e in r["last_entries"]]
            tag = ", ".join(names)
        rows.append({"GW": int(r["event"]), "Last": tag})
    chrono = pd.DataFrame(rows)
    st.dataframe(chrono, use_container_width=True)
