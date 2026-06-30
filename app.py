import streamlit as st
import snowflake.connector
import pandas as pd
import pytz
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="Market Availability", layout="wide")

# ── League config ────────────────────────────────────────────────────────────

LEAGUES = {
    "WNBA":  {"league_id": "94682", "sport_id": "2"},
    "MLB":   {"league_id": "84240", "sport_id": "7"},
}

MLB_EXCLUDED_MARKETS = {
    "Plate Appearance Pitch Count O/U",
    "Plate Appearance Result Exact",
    "Pitch Speed - Will the Next Pitch Be X MPH or Faster?",
    "Either Pitcher Strikeouts Thrown",
    "Either Batter Singles",
    "Either Batter Triples",
    "Combined Batter Total Bases",
    "Combined Batter Hits",
    "Combined Batter Stolen Bases",
    "Combined Batter RBIs",
    "Combined Batter Home Runs",
    "Most Home Runs H2H",
    "Win Probability",
    "Starting Pitcher Race to 3 Strikeouts",
    "1st Strikeout Thrown (H2H)",
    "1st Earned Run Allowed (H2H)",
    "Combined Pitcher Hits Allowed (X or Fewer)",
    "Combined Pitcher Earned Runs Allowed (X or Fewer)",
    "Combined Pitcher Strikeouts Thrown",
    "Either Pitcher Hits Allowed (X or Fewer)",
    "Either Pitcher Earned Runs Allowed (X or Fewer)",
    "Walks Allowed (X or Fewer)",
    "Hits Allowed + Walks Allowed + Earned Runs Allowed (X or Fewer)",
    "Outs O/U",
}

WNBA_EXCLUDED_MARKETS = {
    "Player Next Field Goal Type",
    "Player First Field Goal Made Type",
}

# ── Market classification ────────────────────────────────────────────────────

def classify_market(name: str) -> str:
    n = name.lower()
    if "2nd half" in n or "- 2h" in n:
        return "Exclude"
    if "team first" in n:
        return "Team"
    if "h2h" in n or n.startswith("most "):
        return "H2H"
    if "milestone" in n:
        return "Milestones"
    if (
        " o/u" in n
        or "double-double" in n
        or "triple-double" in n
        or name == "1st Points Scorer"
        or "1st points scorer" in n
        or "first field goal scorer" in n
        or "first field goal made type" in n
        or "player first field goal made type" in n
        or "1st hit" in n
        or "1st batter to strike out" in n
        or "1st stolen base" in n
    ):
        return "Balanced"
    return "Other"

GROUP_ORDER    = ["Balanced", "Milestones", "Team", "H2H", "Other"]

# Canonical stat ordering for WNBA market completion table.
# Applied across Balanced, Milestones, and H2H — suffix (O/U, Milestones, H2H …) is stripped for matching.
WNBA_MARKET_STAT_ORDER = [
    "Points",
    "Rebounds",
    "Assists",
    "Three Pointers Made",
    "Points + Rebounds",
    "Points + Assists",
    "Rebounds + Assists",
    "Points + Rebounds + Assists",
    "Double-Double",
    "Triple-Double",
    "1st Points Scorer",
]

# Suffixes to strip before matching the stat base name
_MARKET_SUFFIXES = [
    " O/U", " Milestones",
    " H2H ML", " H2H Spread", " H2H Total",
]

def _wnba_market_sort_key(market_name: str) -> int:
    base = market_name
    # Strip trailing suffix (O/U, Milestones, H2H variants)
    for suffix in _MARKET_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    # Strip leading "Most " for H2H / most-markets (e.g. "Most Points H2H ML" → "Points")
    if base.startswith("Most "):
        base = base[5:]
    # "Three Pointers" (from "Most Three Pointers") → "Three Pointers Made"
    if base == "Three Pointers":
        base = "Three Pointers Made"
    try:
        return WNBA_MARKET_STAT_ORDER.index(base)
    except ValueError:
        return 99
DEFAULT_GROUPS = ["Balanced", "Milestones"]

def pct_color(pct: float) -> str:
    if pct >= 0.9: return "#16a34a"
    if pct >= 0.6: return "#ca8a04"
    return "#dc2626"

def fmt_countdown(start_pt, now_pt):
    total_secs = int((start_pt - now_pt).total_seconds())
    if total_secs <= 0:
        return "LIVE"
    h, rem = divmod(total_secs, 3600)
    m = rem // 60
    if h >= 24:
        d = h // 24
        return f"{d}d {h%24}h"
    return f"{h}h {m}m"

# ── Snowflake ────────────────────────────────────────────────────────────────

def _new_connection():
    return snowflake.connector.connect(
        account="DRAFTKINGS-DRAFTKINGS",
        user="T.GRAVES",
        token=st.secrets["snowflake"]["pat"],
        authenticator="programmatic_access_token",
        warehouse="QUERY_WH",
        login_timeout=15,
        network_timeout=60,
    )

@st.cache_resource
def get_connection():
    return _new_connection()

def run_query(sql: str) -> pd.DataFrame:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])
    except Exception:
        # Connection expired — clear the cache and reconnect once
        get_connection.clear()
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(sql)
        return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])

# ── Data queries ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def get_events(sport_id: str) -> pd.DataFrame:
    return run_query(f"""
        SELECT DISTINCT e.EVENTID, e.EVENTNAME, e.LEAGUEID, e.LEAGUENAME, e.STARTEVENTDATE
        FROM SPORTSCONTENT.DBO.EVENTS e
        WHERE e.SPORTID = '{sport_id}'
          AND e.STARTEVENTDATE >= CURRENT_TIMESTAMP
          AND EXISTS (SELECT 1 FROM SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp WHERE mp.EVENTID = e.EVENTID)
        ORDER BY e.STARTEVENTDATE
    """)

