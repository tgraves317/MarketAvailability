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

# Allowlist approach for MLB — only these markets are shown, everything else excluded
MLB_ALLOWED_MARKETS = {
    # ── Pitcher ──────────────────────────────────────────────────────────────
    "Outs O/U",
    "Strikeouts Thrown O/U",
    "Strikeouts Thrown Milestones",
    "Walks Allowed O/U",
    "Walks Allowed (X or Fewer)",
    "Hits Allowed O/U",
    "Hits Allowed (X or Fewer)",
    "Earned Runs Allowed O/U",
    "Earned Runs Allowed (X or Fewer)",
    "Hits Allowed + Walks Allowed + Earned Runs Allowed O/U",
    "Hits Allowed + Walks Allowed + Earned Runs Allowed (X or Fewer)",
    "Win Probability",
    # ── Batter ───────────────────────────────────────────────────────────────
    "Strikeouts (Batter) Milestones",
    "Walks (Batter) O/U",
    "Walks (Batter) Milestones",
    "Home Runs Milestones",
    "Hits O/U",
    "Hits Milestones",
    "Singles O/U",
    "Singles Milestones",
    "Doubles O/U",
    "Doubles Milestones",
    "Triples Milestones",
    "Total Bases O/U",
    "Total Bases Milestones",
    "RBIs O/U",
    "RBIs Milestones",
    "Hits + Runs + RBIs O/U",
    "Hits + Runs + RBIs Milestones",
    "Stolen Bases O/U",
    "Stolen Bases Milestones",
    "Runs (Batter) Milestones",
    "Runs + RBIs Milestones",
    "Extra Base Hits Milestones",
    "Hits + Stolen Bases Milestones",
    "Hits + Walks + Stolen Bases Milestones",
    "Hits + Runs + Stolen Bases Milestones",
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
    if "h2h" in n:
        return "Exclude"
    if n.startswith("most "):
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
    ):
        return "Balanced"
    return "Other"

GROUP_ORDER      = ["Balanced", "Milestones", "Team", "H2H", "Other"]
DEFAULT_GROUPS   = ["Balanced", "Milestones"]
# Groups shown by default in the detail tabs
DETAIL_DEFAULT_GROUPS = ["Balanced", "Milestones"]

WNBA_MARKET_STAT_ORDER = [
    "Points", "Rebounds", "Assists", "Three Pointers Made",
    "Points + Rebounds", "Points + Assists", "Rebounds + Assists",
    "Points + Rebounds + Assists", "Double-Double", "Triple-Double", "1st Points Scorer",
]

_MARKET_SUFFIXES = [" O/U", " Milestones", " H2H ML", " H2H Spread", " H2H Total"]

def _wnba_market_sort_key(market_name: str) -> int:
    base = market_name
    for suffix in _MARKET_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if base.startswith("Most "):
        base = base[5:]
    if base == "Three Pointers":
        base = "Three Pointers Made"
    try:
        return WNBA_MARKET_STAT_ORDER.index(base)
    except ValueError:
        return 99

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

@st.cache_data(ttl=1800, show_spinner=False)
def get_bulk_baselines(event_ids: tuple) -> pd.DataFrame:
    ids_sql = ",".join(f"'{e}'" for e in event_ids)
    # Step 1: get all player+event pairs (fast)
    ep_df = run_query(f"""
        SELECT DISTINCT PLAYERNAME, EVENTID AS TARGET_EVENT
        FROM SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL
        WHERE EVENTID IN ({ids_sql})
    """)
    if ep_df.empty:
        return pd.DataFrame()
    players_sql = ",".join(
        f"'{p.replace(chr(39), chr(39)*2)}'" for p in ep_df["PLAYERNAME"].unique().tolist()
    )
    # Step 2: use EVENTSPLAYERS_GLOBAL for the history lookup (fast)
    return run_query(f"""
        WITH target_players AS (
            SELECT DISTINCT PLAYERNAME, EVENTID AS TARGET_EVENT
            FROM SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL
            WHERE EVENTID IN ({ids_sql})
        ),
        player_recent_events AS (
            SELECT ep.PLAYERSNAME AS PLAYERNAME, ep.EVENTID, e.STARTEVENTDATE,
                   ROW_NUMBER() OVER (PARTITION BY ep.PLAYERSNAME ORDER BY e.STARTEVENTDATE DESC) AS rn
            FROM SPORTSCONTENT.DBO.EVENTSPLAYERS_GLOBAL ep
            JOIN SPORTSCONTENT.DBO.EVENTS e ON e.EVENTID = ep.EVENTID
            WHERE ep.PLAYERSNAME IN ({players_sql})
              AND e.STARTEVENTDATE >= CURRENT_TIMESTAMP - INTERVAL '60 days'
              AND e.STARTEVENTDATE < CURRENT_TIMESTAMP
              AND ep.EVENTID NOT IN ({ids_sql})
        ),
        last_game AS (
            SELECT PLAYERNAME, EVENTID, STARTEVENTDATE FROM player_recent_events WHERE rn = 1
        )
        SELECT DISTINCT tp.TARGET_EVENT AS EVENTID, lg.PLAYERNAME,
                        m.MARKETTYPENAME, lg.STARTEVENTDATE AS LAST_GAME_DATE
        FROM target_players tp
        JOIN last_game lg ON lg.PLAYERNAME = tp.PLAYERNAME
        JOIN SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp
            ON mp.PLAYERNAME = lg.PLAYERNAME AND mp.EVENTID = lg.EVENTID
        JOIN SPORTSCONTENT.DBO.MARKETS m ON m.MARKETID = mp.MARKETID AND m.EVENTID = lg.EVENTID
    """)

@st.cache_data(ttl=30, show_spinner=False)
def get_bulk_current_markets(event_ids: tuple) -> pd.DataFrame:
    ids_sql = ",".join(f"'{e}'" for e in event_ids)
    return run_query(f"""
        SELECT mp.EVENTID, mp.PLAYERNAME, m.MARKETTYPENAME,
               MAX(CASE WHEN m.ISREMOVED = FALSE THEN 1 ELSE 0 END) AS IS_LIVE
        FROM SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp
        JOIN SPORTSCONTENT.DBO.MARKETS m ON m.MARKETID = mp.MARKETID AND m.EVENTID = mp.EVENTID
        WHERE mp.EVENTID IN ({ids_sql})
        GROUP BY mp.EVENTID, mp.PLAYERNAME, m.MARKETTYPENAME
    """)