@st.cache_data(ttl=30, show_spinner=False)
def get_overview_stats(event_ids: tuple) -> pd.DataFrame:
    ids_sql = ",".join(f"'{e}'" for e in event_ids)
    return run_query(f"""
        SELECT mp.EVENTID, m.MARKETTYPENAME, COUNT(DISTINCT mp.PLAYERNAME) AS LIVE_PLAYERS
        FROM SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp
        JOIN SPORTSCONTENT.DBO.MARKETS m ON m.MARKETID = mp.MARKETID AND m.EVENTID = mp.EVENTID
        WHERE mp.EVENTID IN ({ids_sql})
          AND m.ISREMOVED = FALSE
        GROUP BY mp.EVENTID, m.MARKETTYPENAME
    """)

@st.cache_data(ttl=300, show_spinner=False)
def get_overview_baselines(event_ids: tuple, league_id: str) -> pd.DataFrame:
    return run_query(f"""
        WITH recent_events AS (
            SELECT DISTINCT e.EVENTID
            FROM SPORTSCONTENT.DBO.EVENTS e
            WHERE e.LEAGUEID = '{league_id}'
              AND e.STARTEVENTDATE < CURRENT_TIMESTAMP
              AND e.STARTEVENTDATE >= CURRENT_TIMESTAMP - INTERVAL '30 days'
              AND EXISTS (SELECT 1 FROM SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp WHERE mp.EVENTID = e.EVENTID)
            LIMIT 5
        ),
        prop_data AS (
            SELECT DISTINCT mp.PLAYERNAME, m.MARKETTYPENAME, m.EVENTID
            FROM recent_events re
            JOIN SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp ON mp.EVENTID = re.EVENTID
            JOIN SPORTSCONTENT.DBO.MARKETS m ON m.MARKETID = mp.MARKETID AND m.EVENTID = re.EVENTID
        )
        SELECT MARKETTYPENAME, COUNT(DISTINCT EVENTID) AS EVENTS_PRESENT
        FROM prop_data
        GROUP BY MARKETTYPENAME
        HAVING COUNT(DISTINCT EVENTID) >= 4
    """)

@st.cache_data(ttl=1800, show_spinner=False)
def get_player_baselines(event_id: str) -> pd.DataFrame:
    # Use 60-day window to catch pitchers who start every 5 days
    return run_query(f"""
        WITH event_players AS (
            SELECT DISTINCT PLAYERNAME
            FROM SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL
            WHERE EVENTID = '{event_id}'
        ),
        recent_player_events AS (
            SELECT ep.PLAYERNAME, mp.EVENTID, e.STARTEVENTDATE
            FROM event_players ep
            JOIN SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp ON mp.PLAYERNAME = ep.PLAYERNAME
            JOIN SPORTSCONTENT.DBO.EVENTS e ON e.EVENTID = mp.EVENTID
            WHERE e.STARTEVENTDATE >= CURRENT_TIMESTAMP - INTERVAL '60 days'
              AND e.STARTEVENTDATE < CURRENT_TIMESTAMP
              AND mp.EVENTID != '{event_id}'
            QUALIFY DENSE_RANK() OVER (PARTITION BY ep.PLAYERNAME ORDER BY e.STARTEVENTDATE DESC) = 1
        ),
        last_game_markets AS (
            SELECT DISTINCT rpe.PLAYERNAME, m.MARKETTYPENAME, rpe.STARTEVENTDATE AS LAST_GAME_DATE
            FROM recent_player_events rpe
            JOIN SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp2
                ON mp2.PLAYERNAME = rpe.PLAYERNAME AND mp2.EVENTID = rpe.EVENTID
            JOIN SPORTSCONTENT.DBO.MARKETS m ON m.MARKETID = mp2.MARKETID AND m.EVENTID = rpe.EVENTID
        )
        SELECT PLAYERNAME, MARKETTYPENAME, LAST_GAME_DATE
        FROM last_game_markets
        ORDER BY PLAYERNAME, MARKETTYPENAME
    """)

@st.cache_data(ttl=30, show_spinner=False)
def get_current_markets(event_id: str) -> pd.DataFrame:
    return run_query(f"""
        SELECT mp.PLAYERNAME, m.MARKETTYPENAME,
               MAX(CASE WHEN m.ISREMOVED = FALSE THEN 1 ELSE 0 END) AS IS_LIVE
        FROM SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp
        JOIN SPORTSCONTENT.DBO.MARKETS m ON m.MARKETID = mp.MARKETID AND m.EVENTID = '{event_id}'
        WHERE mp.EVENTID = '{event_id}'
        GROUP BY mp.PLAYERNAME, m.MARKETTYPENAME
    """)

@st.cache_data(ttl=1800, show_spinner=False)
def get_bulk_baselines(event_ids: tuple) -> pd.DataFrame:
    """Single query fetching last-game baselines for all events at once."""
    ids_sql = ",".join(f"'{e}'" for e in event_ids)
    return run_query(f"""
        WITH event_players AS (
            SELECT DISTINCT PLAYERNAME, EVENTID AS TARGET_EVENT
            FROM SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL
            WHERE EVENTID IN ({ids_sql})
        ),
        recent_player_events AS (
            SELECT ep.PLAYERNAME, ep.TARGET_EVENT, mp.EVENTID, e.STARTEVENTDATE
            FROM event_players ep
            JOIN SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp ON mp.PLAYERNAME = ep.PLAYERNAME
            JOIN SPORTSCONTENT.DBO.EVENTS e ON e.EVENTID = mp.EVENTID
            WHERE e.STARTEVENTDATE >= CURRENT_TIMESTAMP - INTERVAL '60 days'
              AND e.STARTEVENTDATE < CURRENT_TIMESTAMP
              AND mp.EVENTID NOT IN ({ids_sql})
            QUALIFY DENSE_RANK() OVER (
                PARTITION BY ep.PLAYERNAME, ep.TARGET_EVENT
                ORDER BY e.STARTEVENTDATE DESC
            ) = 1
        )
        SELECT DISTINCT rpe.TARGET_EVENT AS EVENTID, rpe.PLAYERNAME,
                        m.MARKETTYPENAME, rpe.STARTEVENTDATE AS LAST_GAME_DATE
        FROM recent_player_events rpe
        JOIN SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp2
            ON mp2.PLAYERNAME = rpe.PLAYERNAME AND mp2.EVENTID = rpe.EVENTID
        JOIN SPORTSCONTENT.DBO.MARKETS m
            ON m.MARKETID = mp2.MARKETID AND m.EVENTID = rpe.EVENTID
    """)

@st.cache_data(ttl=30, show_spinner=False)
def get_bulk_current_markets(event_ids: tuple) -> pd.DataFrame:
    """Single query fetching current market state for all events at once."""
    ids_sql = ",".join(f"'{e}'" for e in event_ids)
    return run_query(f"""
        SELECT mp.EVENTID, mp.PLAYERNAME, m.MARKETTYPENAME,
               MAX(CASE WHEN m.ISREMOVED = FALSE THEN 1 ELSE 0 END) AS IS_LIVE
        FROM SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp
        JOIN SPORTSCONTENT.DBO.MARKETS m ON m.MARKETID = mp.MARKETID AND m.EVENTID = mp.EVENTID
        WHERE mp.EVENTID IN ({ids_sql})
        GROUP BY mp.EVENTID, mp.PLAYERNAME, m.MARKETTYPENAME
    """)

@st.cache_data(ttl=300, show_spinner=False)
def get_player_info(event_id: str) -> pd.DataFrame:
    participants = run_query(f"""
        SELECT PARTICIPANTSNAME AS TEAM, VENUEROLE
        FROM SPORTSCONTENT.DBO.EVENTSPARTICIPANTS_GLOBAL
        WHERE EVENTID = '{event_id}'
        QUALIFY ROW_NUMBER() OVER (PARTITION BY VENUEROLE ORDER BY RECORD_MODIFY_TIMESTAMP DESC) = 1
    """)
    team_map  = dict(zip(participants["VENUEROLE"], participants["TEAM"]))
    home_team = team_map.get("Home", "")
    away_team = team_map.get("Away", "")
    players = run_query(f"""
        SELECT DISTINCT PLAYERSNAME AS PLAYERNAME, VENUEROLE,
               TRY_PARSE_JSON(METADATA):position::string AS POSITION
        FROM SPORTSCONTENT.DBO.EVENTSPLAYERS_GLOBAL
        WHERE EVENTID = '{event_id}'
        QUALIFY ROW_NUMBER() OVER (PARTITION BY PLAYERSNAME ORDER BY RECORD_MODIFY_TIMESTAMP DESC) = 1
    """)
    players["TEAM"]       = players["VENUEROLE"].map({"HomePlayer": home_team, "AwayPlayer": away_team}).fillna("")
    players["TEAM_ORDER"] = players["VENUEROLE"].map({"HomePlayer": 0, "AwayPlayer": 1}).fillna(2)
    return players[["PLAYERNAME", "TEAM", "TEAM_ORDER", "POSITION"]]

@st.cache_data(ttl=30, show_spinner=False)
def get_activity_feed(event_id: str, minutes: int = 30) -> pd.DataFrame:
    """
    One row per (market type, action) change in the last N minutes.
    Shows how many players are affected rather than repeating per player.
    """
    return run_query(f"""
        WITH latest_per_market AS (
            SELECT
                m.MARKETID,
                m.MARKETTYPENAME,
                m.ISREMOVED,
                m.FIRSTMESSAGETIMESTAMP,
                m.LASTMESSAGETIMESTAMP,
                ROW_NUMBER() OVER (
                    PARTITION BY m.MARKETTYPENAME, m.ISREMOVED
                    ORDER BY GREATEST(m.FIRSTMESSAGETIMESTAMP, m.LASTMESSAGETIMESTAMP) DESC
                ) AS rn
            FROM SPORTSCONTENT.DBO.MARKETS m
            WHERE m.EVENTID = '{event_id}'
              AND GREATEST(m.FIRSTMESSAGETIMESTAMP, m.LASTMESSAGETIMESTAMP)
                  >= DATEADD('minute', -{minutes}, CURRENT_TIMESTAMP)
        ),
        deduped AS (
            SELECT MARKETID, MARKETTYPENAME, ISREMOVED,
                   GREATEST(FIRSTMESSAGETIMESTAMP, LASTMESSAGETIMESTAMP) AS CHANGED_AT
            FROM latest_per_market
            WHERE rn = 1
        )
        SELECT
            d.MARKETTYPENAME,
            CASE WHEN d.ISREMOVED = TRUE THEN 'REMOVED' ELSE 'PUBLISHED' END AS ACTION,
            d.CHANGED_AT,
            COUNT(DISTINCT mp.PLAYERNAME) AS PLAYER_COUNT,
            LISTAGG(DISTINCT mp.PLAYERNAME, ', ') WITHIN GROUP (ORDER BY mp.PLAYERNAME) AS PLAYERS
        FROM deduped d
        JOIN SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp
            ON mp.MARKETID = d.MARKETID AND mp.EVENTID = '{event_id}'
        GROUP BY d.MARKETTYPENAME, d.ISREMOVED, d.CHANGED_AT
        ORDER BY d.CHANGED_AT DESC
    """)

# ── Build helpers ─────────────────────────────────────────────────────────────

def compute_group_pcts(baselines: pd.DataFrame, current: pd.DataFrame, league_name: str) -> dict:
    """Returns {group: (live, total)}. REMOVED counts against live."""
    df = build_status_df(baselines, current, league_name)
    if df.empty:
        return {}
    result = {}
    for grp in GROUP_ORDER:
        grp_df = df[df["GROUP"] == grp]
        if grp_df.empty:
            continue
        live  = int((grp_df["STATUS"] == "LIVE").sum())
        total = int(len(grp_df))
        result[grp] = (live, total)
    return result