@st.cache_data(ttl=1800, show_spinner=False)
def get_player_baselines(event_id: str) -> pd.DataFrame:
    # Step 1: get player list (fast — single event filter)
    players_df = run_query(f"""
        SELECT DISTINCT PLAYERNAME FROM SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL
        WHERE EVENTID = '{event_id}'
    """)
    if players_df.empty:
        return pd.DataFrame()
    players_sql = ",".join(
        f"'{p.replace(chr(39), chr(39)*2)}'" for p in players_df["PLAYERNAME"].tolist()
    )
    # Step 2: find last game per player + fetch markets (fast — EVENTSPLAYERS_GLOBAL is small)
    return run_query(f"""
        WITH player_recent_events AS (
            SELECT ep.PLAYERSNAME AS PLAYERNAME, ep.EVENTID, e.STARTEVENTDATE,
                   ROW_NUMBER() OVER (PARTITION BY ep.PLAYERSNAME ORDER BY e.STARTEVENTDATE DESC) AS rn
            FROM SPORTSCONTENT.DBO.EVENTSPLAYERS_GLOBAL ep
            JOIN SPORTSCONTENT.DBO.EVENTS e ON e.EVENTID = ep.EVENTID
            WHERE ep.PLAYERSNAME IN ({players_sql})
              AND e.STARTEVENTDATE >= CURRENT_TIMESTAMP - INTERVAL '60 days'
              AND e.STARTEVENTDATE < CURRENT_TIMESTAMP
              AND ep.EVENTID != '{event_id}'
        )
        SELECT DISTINCT pre.PLAYERNAME, m.MARKETTYPENAME, pre.STARTEVENTDATE AS LAST_GAME_DATE
        FROM player_recent_events pre
        JOIN SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp
            ON mp.PLAYERNAME = pre.PLAYERNAME AND mp.EVENTID = pre.EVENTID
        JOIN SPORTSCONTENT.DBO.MARKETS m ON m.MARKETID = mp.MARKETID AND m.EVENTID = pre.EVENTID
        WHERE pre.rn = 1
        ORDER BY pre.PLAYERNAME, m.MARKETTYPENAME
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
    return run_query(f"""
        WITH latest_per_market AS (
            SELECT m.MARKETID, m.MARKETTYPENAME, m.ISREMOVED,
                   m.FIRSTMESSAGETIMESTAMP, m.LASTMESSAGETIMESTAMP,
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
            FROM latest_per_market WHERE rn = 1
        )
        SELECT d.MARKETTYPENAME,
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

# ── Competitor comparison ─────────────────────────────────────────────────────

# Odds API market key → DK market type name
WNBA_MARKET_MAP = {
    "player_points":                  "Points O/U",
    "player_rebounds":                "Rebounds O/U",
    "player_assists":                 "Assists O/U",
    "player_threes":                  "Three Pointers Made O/U",
    "player_points_rebounds":         "Points + Rebounds O/U",
    "player_points_assists":          "Points + Assists O/U",
    "player_rebounds_assists":        "Rebounds + Assists O/U",
    "player_points_rebounds_assists": "Points + Rebounds + Assists O/U",
    "player_double_double":           "Double-Double",
    "player_triple_double":           "Triple-Double",
}

MLB_MARKET_MAP = {
    "batter_hits":          "Hits O/U",
    "batter_total_bases":   "Total Bases O/U",
    "batter_rbis":          "RBIs O/U",
    "batter_home_runs":     "Home Runs O/U",
    "batter_stolen_bases":  "Stolen Bases O/U",
    "batter_singles":       "Singles O/U",
    "batter_doubles":       "Doubles O/U",
    "batter_triples":       "Triples O/U",
    "batter_strikeouts":    "Strikeouts O/U",
    "batter_walks":         "Walks (Batter) O/U",
    "batter_runs_scored":   "Runs (Batter) O/U",
    "batter_hits_runs_rbis":"Hits + Runs + RBIs O/U",
    "pitcher_strikeouts":   "Strikeouts Thrown O/U",
    "pitcher_outs":         "Outs O/U",
    "pitcher_hits_allowed": "Hits Allowed O/U",
    "pitcher_walks":        "Walks Allowed O/U",
    "pitcher_earned_runs":  "Earned Runs Allowed O/U",
}

PRICE_DIFF_THRESHOLD = 0.04  # implied probability difference (4 percentage points)
LINE_DIFF_THRESHOLD  = 0.5   # line difference in points

def prob_to_american(p: float) -> int:
    p = max(min(p, 0.9999), 0.0001)
    if p >= 0.5:
        return round(-(p / (1 - p)) * 100)
    return round(((1 - p) / p) * 100)

def american_to_prob(a: int) -> float:
    if a < 0:
        return (-a) / (-a + 100)
    return 100 / (a + 100)

@st.cache_data(ttl=60, show_spinner=False)
def get_dk_prices(event_id: str) -> pd.DataFrame:
    """DK's current Over/Under prices per player/market."""
    return run_query(f"""
        SELECT DISTINCT
            mp.PLAYERNAME,
            m.MARKETTYPENAME,
            s.SELECTIONNAME,
            s.POINTS,
            s.LASTPROBABILITY
        FROM SPORTSCONTENT.DBO.SELECTIONS s
        JOIN SPORTSCONTENT.DBO.MARKETS m ON m.MARKETID = s.MARKETID AND m.EVENTID = '{event_id}'
        JOIN SPORTSCONTENT.DBO.MARKETSPLAYERS_GLOBAL mp ON mp.MARKETID = s.MARKETID AND mp.EVENTID = '{event_id}'
        WHERE s.EVENTID = '{event_id}'
          AND s.ISREMOVED = FALSE
          AND m.ISREMOVED = FALSE
          AND s.LASTPROBABILITY > 0
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY mp.PLAYERNAME, m.MARKETTYPENAME, s.SELECTIONNAME
            ORDER BY s.LASTMESSAGETIMESTAMP DESC
        ) = 1
    """)

@st.cache_data(ttl=60, show_spinner=False)
def get_competitor_prices(event_id: str, league_name: str,
                          home_team: str, away_team: str) -> pd.DataFrame:
    """
    Fetch competitor player prop prices from Odds API.
    Returns DataFrame with PLAYERNAME, DK_MARKET, SIDE, LINE, AMERICAN_ODDS.
    """
    import urllib.request, json

    api_key = st.secrets["odds_api"]["key"]
    is_wnba = "wnba" in league_name.lower()

    sport      = "basketball_wnba" if is_wnba else "baseball_mlb"
    bookmaker  = "fanduel"          if is_wnba else "fanatics"
    market_map = WNBA_MARKET_MAP    if is_wnba else MLB_MARKET_MAP
    markets_str = ",".join(market_map.keys())

    # Find matching Odds API event by team name similarity
    try:
        req = urllib.request.urlopen(
            f"https://api.the-odds-api.com/v4/sports/{sport}/events/?apiKey={api_key}",
            timeout=10
        )
        events = json.loads(req.read())
    except Exception:
        return pd.DataFrame()

    # Match by checking if either team name appears in the other
    def team_match(t1: str, t2: str) -> bool:
        t1, t2 = t1.lower(), t2.lower()
        t1_last = t1.split()[-1]
        t2_last = t2.split()[-1]
        return t1_last in t2 or t2_last in t1

    odds_event = None
    for ev in events:
        if (team_match(home_team, ev["home_team"]) or team_match(home_team, ev["away_team"])) and \
           (team_match(away_team, ev["home_team"]) or team_match(away_team, ev["away_team"])):
            odds_event = ev
            break

    if not odds_event:
        return pd.DataFrame()

    try:
        url = (f"https://api.the-odds-api.com/v4/sports/{sport}/events/{odds_event['id']}/odds"
               f"?apiKey={api_key}&regions=us&bookmakers={bookmaker}"
               f"&markets={markets_str}&oddsFormat=american")
        req = urllib.request.urlopen(url, timeout=10)
        data = json.loads(req.read())
    except Exception:
        return pd.DataFrame()

    rows = []
    for bk in data.get("bookmakers", []):
        if bk["key"] != bookmaker:
            continue
        for mkt in bk["markets"]:
            dk_market = market_map.get(mkt["key"])
            if not dk_market:
                continue
            for outcome in mkt["outcomes"]:
                rows.append({
                    "PLAYERNAME":   outcome["description"],
                    "DK_MARKET":    dk_market,
                    "SIDE":         outcome["name"],   # Over / Under / Yes / No
                    "LINE":         outcome.get("point"),
                    "AMERICAN_ODDS": int(outcome["price"]),
                })

    return pd.DataFrame(rows) if rows else pd.DataFrame()

def build_competitor_comparison(dk_prices: pd.DataFrame,
                                comp_prices: pd.DataFrame,
                                league_name: str) -> dict:
    """
    Returns dict with:
      - missing_on_dk:  markets competitor has but DK doesn't
      - missing_on_comp: markets DK has but competitor doesn't
      - price_gaps:     same market/player/line but odds differ >= threshold
    """
    result = {"missing_on_dk": [], "missing_on_comp": [], "price_gaps": [], "line_diffs": [], "arbs": []}
    if comp_prices.empty:
        return result

    is_wnba    = "wnba" in league_name.lower()
    bookmaker  = "FanDuel" if is_wnba else "Fanatics"
    dk_markets = WNBA_MARKET_MAP if is_wnba else MLB_MARKET_MAP

    # Build DK lookup: (player, market, side) → (line, american_odds)
    dk_lookup = {}
    if not dk_prices.empty:
        for _, row in dk_prices.iterrows():
            side = str(row["SELECTIONNAME"]).split()[0]  # "Over" or "Under"
            american = prob_to_american(float(row["LASTPROBABILITY"]))
            key = (row["PLAYERNAME"], row["MARKETTYPENAME"], side)
            dk_lookup[key] = (row["POINTS"], american)

    # DK player+market set (live markets only from dk_prices)
    dk_set = {(r["PLAYERNAME"], r["MARKETTYPENAME"]) for _, r in dk_prices.iterrows()} if not dk_prices.empty else set()

    # Competitor player+market set
    comp_set = {(r["PLAYERNAME"], r["DK_MARKET"]) for _, r in comp_prices.iterrows()}

    # Missing on DK — competitor has it, DK doesn't
    for player, market in sorted(comp_set - dk_set):
        result["missing_on_dk"].append({"PLAYERNAME": player, "MARKET": market, "SOURCE": bookmaker})

    # Missing on competitor — DK has it, competitor doesn't (only for mapped markets)
    dk_mapped_set = {(p, m) for p, m in dk_set if m in dk_markets.values()}
    for player, market in sorted(dk_mapped_set - comp_set):
        result["missing_on_comp"].append({"PLAYERNAME": player, "MARKET": market, "SOURCE": bookmaker})

    # Price gaps — same line, different price (implied prob diff ≥ threshold)
    # Line differences are shown separately to avoid cluttering price gaps
    for _, crow in comp_prices.iterrows():
        key = (crow["PLAYERNAME"], crow["DK_MARKET"], crow["SIDE"])
        if key not in dk_lookup:
            continue
        dk_line, dk_american = dk_lookup[key]
        comp_american = crow["AMERICAN_ODDS"]
        comp_line     = crow["LINE"]

        line_diff = abs(float(dk_line or 0) - float(comp_line or 0)) if dk_line and comp_line else 0

        # Use implied probability difference — scales correctly for long shots
        dk_prob   = american_to_prob(dk_american)
        comp_prob = american_to_prob(comp_american)
        prob_diff = abs(dk_prob - comp_prob)

        # Only flag same-line price differences; skip if lines diverge
        if line_diff < LINE_DIFF_THRESHOLD and prob_diff >= PRICE_DIFF_THRESHOLD:
            result["price_gaps"].append({
                "PLAYERNAME":   crow["PLAYERNAME"],
                "MARKET":       crow["DK_MARKET"],
                "SIDE":         crow["SIDE"],
                "LINE":         dk_line,
                "DK_ODDS":      dk_american,
                "COMP_ODDS":    comp_american,
                "PROB_DIFF":    round(prob_diff * 100, 1),  # as percentage points
                "SOURCE":       bookmaker,
            })
        elif line_diff >= LINE_DIFF_THRESHOLD:
            result["line_diffs"].append({
                "PLAYERNAME":   crow["PLAYERNAME"],
                "MARKET":       crow["DK_MARKET"],
                "SIDE":         crow["SIDE"],
                "DK_LINE":      dk_line,
                "DK_ODDS":      dk_american,
                "COMP_LINE":    comp_line,
                "COMP_ODDS":    comp_american,
                "LINE_DIFF":    line_diff,
                "SOURCE":       bookmaker,
            })

    # Sort price gaps by implied prob difference
    result["price_gaps"].sort(key=lambda x: -x["PROB_DIFF"])
    result["line_diffs"].sort(key=lambda x: (-x["LINE_DIFF"], x["PLAYERNAME"]))

    # Arb detection: DK Over + comp Under (or vice versa) combined implied prob < 100%
    for _, crow in comp_prices.iterrows():
        player = crow["PLAYERNAME"]
        market = crow["DK_MARKET"]
        comp_side = crow["SIDE"]        # Over or Under
        opp_side  = "Under" if comp_side == "Over" else "Over"
        comp_line = crow["LINE"]
        comp_prob = american_to_prob(crow["AMERICAN_ODDS"])

        # Find the opposite side on DK at same line
        dk_key = (player, market, opp_side)
        if dk_key not in dk_lookup:
            continue
        dk_line, dk_american = dk_lookup[dk_key]
        # Lines must match for a true arb
        if comp_line and dk_line and abs(float(comp_line) - float(dk_line)) >= 0.5:
            continue
        dk_prob   = american_to_prob(dk_american)
        total_imp = dk_prob + comp_prob
        if total_imp < 0.98:  # require ≥2% edge to filter out data lag noise
            profit_pct = round((1 - total_imp) * 100, 2)
            result["arbs"].append({
                "PLAYERNAME":  player,
                "MARKET":      market,
                "LINE":        dk_line or comp_line,
                "DK_SIDE":     opp_side,
                "DK_ODDS":     dk_american,
                "COMP_SIDE":   comp_side,
                "COMP_ODDS":   crow["AMERICAN_ODDS"],
                "PROFIT_PCT":  profit_pct,
                "SOURCE":      bookmaker,
            })

    result["arbs"].sort(key=lambda x: -x["PROFIT_PCT"])
    return result

# ── Build helpers ─────────────────────────────────────────────────────────────

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
            bl = bl[bl["MARKETTYPENAME"].isin(MLB_ALLOWED_MARKETS)]
        if is_wnba:
            bl = bl[~bl["MARKETTYPENAME"].isin(WNBA_EXCLUDED_MARKETS)]
        prop_players = bl[bl["GROUP"].isin(["Balanced", "Milestones"])]["PLAYERNAME"].unique()
        if not current.empty:
            individual = current[
                current["MARKETTYPENAME"].apply(classify_market).isin(["Balanced", "Milestones"])
            ]["PLAYERNAME"].unique()
            players_in_event = set(individual)
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

    if is_mlb and player_info is not None and not current.empty:
        covered = {r["PLAYERNAME"] for r in rows}
        roster_players = set(player_info["PLAYERNAME"].tolist())
        for player in roster_players - covered:
            player_current = current[current["PLAYERNAME"] == player]
            for _, crow in player_current.iterrows():
                grp = classify_market(crow["MARKETTYPENAME"])
                if grp == "Exclude" or crow["MARKETTYPENAME"] not in MLB_ALLOWED_MARKETS:
                    continue
                status = "LIVE" if crow["IS_LIVE"] == 1 else "REMOVED"
                rows.append({"PLAYERNAME": player, "MARKET": crow["MARKETTYPENAME"],
                              "GROUP": grp, "LAST_GAME": "roster", "STATUS": status})

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    SCORER_MARKETS = {"1st Points Scorer", "Player First Field Goal Made Type"}
    individual = df[~df["MARKET"].isin(SCORER_MARKETS) & df["GROUP"].isin(["Balanced", "Milestones"])]
    if not individual.empty:
        all_removed = individual.groupby("PLAYERNAME")["STATUS"].apply(lambda s: (s == "REMOVED").all())
        keep = set(all_removed[~all_removed].index)
        players_without_individual = set(df["PLAYERNAME"].unique()) - set(individual["PLAYERNAME"].unique())
        df = df[df["PLAYERNAME"].isin(keep | players_without_individual)]
    return df

def compute_group_pcts(baselines: pd.DataFrame, current: pd.DataFrame, league_name: str) -> dict:
    df = build_status_df(baselines, current, league_name)
    if df.empty:
        return {}
    result = {}
    for grp in DEFAULT_GROUPS:  # Only Balanced + Milestones for overview
        grp_df = df[df["GROUP"] == grp]
        if grp_df.empty:
            continue
        result[grp] = (int((grp_df["STATUS"] == "LIVE").sum()), int(len(grp_df)))
    return result

# O/U ↔ Milestone pairing
OU_MILESTONE_PAIRS = {
    "Points O/U": "Points Milestones",
    "Rebounds O/U": "Rebounds Milestones",
    "Assists O/U": "Assists Milestones",
    "Three Pointers Made O/U": "Three Pointers Made Milestones",
    "Points + Rebounds O/U": "Points + Rebounds Milestones",
    "Points + Assists O/U": "Points + Assists Milestones",
    "Rebounds + Assists O/U": "Rebounds + Assists Milestones",
    "Points + Rebounds + Assists O/U": "Points + Rebounds + Assists Milestones",
    "Hits O/U": "Hits Milestones",
    "Strikeouts Thrown O/U": "Strikeouts Thrown Milestones",
    "Earned Runs Allowed O/U": "Earned Runs Allowed Milestones",
    "Hits Allowed O/U": "Hits Allowed Milestones",
    "Total Bases O/U": "Total Bases Milestones",
    "Hits + Runs + RBIs O/U": "Hits + Runs + RBIs Milestones",
    "Runs + RBIs O/U": "Runs + RBIs Milestones",
    "Stolen Bases O/U": "Stolen Bases Milestones",
    "Singles O/U": "Singles Milestones",
    "Triples O/U": "Triples Milestones",
    "Walks Allowed O/U": "Walks Allowed Milestones",
    "Strikeouts O/U": "Strikeouts Milestones",
}

def get_pairing_flags(df: pd.DataFrame) -> tuple:
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()
    status_map = df.groupby(["PLAYERNAME", "MARKET"])["STATUS"].first().to_dict()
    urgent, fyi = [], []
    for player in df["PLAYERNAME"].unique():
        for ou, mile in OU_MILESTONE_PAIRS.items():
            ou_s   = status_map.get((player, ou))
            mile_s = status_map.get((player, mile))
            if ou_s is None and mile_s is None:
                continue
            ou_short   = market_short(ou)
            mile_short = market_short(mile)
            if ou_s == "LIVE" and mile_s != "LIVE":
                urgent.append({"PLAYERNAME": player,
                                "ISSUE": f"{ou_short} ✓ → {mile_short} {mile_s or 'not posted'}"})
            elif mile_s == "LIVE" and ou_s != "LIVE":
                fyi.append({"PLAYERNAME": player,
                             "ISSUE": f"{mile_short} ✓ → {ou_short} {ou_s or 'not posted'}"})
    return (pd.DataFrame(urgent) if urgent else pd.DataFrame(),
            pd.DataFrame(fyi)    if fyi    else pd.DataFrame())

STATUS_COLOR = {"LIVE": "#16a34a", "MISSING": "#dc2626", "REMOVED": "#b45309"}

def market_short(name: str) -> str:
    replacements = [
        ("Points + Rebounds + Assists", "PRA"), ("Points + Rebounds", "P+R"),
        ("Points + Assists", "P+A"), ("Rebounds + Assists", "R+A"),
        ("Three Pointers Made", "3PM"), ("Hits + Runs + RBIs", "H+R+RBI"),
        ("Hits Allowed + Walks Allowed + Earned Runs Allowed", "HA+BB+ER"),
        ("Strikeouts Thrown", "K Thrown"), ("Earned Runs Allowed", "ERA"),
        ("Stolen Bases", "SB"), ("Total Bases", "TB"), ("Points", "Pts"),
        ("Rebounds", "Reb"), ("Assists", "Ast"), ("Strikeouts", "K"),
        ("Triples", "3B"), ("Singles", "1B"), (" O/U", ""), (" Milestones", " Mile"),
        ("Double-Double", "Dbl-Dbl"), ("Triple-Double", "Tri-Dbl"),
        ("1st Points Scorer", "1st Pts"), ("Player First Field Goal Made Type", "1st FG Type"),
        ("1st Batter to Strike Out", "1st K"), ("1st Stolen Base", "1st SB"),
        ("1st Hit", "1st Hit"), ("Hits Allowed (X or Fewer)", "H Allow"), ("Hits Allowed", "HA"),
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

        st.markdown(
            "<div style='display:grid;grid-template-columns:1fr auto;"
            "align-items:center;gap:12px;padding:6px 0;border-bottom:1px solid #1f2937'>"
            "<div><div style='font-size:0.83em;font-weight:500'>" + market + "</div>" + sub + "</div>"
            "<div style='text-align:right;min-width:140px'>"
            "<div style='display:flex;align-items:center;gap:8px;justify-content:flex-end'>"
            "<div style='width:80px;background:#374151;border-radius:3px;height:7px'>"
            "<div style='width:" + str(bar_w) + "%;background:" + color + ";height:7px;border-radius:3px'></div>"
            "</div>"
            "<span style='font-size:0.8em;font-weight:700;color:" + color + ";min-width:38px;text-align:right'>"
            + str(live) + "/" + str(total) + "</span></div></div></div>",
            unsafe_allow_html=True,
        )

# ── Render: player cards ──────────────────────────────────────────────────────

def render_player_cards(df: pd.DataFrame, player_info: pd.DataFrame, sport_name: str,
                        issues_only: bool = False):
    info = player_info.set_index("PLAYERNAME")
    missing_count = df[df["STATUS"] == "MISSING"].groupby("PLAYERNAME").size().to_dict()

    def pos_order(p):
        pos = str(info.loc[p, "POSITION"]).lower() if p in info.index else ""
        if "baseball" in sport_name.lower():
            if "pitcher" in pos:  return 0
            if "two-way" in pos:  return 1
            return 2
        return 0

    players = sorted(df["PLAYERNAME"].unique(), key=lambda p: (
        int(info.loc[p, "TEAM_ORDER"]) if p in info.index else 99,
        -missing_count.get(p, 0),
        pos_order(p), p,
    ))

    current_team = None
    cols_per_row = 4
    card_buffer  = []
    is_baseball  = "baseball" in sport_name.lower() or "mlb" in sport_name.lower()

    def build_card_html(player, player_df):
        markets   = player_df.sort_values(["GROUP", "MARKET"])
        n_live    = int((markets["STATUS"] == "LIVE").sum())
        n_missing = int((markets["STATUS"] == "MISSING").sum())
        n_removed = int((markets["STATUS"] == "REMOVED").sum())
        n_total   = int(len(markets))
        pct       = n_live / n_total if n_total else 0
        border    = "#dc2626" if n_missing > 0 else ("#b45309" if n_removed > 0 else "#16a34a")
        bar_fill  = str(int(pct * 56))
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
                    "<div style='margin-top:3px'>" + pills + "</div></div>"
                )

        bar_html = (
            "<div style='width:56px;height:3px;background:#374151;border-radius:2px;margin-top:3px'>"
            "<div style='width:" + bar_fill + "px;height:3px;background:" + border + ";border-radius:2px'></div></div>"
        )
        return (
            "<div style='border:1px solid " + border + ";border-radius:8px;"
            "padding:11px 13px 9px;margin-bottom:8px;background:#0f172a'>"
            "<div style='display:flex;justify-content:space-between;align-items:flex-start'>"
            "<span style='font-weight:700;font-size:0.88em;line-height:1.3'>" + player + "</span>"
            "<div style='text-align:right;flex-shrink:0;margin-left:8px'>"
            "<span style='font-size:0.75em;color:" + border + ";font-weight:700'>"
            + str(n_live) + "/" + str(n_total) + "</span>" + bar_html + "</div></div>"
            + sections + "</div>"
        )

    def flush(cards):
        for i in range(0, len(cards), cols_per_row):
            chunk = cards[i:i + cols_per_row]
            cols  = st.columns(cols_per_row)
            for col, (player, player_df) in zip(cols, chunk):
                col.markdown(build_card_html(player, player_df), unsafe_allow_html=True)

    hidden_count = 0
    for player in players:
        player_df = df[df["PLAYERNAME"] == player]
        if is_baseball and (player_df["STATUS"] == "MISSING").all():
            continue
        has_issue = not ((player_df["STATUS"] == "LIVE").all())
        if issues_only and not has_issue:
            hidden_count += 1
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
    if hidden_count:
        st.caption(f"{hidden_count} player{'s' if hidden_count > 1 else ''} with all markets live hidden.")

@st.fragment
def render_competitor_section(event_id: str, league_name: str, player_info: pd.DataFrame):
    """Runs as an independent fragment so the Odds API call doesn't block the main page."""
    is_wnba   = "wnba" in league_name.lower()
    bookmaker = "FanDuel" if is_wnba else "Fanatics"

    home_team = player_info[player_info["TEAM_ORDER"] == 0]["TEAM"].iloc[0] if not player_info.empty else ""
    away_team = player_info[player_info["TEAM_ORDER"] == 1]["TEAM"].iloc[0] if not player_info.empty else ""

    try:
        dk_prices   = get_dk_prices(event_id)
        comp_prices = get_competitor_prices(event_id, league_name, home_team, away_team)
        comparison  = build_competitor_comparison(dk_prices, comp_prices, league_name)
    except Exception:
        comparison = {"missing_on_dk": [], "missing_on_comp": [], "price_gaps": [], "line_diffs": [], "arbs": []}

    n_missing_dk = len(comparison["missing_on_dk"])
    n_price_gaps = len(comparison["price_gaps"])
    n_line_diffs = len(comparison["line_diffs"])
    n_arbs       = len(comparison["arbs"])

    if n_missing_dk == 0 and n_price_gaps == 0 and n_line_diffs == 0 and n_arbs == 0:
        return

    parts = []
    if n_arbs:        parts.append(f"⚡ {n_arbs} arb{'s' if n_arbs > 1 else ''}")
    if n_missing_dk:  parts.append(f"🚨 {n_missing_dk} we're missing")
    if n_price_gaps:  parts.append(f"{n_price_gaps} price gaps")
    if n_line_diffs:  parts.append(f"{n_line_diffs} line diffs")
    expander_title = f"🔍 vs {bookmaker}  —  " + "  ·  ".join(parts)

    def row_html(player, market, detail, left_odds, right_label, right_odds, badge="", badge_color="#fbbf24"):
        mkt = market_short(market)
        badge_span = (f"<span style='color:{badge_color};font-weight:700;margin-left:6px'>{badge}</span>"
                      if badge else "")
        return (
            "<div style='display:grid;grid-template-columns:160px 180px 1fr;gap:10px;"
            "align-items:center;padding:5px 0;border-bottom:1px solid #1e293b;font-size:0.83em'>"
            "<span style='color:#e5e7eb;font-weight:700'>" + player + "</span>"
            "<span style='color:#9ca3af'>" + mkt + "  " + detail + "</span>"
            "<span style='display:flex;gap:8px;align-items:center'>"
            "<span style='color:#16a34a'>DK " + left_odds + "</span>"
            "<span style='color:#4b5563'>vs</span>"
            "<span style='color:#e5e7eb'>" + right_label + " " + right_odds + "</span>"
            + badge_span +
            "</span>"
            "</div>"
        )

    with st.expander(expander_title, expanded=True):
        st.caption("DK prices from Snowflake — may lag 1-2 min. Arbs require ≥2% edge to filter data lag noise.")

        # ── Arbs ──────────────────────────────────────────────────────────────
        if comparison["arbs"]:
            st.markdown(
                "<div style='font-size:0.72em;text-transform:uppercase;letter-spacing:0.08em;"
                "color:#a78bfa;font-weight:700;margin-bottom:6px'>⚡ Arb opportunities</div>",
                unsafe_allow_html=True,
            )
            for item in comparison["arbs"]:
                line_str = f"@ {item['LINE']}" if item["LINE"] else ""
                dk_str   = f"{item['DK_ODDS']:+d} {item['DK_SIDE']}"
                comp_str = f"{item['COMP_ODDS']:+d} {item['COMP_SIDE']}"
                st.markdown(
                    row_html(
                        item["PLAYERNAME"], item["MARKET"], line_str,
                        dk_str, bookmaker, comp_str,
                        badge=f"+{item['PROFIT_PCT']}% profit",
                        badge_color="#a78bfa"
                    ),
                    unsafe_allow_html=True,
                )
            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        # ── Missing on DK ─────────────────────────────────────────────────────
        if comparison["missing_on_dk"]:
            st.markdown(
                "<div style='font-size:0.72em;text-transform:uppercase;letter-spacing:0.08em;"
                "color:#f87171;font-weight:700;margin-bottom:6px'>"
                "🚨 " + bookmaker + " has, DK doesn't</div>",
                unsafe_allow_html=True,
            )
            for item in sorted(comparison["missing_on_dk"], key=lambda x: (x["MARKET"], x["PLAYERNAME"])):
                st.markdown(
                    "<div style='display:grid;grid-template-columns:160px 1fr;gap:10px;"
                    "align-items:center;padding:5px 0;border-bottom:1px solid #1e293b;font-size:0.83em'>"
                    "<span style='color:#fca5a5;font-weight:700'>" + item["PLAYERNAME"] + "</span>"
                    "<span style='color:#f87171'>" + market_short(item["MARKET"]) + "</span>"
                    "</div>",
                    unsafe_allow_html=True,
                )
            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        # ── Price gaps ────────────────────────────────────────────────────────
        if comparison["price_gaps"]:
            st.markdown(
                "<div style='font-size:0.72em;text-transform:uppercase;letter-spacing:0.08em;"
                "color:#fbbf24;font-weight:700;margin-bottom:6px'>Price gaps ≥4%</div>",
                unsafe_allow_html=True,
            )
            for item in comparison["price_gaps"]:
                line_str   = f"@ {item['LINE']}" if item["LINE"] else ""
                diff_color = "#f87171" if item["PROB_DIFF"] >= 6 else "#fbbf24"
                st.markdown(
                    row_html(
                        item["PLAYERNAME"], item["MARKET"],
                        item["SIDE"] + " " + line_str,
                        f"{item['DK_ODDS']:+d}", bookmaker, f"{item['COMP_ODDS']:+d}",
                        badge=f"({item['PROB_DIFF']}%)",
                        badge_color=diff_color
                    ),
                    unsafe_allow_html=True,
                )
            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        # ── Line differences ──────────────────────────────────────────────────
        if comparison["line_diffs"]:
            st.markdown(
                "<div style='font-size:0.72em;text-transform:uppercase;letter-spacing:0.08em;"
                "color:#6b7280;font-weight:700;margin-bottom:6px'>Different lines</div>",
                unsafe_allow_html=True,
            )
            for item in comparison["line_diffs"]:
                dk_detail   = f"{item['DK_SIDE']} @ {item['DK_LINE']}" if "DK_SIDE" in item else f"@ {item['DK_LINE']}"
                comp_detail = f"@ {item['COMP_LINE']}"
                st.markdown(
                    "<div style='display:grid;grid-template-columns:160px 180px 1fr;gap:10px;"
                    "align-items:center;padding:4px 0;border-bottom:1px solid #1e293b;"
                    "font-size:0.8em;color:#6b7280'>"
                    "<span>" + item["PLAYERNAME"] + "</span>"
                    "<span>" + market_short(item["MARKET"]) + " " + item["SIDE"] + "</span>"
                    "<span>DK " + str(item["DK_LINE"]) + " " + f"{item['DK_ODDS']:+d}" +
                    "  vs  " + bookmaker + " " + str(item["COMP_LINE"]) + " " + f"{item['COMP_ODDS']:+d}" + "</span>"
                    "</div>",
                    unsafe_allow_html=True,
                )

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

    urgent_flags, fyi_flags = get_pairing_flags(df)

    # ── Summary — Missing is the hero ────────────────────────────────────────
    live    = int((df["STATUS"] == "LIVE").sum())
    missing = int((df["STATUS"] == "MISSING").sum())
    removed = int((df["STATUS"] == "REMOVED").sum())
    total   = int(len(df))

    m1, m2, m3, m4, m5 = st.columns([2, 1, 1, 1, 1])
    # Missing as hero — large red number
    miss_color = "#dc2626" if missing > 0 else "#16a34a"
    m1.markdown(
        "<div>"
        "<div style='font-size:0.75em;color:#9ca3af;font-weight:600;text-transform:uppercase;"
        "letter-spacing:0.05em'>Missing</div>"
        "<div style='font-size:2.2em;font-weight:800;color:" + miss_color + ";line-height:1.1'>"
        + str(missing) + "</div>"
        "<div style='font-size:0.75em;color:#6b7280'>of " + str(total) + " expected</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    m2.metric("Live", live)
    # Only show Removed if >0; label makes clear these are pulled markets not gaps
    if removed > 0:
        m3.metric("Removed (pulled)", removed)
    if len(urgent_flags) > 0:
        m4.metric("Line/Mile gaps", len(urgent_flags))
    m5.metric("Total", total)

    # ── Activity feed (most urgent — what just changed) ───────────────────────
    try:
        activity = get_activity_feed(event_row["EVENTID"])
    except Exception:
        activity = pd.DataFrame()

    if not activity.empty:
        PT = pytz.timezone("America/Los_Angeles")
        activity["CHANGED_AT_PT"] = pd.to_datetime(activity["CHANGED_AT"]).dt.tz_localize("UTC").dt.tz_convert(PT)
        with st.expander(f"📋 Recent activity — last 30 min ({len(activity)} changes)", expanded=True):
            for _, row in activity.iterrows():
                action      = str(row["ACTION"])
                action_color = "#16a34a" if action == "PUBLISHED" else "#dc2626"
                ts          = row["CHANGED_AT_PT"].strftime("%I:%M %p")
                players     = str(row["PLAYERS"]) if row["PLAYERS"] else ""
                # Color market name by group importance
                mkt_grp     = classify_market(str(row["MARKETTYPENAME"]))
                mkt_color   = "#e5e7eb" if mkt_grp in ("Balanced", "Milestones") else "#6b7280"
                st.markdown(
                    "<div style='padding:5px 0;border-bottom:1px solid #1e293b;font-size:0.82em'>"
                    "<div style='display:flex;gap:12px;align-items:center'>"
                    "<span style='color:#6b7280;min-width:60px'>" + ts + "</span>"
                    "<span style='color:" + action_color + ";font-weight:700;min-width:80px'>" + action + "</span>"
                    "<span style='color:" + mkt_color + ";font-weight:500'>" + str(row["MARKETTYPENAME"]) + "</span>"
                    "</div>"
                    "<div style='color:#9ca3af;font-size:0.9em;margin-top:2px;padding-left:152px'>" + players + "</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )

    # ── Pairing flags ─────────────────────────────────────────────────────────
    if not urgent_flags.empty:
        with st.expander(f"⚠️ Line posted, Milestone missing ({len(urgent_flags)})", expanded=True):
            for _, row in urgent_flags.iterrows():
                st.markdown(
                    "<div style='padding:4px 0;font-size:0.85em'>"
                    "<span style='color:#f87171;font-weight:700'>" + row["PLAYERNAME"] + "</span>"
                    "  —  <span style='color:#e5e7eb'>" + row["ISSUE"] + "</span></div>",
                    unsafe_allow_html=True,
                )

    if not fyi_flags.empty:
        with st.expander(f"ℹ️ Milestone posted, Line missing ({len(fyi_flags)})", expanded=False):
            for _, row in fyi_flags.iterrows():
                st.markdown(
                    "<div style='padding:4px 0;font-size:0.85em'>"
                    "<span style='color:#fbbf24;font-weight:700'>" + row["PLAYERNAME"] + "</span>"
                    "  —  <span style='color:#9ca3af'>" + row["ISSUE"] + "</span></div>",
                    unsafe_allow_html=True,
                )

    # ── Competitor comparison (loads after main content) ─────────────────────
    render_competitor_section(event_row["EVENTID"], league_name, player_info)

    st.divider()

    # ── Group tabs — Balanced + Milestones by default, Team/H2H hidden ────────
    all_groups = [g for g in GROUP_ORDER if g in df["GROUP"].unique()]

    # Tab labels: live/total + status badge
    tab_labels = []
    for g in all_groups:
        grp_df   = df[df["GROUP"] == g]
        n_live   = int((grp_df["STATUS"] == "LIVE").sum())
        n_miss   = int((grp_df["STATUS"] == "MISSING").sum())
        n_rem    = int((grp_df["STATUS"] == "REMOVED").sum())
        n_total  = int(len(grp_df))
        if n_miss:
            badge = "  ❌"
        elif n_rem:
            badge = "  🟡"
        else:
            badge = "  ✅"
        label = g + f"{badge} {n_live}/{n_total}"
        tab_labels.append(label)

    # Reorder so default groups come first
    ordered_groups = [g for g in DETAIL_DEFAULT_GROUPS if g in all_groups] + \
                     [g for g in all_groups if g not in DETAIL_DEFAULT_GROUPS]
    ordered_labels = []
    for g in ordered_groups:
        idx = all_groups.index(g)
        ordered_labels.append(tab_labels[idx])

    tabs = st.tabs(ordered_labels)
    for tab, grp in zip(tabs, ordered_groups):
        with tab:
            grp_df = df[df["GROUP"] == grp]
            if grp_df.empty:
                st.caption("No markets in this group.")
                continue

            st.markdown("##### Market Completion")
            render_market_completion(grp_df, league_name)

            st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

            # Players header + issues-only toggle on same line
            p_col, t_col = st.columns([3, 2])
            p_col.markdown("##### Players")
            issues_only = t_col.toggle("Issues only", value=False, key=f"issues_{grp}")
            render_player_cards(grp_df, player_info, league_name, issues_only)

# ── Page: Overview ────────────────────────────────────────────────────────────

def show_overview(events_df, league_name):
    st_autorefresh(interval=60_000, key="overview_refresh")

    if events_df.empty:
        st.warning(f"No upcoming events with player props found for {league_name}.")
        return

    event_ids = tuple(events_df["EVENTID"].tolist())
    PT        = pytz.timezone("America/Los_Angeles")
    now_pt    = pd.Timestamp.utcnow().tz_convert(PT)

    try:
        bulk_baselines = get_bulk_baselines(event_ids)
        bulk_current   = get_bulk_current_markets(event_ids)
    except Exception as e:
        st.error(f"Failed to load market stats: {e}")
        return

    all_pcts = {}
    for eid in event_ids:
        bl  = bulk_baselines[bulk_baselines["EVENTID"] == eid].drop(columns="EVENTID")
        cur = bulk_current[bulk_current["EVENTID"] == eid].drop(columns="EVENTID")
        try:
            all_pcts[eid] = compute_group_pcts(bl, cur, league_name)
        except Exception:
            all_pcts[eid] = {}

    for _, ev in events_df.iterrows():
        eid          = ev["EVENTID"]
        countdown    = ev["countdown"]
        start_str    = ev["STARTEVENTDATE"].strftime("%b %d, %I:%M %p PT")
        pcts_raw     = all_pcts.get(eid, {})
        group_pcts   = {g: v[0] / v[1] if v[1] else 0.0 for g, v in pcts_raw.items()}
        group_counts = pcts_raw

        # Overall % = Balanced + Milestones combined
        total_live  = sum(v[0] for v in pcts_raw.values())
        total_exp   = sum(v[1] for v in pcts_raw.values())
        overall_pct = total_live / total_exp if total_exp else 0.0
        overall_color = pct_color(overall_pct)

        secs_left = int((ev["STARTEVENTDATE"] - now_pt).total_seconds())
        if secs_left <= 0:
            cd_color, cd_bg = "#4ade80", "#052e16"
            card_border = "#052e16"
        elif secs_left < 3600:
            cd_color, cd_bg = "#f87171", "#2d0a0a"
            card_border = "#7f1d1d" if overall_pct < 0.9 else "#1e293b"
        elif secs_left < 10800:
            cd_color, cd_bg = "#fbbf24", "#2d1b00"
            card_border = "#1e293b"
        else:
            cd_color, cd_bg = "#93c5fd", "#0f172a"
            card_border = "#1e293b"

        # Colored left border by overall health
        if overall_pct < 0.8:
            card_border = "#7f1d1d"
        elif overall_pct < 0.95:
            card_border = "#78350f"

        # Whole card is clickable via a button overlay
        with st.container(border=False):
            st.markdown(
                "<div style='border:1px solid #374151;border-left:4px solid " + card_border + ";"
                "border-radius:8px;padding:0;margin-bottom:8px;overflow:hidden'>",
                unsafe_allow_html=True,
            )
            col_cd, col_info, col_stats, col_btn = st.columns([1, 3, 3, 1])

            with col_cd:
                st.markdown(
                    "<div style='background:" + cd_bg + ";padding:14px 8px;text-align:center;"
                    "height:100%;display:flex;flex-direction:column;justify-content:center;align-items:center'>"
                    "<span style='font-size:1.5em;font-weight:800;color:" + cd_color + ";line-height:1.1'>"
                    + countdown + "</span>"
                    "<span style='font-size:0.62em;color:#6b7280;margin-top:4px'>" + start_str + "</span>"
                    "</div>",
                    unsafe_allow_html=True,
                )

            with col_info:
                st.markdown(
                    "<div style='padding:14px 0 14px 8px'>"
                    "<div style='font-size:1.05em;font-weight:700'>" + ev["EVENTNAME"] + "</div>"
                    "<div style='font-size:0.78em;color:#6b7280;margin-top:3px'>" + ev["LEAGUENAME"] + "</div>"
                    # Overall % as a single hero number
                    "<div style='margin-top:8px'>"
                    "<span style='font-size:1.8em;font-weight:800;color:" + overall_color + "'>"
                    + str(int(overall_pct * 100)) + "%</span>"
                    "<span style='font-size:0.75em;color:#6b7280;margin-left:6px'>"
                    + str(total_live) + "/" + str(total_exp) + " props live</span>"
                    "</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )

            with col_stats:
                # Only Balanced + Milestones pills — no Team/H2H
                if group_pcts:
                    pills_html = "<div style='display:flex;flex-wrap:wrap;gap:8px;padding:14px 0'>"
                    for grp in DEFAULT_GROUPS:
                        if grp not in group_pcts:
                            continue
                        pct   = group_pcts[grp]
                        color = pct_color(pct)
                        live_n, total_n = group_counts.get(grp, (0, 0))
                        pills_html += (
                            "<div style='display:flex;flex-direction:column;align-items:center;"
                            "min-width:80px;padding:6px 10px;border-radius:6px;background:#1e293b'>"
                            "<span style='font-size:1.3em;font-weight:800;color:" + color + ";line-height:1.1'>"
                            + str(int(pct * 100)) + "%</span>"
                            "<span style='font-size:0.72em;color:" + color + ";font-weight:600'>"
                            + str(live_n) + "/" + str(total_n) + "</span>"
                            "<span style='font-size:0.62em;color:#6b7280;margin-top:1px'>" + grp + "</span>"
                            "</div>"
                        )
                    pills_html += "</div>"
                    st.markdown(pills_html, unsafe_allow_html=True)

            with col_btn:
                st.markdown("<div style='padding-top:20px;padding-right:8px'>", unsafe_allow_html=True)
                if st.button("View →", key="btn_" + eid, use_container_width=True):
                    st.session_state.selected_event_id = eid
                    st.rerun()
                st.markdown("</div>", unsafe_allow_html=True)

            st.markdown("</div>", unsafe_allow_html=True)

# ── Main ──────────────────────────────────────────────────────────────────────

st.markdown(
    "<h1 style='text-align:center;margin-bottom:4px'>Market Availability</h1>",
    unsafe_allow_html=True,
)

if "selected_league" not in st.session_state:
    st.session_state.selected_league = "WNBA"

btn_col = st.columns([3, 1, 1, 3])
for i, league_name in enumerate(LEAGUES):
    active = st.session_state.selected_league == league_name
    if btn_col[i + 1].button(league_name, type="primary" if active else "secondary",
                              use_container_width=True):
        st.session_state.selected_league   = league_name
        st.session_state.selected_event_id = None

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