# O/U ↔ Milestone pairing: maps each O/U market to its expected Milestone partner
OU_MILESTONE_PAIRS = {
    "Points O/U":                       "Points Milestones",
    "Rebounds O/U":                     "Rebounds Milestones",
    "Assists O/U":                      "Assists Milestones",
    "Three Pointers Made O/U":          "Three Pointers Made Milestones",
    "Points + Rebounds O/U":            "Points + Rebounds Milestones",
    "Points + Assists O/U":             "Points + Assists Milestones",
    "Rebounds + Assists O/U":           "Rebounds + Assists Milestones",
    "Points + Rebounds + Assists O/U":  "Points + Rebounds + Assists Milestones",
    # MLB
    "Hits O/U":                         "Hits Milestones",
    "Strikeouts Thrown O/U":            "Strikeouts Thrown Milestones",
    "Earned Runs Allowed O/U":          "Earned Runs Allowed Milestones",
    "Hits Allowed O/U":                 "Hits Allowed Milestones",
    "Total Bases O/U":                  "Total Bases Milestones",
    "Hits + Runs + RBIs O/U":          "Hits + Runs + RBIs Milestones",
    "Runs + RBIs O/U":                  "Runs + RBIs Milestones",
    "Stolen Bases O/U":                 "Stolen Bases Milestones",
    "Singles O/U":                      "Singles Milestones",
    "Triples O/U":                      "Triples Milestones",
    "Walks Allowed O/U":                "Walks Allowed Milestones",
    "Strikeouts O/U":                   "Strikeouts Milestones",
}

def get_pairing_flags(df: pd.DataFrame) -> tuple:
    """
    Returns (urgent_flags, fyi_flags) DataFrames.
    urgent = O/U live but Milestone not live (bettor can bet the line but not milestones)
    fyi    = Milestone live but O/U not live (less critical)
    """
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()
    status_map = df.groupby(["PLAYERNAME", "MARKET"])["STATUS"].first().to_dict()
    urgent, fyi = [], []
    for player in df["PLAYERNAME"].unique():
        for ou, mile in OU_MILESTONE_PAIRS.items():
            ou_status   = status_map.get((player, ou))
            mile_status = status_map.get((player, mile))
            if ou_status is None and mile_status is None:
                continue
            if ou_status == "LIVE" and mile_status != "LIVE":
                urgent.append({
                    "PLAYERNAME": player,
                    "ISSUE": f"{ou} ✓  →  {mile} {mile_status or 'not posted'}",
                })
            elif mile_status == "LIVE" and ou_status != "LIVE":
                fyi.append({
                    "PLAYERNAME": player,
                    "ISSUE": f"{mile} ✓  →  {ou} {ou_status or 'not posted'}",
                })
    return (
        pd.DataFrame(urgent) if urgent else pd.DataFrame(),
        pd.DataFrame(fyi)    if fyi    else pd.DataFrame(),
    )

def build_status_df(baselines: pd.DataFrame, current: pd.DataFrame, league_name: str,
                    player_info: pd.DataFrame = None) -> pd.DataFrame:
    is_mlb  = "mlb" in league_name.lower()
    is_wnba = "wnba" in league_name.lower()

    current_map = current.set_index(["PLAYERNAME", "MARKETTYPENAME"])["IS_LIVE"].to_dict() if not current.empty else {}

    rows = []

    if not baselines.empty:
        bl = baselines.copy()
        bl["GROUP"] = bl["MARKETTYPENAME"].apply(classify_market)
        bl = bl[bl["GROUP"] != "Exclude"]
        if is_mlb:
            bl = bl[~bl["MARKETTYPENAME"].isin(MLB_EXCLUDED_MARKETS)]
        if is_wnba:
            bl = bl[~bl["MARKETTYPENAME"].isin(WNBA_EXCLUDED_MARKETS)]
        prop_players = bl[bl["GROUP"].isin(["Balanced", "Milestones"])]["PLAYERNAME"].unique()
        # Only include players who have at least one live individual prop (Balanced or Milestones)
        # — 1st Points Scorer / team markets keep scratched players in the feed so we can't
        #   use any-market presence; must require a live O/U or Milestone
        if not current.empty:
            live_individual = current[
                (current["IS_LIVE"] == 1) &
                (current["MARKETTYPENAME"].apply(classify_market).isin(["Balanced", "Milestones"]))
            ]["PLAYERNAME"].unique()
            players_in_event = set(live_individual)
        else:
            players_in_event = set()
        prop_players = [p for p in prop_players if p in players_in_event]
        bl = bl[bl["PLAYERNAME"].isin(prop_players)]

        for _, row in bl.iterrows():
            key = (row["PLAYERNAME"], row["MARKETTYPENAME"])
            if key in current_map:
                status = "LIVE" if current_map[key] == 1 else "REMOVED"
            else:
                status = "MISSING"
            rows.append({
                "PLAYERNAME": row["PLAYERNAME"],
                "MARKET":     row["MARKETTYPENAME"],
                "GROUP":      row["GROUP"],
                "LAST_GAME":  str(row["LAST_GAME_DATE"])[:10],
                "STATUS":     status,
            })

    # For MLB: any roster player not covered by their own baseline should still
    # appear using whatever markets are currently live/missing for them
    if is_mlb and player_info is not None and not current.empty:
        covered = {r["PLAYERNAME"] for r in rows}
        roster_players = set(player_info["PLAYERNAME"].tolist())
        for player in roster_players - covered:
            player_current = current[current["PLAYERNAME"] == player]
            for _, crow in player_current.iterrows():
                grp = classify_market(crow["MARKETTYPENAME"])
                if grp in ("Exclude",) or crow["MARKETTYPENAME"] in MLB_EXCLUDED_MARKETS:
                    continue
                status = "LIVE" if crow["IS_LIVE"] == 1 else "REMOVED"
                rows.append({
                    "PLAYERNAME": player,
                    "MARKET":     crow["MARKETTYPENAME"],
                    "GROUP":      grp,
                    "LAST_GAME":  "roster",
                    "STATUS":     status,
                })

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # Drop players where every market is REMOVED — they're scratched/pulled
    # and should not appear anywhere or count against availability %s
    active = df.groupby("PLAYERNAME")["STATUS"].apply(lambda s: not (s == "REMOVED").all())
    return df[df["PLAYERNAME"].isin(active[active].index)]

STATUS_COLOR = {"LIVE": "#16a34a", "MISSING": "#dc2626", "REMOVED": "#b45309"}

def market_short(name: str) -> str:
    replacements = [
        ("Points + Rebounds + Assists", "PRA"),
        ("Points + Rebounds", "P+R"),
        ("Points + Assists", "P+A"),
        ("Rebounds + Assists", "R+A"),
        ("Three Pointers Made", "3PM"),
        ("Hits + Runs + RBIs", "H+R+RBI"),
        ("Hits Allowed + Walks Allowed + Earned Runs Allowed", "HA+BB+ER"),
        ("Strikeouts Thrown", "K Thrown"),
        ("Earned Runs Allowed", "ERA"),
        ("Stolen Bases", "SB"),
        ("Total Bases", "TB"),
        ("Points", "Pts"),
        ("Rebounds", "Reb"),
        ("Assists", "Ast"),
        ("Strikeouts", "K"),
        ("Triples", "3B"),
        ("Singles", "1B"),
        (" O/U", ""),
        (" Milestones", " Mile"),
        ("Double-Double", "Dbl-Dbl"),
        ("Triple-Double", "Tri-Dbl"),
        ("1st Points Scorer", "1st Pts"),
        ("Player First Field Goal Made Type", "1st FG Type"),
        ("1st Batter to Strike Out", "1st K"),
        ("1st Stolen Base", "1st SB"),
        ("1st Hit", "1st Hit"),
        ("Hits Allowed (X or Fewer)", "H Allow"),
        ("Hits Allowed", "HA"),
    ]
    out = name
    for old, new in replacements:
        out = out.replace(old, new)
    return out.strip()

# ── Render: market completion ─────────────────────────────────────────────────

def render_market_completion(df: pd.DataFrame, league_name: str = ""):
    summary = (
        df.groupby(["GROUP", "MARKET"])["STATUS"]
        .value_counts().unstack(fill_value=0).reset_index()
    )
    for c in ["LIVE", "MISSING", "REMOVED"]:
        if c not in summary.columns:
            summary[c] = 0
    summary["TOTAL"] = summary["LIVE"] + summary["MISSING"] + summary["REMOVED"]
    summary["PCT"]   = summary["LIVE"] / summary["TOTAL"].replace(0, 1)
    summary["GORD"]  = summary["GROUP"].apply(lambda g: GROUP_ORDER.index(g) if g in GROUP_ORDER else 99)

    is_wnba = "wnba" in league_name.lower()
    if is_wnba:
        summary["MORD"] = summary["MARKET"].apply(_wnba_market_sort_key)
        summary = summary.sort_values(["GORD", "MORD"]).reset_index(drop=True)
    else:
        summary = summary.sort_values(["GORD", "PCT"]).reset_index(drop=True)

    last_grp = None
    for _, row in summary.iterrows():
        grp    = row["GROUP"]
        pct    = float(row["PCT"])
        color  = pct_color(pct)
        bar_w  = int(pct * 100)
        live   = int(row["LIVE"])
        total  = int(row["TOTAL"])
        market = str(row["MARKET"])

        if grp != last_grp:
            st.markdown(
                "<div style='margin:18px 0 6px;font-size:0.68em;text-transform:uppercase;"
                "letter-spacing:0.1em;color:#6b7280;font-weight:700'>" + grp + "</div>",
                unsafe_allow_html=True,
            )
            last_grp = grp

        not_live = df[(df["MARKET"] == market) & (df["STATUS"] != "LIVE")]
        if not not_live.empty:
            parts = []
            missing_p = not_live[not_live["STATUS"] == "MISSING"]["PLAYERNAME"].tolist()
            removed_p = not_live[not_live["STATUS"] == "REMOVED"]["PLAYERNAME"].tolist()
            if missing_p:
                names = ", ".join(p.split()[-1] for p in missing_p[:6])
                if len(missing_p) > 6: names += f" +{len(missing_p)-6}"
                parts.append("<span style='color:#f87171'>" + names + "</span>")
            if removed_p:
                names = ", ".join(p.split()[-1] for p in removed_p[:6])
                if len(removed_p) > 6: names += f" +{len(removed_p)-6}"
                parts.append("<span style='color:#b45309'>" + names + " (removed)</span>")
            sub = (
                "<div style='font-size:0.68em;margin-top:1px;white-space:nowrap;"
                "overflow:hidden;text-overflow:ellipsis'>" + "  ·  ".join(parts) + "</div>"
            )
        else:
            sub = ""

        bar_track = "<div style='width:80px;background:#374151;border-radius:3px;height:7px'>"
        bar_fill  = "<div style='width:" + str(bar_w) + "%;background:" + color + ";height:7px;border-radius:3px'></div>"
        count_span = (
            "<span style='font-size:0.8em;font-weight:700;color:" + color + ";min-width:38px;"
            "text-align:right'>" + str(live) + "/" + str(total) + "</span>"
        )
        right_div = (
            "<div style='display:flex;align-items:center;gap:8px;justify-content:flex-end'>"
            + bar_track + bar_fill + "</div>"
            + count_span
            + "</div>"
        )
        row_html = (
            "<div style='display:grid;grid-template-columns:1fr auto;"
            "align-items:center;gap:12px;padding:6px 0;border-bottom:1px solid #1f2937'>"
            "<div>"
            "<div style='font-size:0.83em;font-weight:500'>" + market + "</div>"
            + sub +
            "</div>"
            "<div style='text-align:right;min-width:140px'>"
            + right_div +
            "</div>"
            "</div>"
        )
        st.markdown(row_html, unsafe_allow_html=True)

# ── Render: player cards ──────────────────────────────────────────────────────

def render_player_cards(df: pd.DataFrame, player_info: pd.DataFrame, sport_name: str):
    info = player_info.set_index("PLAYERNAME")
    missing_count = df[df["STATUS"] == "MISSING"].groupby("PLAYERNAME").size().to_dict()

    def pos_order(p):
        pos = str(info.loc[p, "POSITION"]).lower() if p in info.index else ""
        if "baseball" in sport_name.lower():
            if "pitcher" in pos:  return 0
            if "two-way" in pos:  return 1
            return 2
        return 0

    # Team first, then most missing within each team, then position (baseball), then name
    players = sorted(df["PLAYERNAME"].unique(),
        key=lambda p: (
            int(info.loc[p, "TEAM_ORDER"]) if p in info.index else 99,
            -missing_count.get(p, 0),
            pos_order(p),
            p,
        ))

    current_team = None
    cols_per_row = 4
    card_buffer  = []

    def build_card_html(player, player_df):
        markets   = player_df.sort_values(["GROUP", "MARKET"])
        n_live    = int((markets["STATUS"] == "LIVE").sum())
        n_missing = int((markets["STATUS"] == "MISSING").sum())
        n_removed = int((markets["STATUS"] == "REMOVED").sum())
        n_total   = int(len(markets))
        pct       = n_live / n_total if n_total else 0
        border    = "#dc2626" if n_missing > 0 else ("#b45309" if n_removed > 0 else "#16a34a")
        bar_fill  = str(int(pct * 56))

        # Only show markets that are not live (missing or removed)
        not_live_markets = markets[markets["STATUS"] != "LIVE"]

        if n_missing == 0 and n_removed == 0:
            sections = (
                "<div style='margin-top:8px;font-size:0.72em;color:#16a34a;font-weight:600'>"
                "All markets live ✓</div>"
            )
        else:
            sections = ""
            for grp in [g for g in GROUP_ORDER if g in not_live_markets["GROUP"].values]:
                grp_mrkts = not_live_markets[not_live_markets["GROUP"] == grp]
                pills = ""
                for _, mrow in grp_mrkts.iterrows():
                    short = market_short(str(mrow["MARKET"]))
                    pills += (
                        "<span style='display:inline-block;margin:2px 3px 2px 0;"
                        "padding:2px 7px;border-radius:3px;font-size:0.68em;font-weight:500;"
                        "background:" + STATUS_COLOR.get(str(mrow["STATUS"]), "#6b7280") + ";color:white;white-space:nowrap'>"
                        + short + "</span>"
                    )
                sections += (
                    "<div style='margin-top:7px'>"
                    "<span style='font-size:0.62em;text-transform:uppercase;letter-spacing:0.08em;"
                    "color:#4b5563;font-weight:700'>" + grp + "</span>"
                    "<div style='margin-top:3px'>" + pills + "</div>"
                    "</div>"
                )

        bar_html = (
            "<div style='width:56px;height:3px;background:#374151;border-radius:2px;margin-top:3px'>"
            "<div style='width:" + bar_fill + "px;height:3px;background:" + border + ";border-radius:2px'></div>"
            "</div>"
        )
        count_str = str(n_live) + "/" + str(n_total)

        return (
            "<div style='border:1px solid " + border + ";border-radius:8px;"
            "padding:11px 13px 9px;margin-bottom:8px;background:#0f172a'>"
            "<div style='display:flex;justify-content:space-between;align-items:flex-start'>"
            "<span style='font-weight:700;font-size:0.88em;line-height:1.3'>" + player + "</span>"
            "<div style='text-align:right;flex-shrink:0;margin-left:8px'>"
            "<span style='font-size:0.75em;color:" + border + ";font-weight:700'>" + count_str + "</span>"
            + bar_html +
            "</div>"
            "</div>"
            + sections +
            "</div>"
        )

    def flush(cards):
        for i in range(0, len(cards), cols_per_row):
            chunk = cards[i:i + cols_per_row]
            cols  = st.columns(cols_per_row)
            for col, (player, player_df) in zip(cols, chunk):
                col.markdown(build_card_html(player, player_df), unsafe_allow_html=True)

    is_baseball = "baseball" in sport_name.lower() or "mlb" in sport_name.lower()

    for player in players:
        player_df = df[df["PLAYERNAME"] == player]
        # For MLB: skip players with no live or removed markets (lineup scratch, props not up yet)
        if is_baseball and (player_df["STATUS"] == "MISSING").all():
            continue

        team = info.loc[player, "TEAM"] if player in info.index else ""
        if team != current_team:
            if card_buffer:
                flush(card_buffer)
                card_buffer = []
            current_team = team
            st.markdown(
                "<div style='margin:14px 0 6px;padding:5px 12px;background:#1e293b;"
                "border-left:3px solid #3b82f6;border-radius:0 4px 4px 0'>"
                "<span style='font-weight:700;font-size:0.85em;letter-spacing:0.05em;"
                "text-transform:uppercase;color:#93c5fd'>" + (team or "Unknown") + "</span></div>",
                unsafe_allow_html=True,
            )
        card_buffer.append((player, player_df))

    if card_buffer:
        flush(card_buffer)

# ── Page: Detail view ─────────────────────────────────────────────────────────

def show_detail(event_row, league_name):
    st_autorefresh(interval=60_000, key="detail_refresh")

    back, title = st.columns([1, 10])
    if back.button("← Back"):
        st.session_state.selected_event_id = None
        st.rerun()
    title.markdown(
        "<h2 style='margin:0;padding:4px 0'>" + event_row["EVENTNAME"] + "</h2>"
        "<span style='color:#6b7280;font-size:0.88em'>"
        + event_row["LEAGUENAME"] + "  ·  "
        + event_row["STARTEVENTDATE"].strftime("%b %d, %I:%M %p PT") + "  ·  "
        "<b style='color:#e5e7eb'>" + event_row["countdown"] + "</b></span>",
        unsafe_allow_html=True,
    )

    try:
        baselines   = get_player_baselines(event_row["EVENTID"])
        current     = get_current_markets(event_row["EVENTID"])
        player_info = get_player_info(event_row["EVENTID"])
    except Exception as e:
        st.error(f"Query failed: {e}")
        return

    df = build_status_df(baselines, current, league_name, player_info)
    if df.empty:
        st.warning("No player prop markets found for this event.")
        return

    st.divider()

    # ── Load activity feed + pairing flags ───────────────────────────────────
    try:
        activity = get_activity_feed(event_row["EVENTID"])
    except Exception:
        activity = pd.DataFrame()

    urgent_flags, fyi_flags = get_pairing_flags(df)

    # ── Summary counts (REMOVED counts as not-live) ───────────────────────────
    filtered = df.copy()
    live    = int((filtered["STATUS"] == "LIVE").sum())
    missing = int((filtered["STATUS"] == "MISSING").sum())
    removed = int((filtered["STATUS"] == "REMOVED").sum())
    total   = int(len(filtered))

    cols_sum = st.columns([1, 1, 1, 1, 1])
    cols_sum[0].metric("Live",       live)
    cols_sum[1].metric("Missing",    missing)
    cols_sum[2].metric("Removed",    removed)
    cols_sum[3].metric("Line/Mile gaps", len(urgent_flags))
    cols_sum[4].metric("Total",      total)

    # ── Urgent pairing flags: O/U live, Milestone not ────────────────────────
    if not urgent_flags.empty:
        with st.expander(f"⚠️ Line posted, Milestone missing ({len(urgent_flags)})", expanded=True):
            for _, row in urgent_flags.iterrows():
                st.markdown(
                    "<div style='padding:4px 0;font-size:0.85em'>"
                    "<span style='color:#f87171;font-weight:700'>" + row["PLAYERNAME"] + "</span>"
                    "  —  <span style='color:#e5e7eb'>" + row["ISSUE"] + "</span>"
                    "</div>",
                    unsafe_allow_html=True,
                )

    # ── FYI pairing flags: Milestone live, O/U not ───────────────────────────
    if not fyi_flags.empty:
        with st.expander(f"ℹ️ Milestone posted, Line missing ({len(fyi_flags)})", expanded=False):
            for _, row in fyi_flags.iterrows():
                st.markdown(
                    "<div style='padding:4px 0;font-size:0.85em'>"
                    "<span style='color:#fbbf24;font-weight:700'>" + row["PLAYERNAME"] + "</span>"
                    "  —  <span style='color:#9ca3af'>" + row["ISSUE"] + "</span>"
                    "</div>",
                    unsafe_allow_html=True,
                )

    # ── Activity feed ─────────────────────────────────────────────────────────
    if not activity.empty:
        PT = pytz.timezone("America/Los_Angeles")
        activity["CHANGED_AT_PT"] = pd.to_datetime(activity["CHANGED_AT"]).dt.tz_localize("UTC").dt.tz_convert(PT)
        with st.expander(f"📋 Recent activity — last 30 min ({len(activity)} changes)", expanded=False):
            for _, row in activity.iterrows():
                color    = "#16a34a" if row["ACTION"] == "PUBLISHED" else "#dc2626"
                ts       = row["CHANGED_AT_PT"].strftime("%I:%M %p")
                players  = str(row["PLAYERS"]) if row["PLAYERS"] else ""
                st.markdown(
                    "<div style='padding:5px 0;border-bottom:1px solid #1e293b;font-size:0.82em'>"
                    "<div style='display:flex;gap:12px;align-items:center'>"
                    "<span style='color:#6b7280;min-width:60px'>" + ts + "</span>"
                    "<span style='color:" + color + ";font-weight:700;min-width:80px'>" + row["ACTION"] + "</span>"
                    "<span style='color:#e5e7eb;font-weight:500'>" + row["MARKETTYPENAME"] + "</span>"
                    "</div>"
                    "<div style='color:#9ca3af;font-size:0.9em;margin-top:2px;padding-left:152px'>" + players + "</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )

    st.divider()

    # ── Group tabs (REMOVED counts against live in label) ────────────────────
    all_groups = [g for g in GROUP_ORDER if g in df["GROUP"].unique()]
    tab_labels = []
    for g in all_groups:
        grp_df  = filtered[filtered["GROUP"] == g]
        n_miss  = int((grp_df["STATUS"] == "MISSING").sum())
        n_rem   = int((grp_df["STATUS"] == "REMOVED").sum())
        n_live  = int((grp_df["STATUS"] == "LIVE").sum())
        n_total = int(len(grp_df))
        n_bad   = n_miss + n_rem
        if n_bad:
            label = g + f"  ❌ {n_bad}/{n_total}"
        else:
            label = g + f"  ✅ {n_live}"
        tab_labels.append(label)

    tabs = st.tabs(tab_labels)
    for tab, grp in zip(tabs, all_groups):
        with tab:
            grp_df = filtered[filtered["GROUP"] == grp]
            if grp_df.empty:
                st.caption("No markets in this group.")
                continue

            st.markdown("##### Market Completion")
            render_market_completion(grp_df, league_name)

            st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

            st.markdown("##### Players")
            render_player_cards(grp_df, player_info, league_name)

# ── Page: Overview ────────────────────────────────────────────────────────────

def show_overview(events_df, league_name):
    st_autorefresh(interval=60_000, key="overview_refresh")

    if events_df.empty:
        st.warning(f"No upcoming events with player props found for {league_name}.")
        return

    event_ids = tuple(events_df["EVENTID"].tolist())
    PT        = pytz.timezone("America/Los_Angeles")
    now_pt    = pd.Timestamp.utcnow().tz_convert(PT)

    # ── Two bulk queries, then all computation in Python ─────────────────────
    try:
        bulk_baselines = get_bulk_baselines(event_ids)
        bulk_current   = get_bulk_current_markets(event_ids)
    except Exception as e:
        st.error(f"Failed to load market stats: {e}")
        return

    # Pre-compute pcts for every event
    all_pcts = {}
    for eid in event_ids:
        bl  = bulk_baselines[bulk_baselines["EVENTID"] == eid].drop(columns="EVENTID")
        cur = bulk_current[bulk_current["EVENTID"] == eid].drop(columns="EVENTID")
        try:
            all_pcts[eid] = compute_group_pcts(bl, cur, league_name)
        except Exception:
            all_pcts[eid] = {}

    # ── Render all cards ──────────────────────────────────────────────────────
    for _, ev in events_df.iterrows():
        eid       = ev["EVENTID"]
        countdown = ev["countdown"]
        start_str = ev["STARTEVENTDATE"].strftime("%b %d, %I:%M %p PT")
        pcts_raw  = all_pcts.get(eid, {})
        group_pcts   = {g: v[0] / v[1] if v[1] else 0.0 for g, v in pcts_raw.items()}
        group_counts = pcts_raw

        secs_left = int((ev["STARTEVENTDATE"] - now_pt).total_seconds())
        if secs_left <= 0:
            cd_color, cd_bg = "#4ade80", "#052e16"
        elif secs_left < 3600:
            cd_color, cd_bg = "#f87171", "#2d0a0a"
        elif secs_left < 10800:
            cd_color, cd_bg = "#fbbf24", "#2d1b00"
        else:
            cd_color, cd_bg = "#93c5fd", "#0f172a"

        with st.container(border=True):
            col_cd, col_info, col_stats, col_btn = st.columns([1, 3, 4, 1])

            with col_cd:
                st.markdown(
                    "<div style='background:" + cd_bg + ";border-radius:8px;padding:10px 6px;"
                    "text-align:center;height:100%;display:flex;flex-direction:column;"
                    "justify-content:center;align-items:center'>"
                    "<span style='font-size:1.5em;font-weight:800;color:" + cd_color + ";line-height:1.1'>"
                    + countdown + "</span>"
                    "<span style='font-size:0.65em;color:#6b7280;margin-top:4px'>" + start_str + "</span>"
                    "</div>",
                    unsafe_allow_html=True,
                )

            with col_info:
                st.markdown(
                    "<div style='padding:8px 0'>"
                    "<div style='font-size:1.05em;font-weight:700'>" + ev["EVENTNAME"] + "</div>"
                    "<div style='font-size:0.78em;color:#6b7280;margin-top:3px'>" + ev["LEAGUENAME"] + "</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )

            with col_stats:
                if group_pcts:
                    pills_html = "<div style='display:flex;flex-wrap:wrap;gap:8px;padding:4px 0'>"
                    for grp, pct in group_pcts.items():
                        color         = pct_color(pct)
                        live_n, total_n = group_counts.get(grp, (0, 0))
                        pills_html += (
                            "<div style='display:flex;flex-direction:column;align-items:center;"
                            "min-width:72px;padding:4px 8px;border-radius:6px;background:#1e293b'>"
                            "<span style='font-size:1.25em;font-weight:800;color:" + color + ";line-height:1.1'>"
                            + str(int(pct * 100)) + "%</span>"
                            "<span style='font-size:0.7em;color:" + color + ";font-weight:600'>"
                            + str(live_n) + "/" + str(total_n) + "</span>"
                            "<span style='font-size:0.62em;color:#6b7280;margin-top:1px'>" + grp + "</span>"
                            "</div>"
                        )
                    pills_html += "</div>"
                    st.markdown(pills_html, unsafe_allow_html=True)
                else:
                    st.caption("No baseline data for this league.")

            with col_btn:
                st.markdown("<div style='padding-top:14px'>", unsafe_allow_html=True)
                if st.button("View →", key="btn_" + eid):
                    st.session_state.selected_event_id = eid
                    st.rerun()
                st.markdown("</div>", unsafe_allow_html=True)

# ── Main ──────────────────────────────────────────────────────────────────────

st.markdown(
    "<h1 style='text-align:center;margin-bottom:4px'>Market Availability</h1>",
    unsafe_allow_html=True,
)

# League selector — two buttons, no dropdown
if "selected_league" not in st.session_state:
    st.session_state.selected_league = "WNBA"

btn_col = st.columns([3, 1, 1, 3])
for i, league_name in enumerate(LEAGUES):
    active = st.session_state.selected_league == league_name
    style  = "primary" if active else "secondary"
    if btn_col[i + 1].button(league_name, type=style, use_container_width=True):
        st.session_state.selected_league     = league_name
        st.session_state.selected_event_id   = None

selected_league = st.session_state.selected_league
sport_id        = LEAGUES[selected_league]["sport_id"]

st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
st.divider()

try:
    events_df = get_events(sport_id)
except Exception as e:
    st.error(f"Failed to load events: {e}")
    st.stop()

if events_df.empty:
    st.warning(f"No upcoming {selected_league} events with player props.")
    st.stop()

PT     = pytz.timezone("America/Los_Angeles")
now_pt = pd.Timestamp.utcnow().tz_convert(PT)
events_df["STARTEVENTDATE"] = events_df["STARTEVENTDATE"].dt.tz_localize("UTC").dt.tz_convert(PT)
events_df["countdown"]      = events_df["STARTEVENTDATE"].apply(lambda s: fmt_countdown(s, now_pt))
events_df = events_df.sort_values("STARTEVENTDATE").reset_index(drop=True)

if "selected_event_id" not in st.session_state:
    st.session_state.selected_event_id = None

if st.session_state.selected_event_id:
    match = events_df[events_df["EVENTID"] == st.session_state.selected_event_id]
    if match.empty:
        st.session_state.selected_event_id = None
        st.rerun()
    show_detail(match.iloc[0], selected_league)
else:
    show_overview(events_df, selected_league)
