"""
dashboard.py ─ Plotly Dash dashboard for the Funding Rate Arbitrage Bot.

Run standalone (read-only, no bot):  python dashboard.py
Run via main.py (with live bot):     python main.py
"""
import time
import logging
from datetime import datetime

import dash
import dash_bootstrap_components as dbc
from dash import dcc, html, Input, Output, ctx
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd

from database import Database
from config  import Config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Global bot reference (injected by main.py)
# ─────────────────────────────────────────────────────────────────────────────
_bot = None

def inject_bot(bot_instance):
    global _bot
    _bot = bot_instance


# ─────────────────────────────────────────────────────────────────────────────
# App initialisation
# ─────────────────────────────────────────────────────────────────────────────
app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG,
                           "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600&family=Syne:wght@400;700;800&display=swap"],
    title="Funding Arb Bot",
    update_title=None,
)

# ─────────────────────────────────────────────────────────────────────────────
# Reusable style helpers
# ─────────────────────────────────────────────────────────────────────────────
MONO = {"fontFamily": "'JetBrains Mono', monospace"}
SANS = {"fontFamily": "'Syne', sans-serif"}

GREEN  = "#00f5a0"
RED    = "#ff4d6d"
YELLOW = "#ffd166"
BLUE   = "#00b4d8"
GREY   = "#6c757d"
BG     = "#0a0e1a"
CARD   = "#111827"
BORDER = "#1f2d3d"

def _card(title: str, value: str, color: str, icon: str = "") -> dbc.Card:
    return dbc.Card(
        dbc.CardBody([
            html.Div(f"{icon}  {title}",
                     style={**MONO, "fontSize": "11px", "color": GREY,
                            "textTransform": "uppercase", "letterSpacing": "1.5px"}),
            html.Div(value,
                     style={**MONO, "fontSize": "28px", "fontWeight": "600",
                            "color": color, "marginTop": "6px"}),
        ]),
        style={"background": CARD, "border": f"1px solid {color}22",
               "borderRadius": "12px"},
    )


def _badge(label: str, color: str = GREY) -> html.Span:
    return html.Span(label, style={
        "background": f"{color}22", "color": color, "border": f"1px solid {color}55",
        "borderRadius": "4px", "padding": "2px 8px",
        "fontSize": "11px", **MONO,
    })


def _pct_cell(val: float) -> html.Td:
    color = GREEN if val < 0 else RED   # negative funding = good (green)
    return html.Td(f"{val:+.4f}%", style={"color": color, **MONO})


def _secs_to_hms(secs: float) -> str:
    secs = max(0, int(secs))
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# Layout
# ─────────────────────────────────────────────────────────────────────────────
app.layout = html.Div(
    style={"background": BG, "minHeight": "100vh", "padding": "20px 28px",
           "fontFamily": "'Syne', sans-serif"},
    children=[

        # ── Header ──────────────────────────────────────────────────────────
        dbc.Row([
            dbc.Col([
                html.H1("⚡ Funding Rate Arbitrage",
                        style={**SANS, "fontWeight": "800", "fontSize": "28px",
                               "color": "white", "marginBottom": "2px"}),
                html.Div("Binance USD-M Perpetuals  ·  Delta-Neutral  ·  1× Leverage",
                         style={**MONO, "fontSize": "12px", "color": GREY}),
            ], width=8),
            dbc.Col([
                html.Div(id="hdr-status",  style={"textAlign": "right", "marginBottom": "4px"}),
                html.Div(id="hdr-updated", style={"textAlign": "right",
                                                   **MONO, "fontSize": "11px", "color": GREY}),
            ], width=4, style={"alignSelf": "center"}),
        ], className="mb-4"),

        # ── Stat cards ──────────────────────────────────────────────────────
        dbc.Row(id="stat-cards", className="mb-4 g-3"),

        # ── Opportunities & Active split ─────────────────────────────────────
        dbc.Row([
            dbc.Col([
                html.Div("TOP 10 NEXT FUNDING RATES",
                         style={**MONO, "fontSize": "11px", "color": BLUE,
                                "letterSpacing": "2px", "marginBottom": "10px"}),
                html.Div(id="opp-table"),
            ], width=7),
            dbc.Col([
                html.Div("ACTIVE POSITIONS",
                         style={**MONO, "fontSize": "11px", "color": GREEN,
                                "letterSpacing": "2px", "marginBottom": "10px"}),
                html.Div(id="pos-table"),
            ], width=5),
        ], className="mb-4"),

        # ── Spread chart ────────────────────────────────────────────────────
        dbc.Row([
            dbc.Col([
                html.Div("SPREAD HISTORY  (last 24 h)",
                         style={**MONO, "fontSize": "11px", "color": YELLOW,
                                "letterSpacing": "2px", "marginBottom": "10px"}),
                dcc.Graph(id="spread-chart", config={"displayModeBar": False}),
            ]),
        ], className="mb-4"),

        # ── History + Logs ───────────────────────────────────────────────────
        dbc.Row([
            dbc.Col([
                html.Div("TRADE HISTORY",
                         style={**MONO, "fontSize": "11px", "color": GREY,
                                "letterSpacing": "2px", "marginBottom": "10px"}),
                html.Div(id="hist-table"),
            ], width=8),
            dbc.Col([
                html.Div("RECENT LOGS",
                         style={**MONO, "fontSize": "11px", "color": GREY,
                                "letterSpacing": "2px", "marginBottom": "10px"}),
                html.Div(id="log-box",
                         style={"background": CARD, "borderRadius": "10px",
                                "padding": "12px", "maxHeight": "360px",
                                "overflowY": "auto",
                                "border": f"1px solid {BORDER}"}),
            ], width=4),
        ]),

        # ── Auto-refresh ─────────────────────────────────────────────────────
        dcc.Interval(id="ticker", interval=Config.DASHBOARD_REFRESH_MS,
                     n_intervals=0),
    ],
)


# ─────────────────────────────────────────────────────────────────────────────
# Callbacks
# ─────────────────────────────────────────────────────────────────────────────
@app.callback(
    [
        Output("hdr-status",    "children"),
        Output("hdr-updated",   "children"),
        Output("stat-cards",    "children"),
        Output("opp-table",     "children"),
        Output("pos-table",     "children"),
        Output("spread-chart",  "figure"),
        Output("hist-table",    "children"),
        Output("log-box",       "children"),
    ],
    Input("ticker", "n_intervals"),
)
def refresh(_n):
    db   = Database()
    now  = time.time()
    dt   = datetime.now().strftime("%H:%M:%S")

    # ── Bot status badge ──────────────────────────────────────────────────────
    running = _bot is not None and _bot.is_running
    status_badge = _badge(
        "● LIVE" if running else "● OFFLINE",
        GREEN if running else RED,
    )

    # ── Balances ──────────────────────────────────────────────────────────────
    balances = (_bot.balances if _bot else {})
    spot_usdt     = balances.get("spot",         0.0)
    futures_usdt  = balances.get("futures",      0.0)
    total_usdt    = balances.get("total",        0.0)
    per_pos_usdt  = balances.get("per_position", 0.0)

    stats = db.get_stats()

    stat_cards = [
        dbc.Col(_card("Spot USDT",       f"${spot_usdt:,.2f}",    GREEN,  "💰"), lg=3, md=6),
        dbc.Col(_card("Futures USDT",    f"${futures_usdt:,.2f}", BLUE,   "📊"), lg=3, md=6),
        dbc.Col(_card("Per Position",    f"${per_pos_usdt:,.2f}", YELLOW, "🎯"), lg=3, md=6),
        dbc.Col(_card("Total Realised P&L", f"${stats['total_pnl']:+.4f}",
                      GREEN if stats["total_pnl"] >= 0 else RED,          "💹"), lg=3, md=6),
    ]

    # ── Opportunities table ───────────────────────────────────────────────────
    fund_rows = _bot.get_live_funding_table() if _bot else []
    open_syms = {p["symbol"] for p in db.get_open_positions()}

    opp_header = html.Tr([
        html.Th(h, style={**MONO, "fontSize": "10px", "color": GREY,
                          "borderBottom": f"1px solid {BORDER}", "padding": "6px 8px"})
        for h in ["COIN", "NEXT RATE", "APR", "SPREAD", "TIME TO FUND", "STATUS"]
    ])

    opp_body_rows = []
    for d in fund_rows:
        secs = d["secs_to_funding"]
        urgent = secs < Config.ENTRY_BEFORE_SECONDS + 60  # turning yellow soon

        is_open = d["symbol"] in open_syms
        status_cell = (
            _badge("OPEN", GREEN) if is_open
            else (_badge("ENTERING SOON", YELLOW) if urgent
                  else _badge("WATCHING", GREY))
        )

        opp_body_rows.append(html.Tr([
            html.Td(d["base"],
                    style={**MONO, "fontSize": "13px", "fontWeight": "600",
                           "color": "white", "padding": "7px 8px"}),
            _pct_cell(d["funding_rate_pct"]),
            html.Td(f"{d['apr_pct']:+.1f}%",
                    style={"color": GREEN if d["apr_pct"] < 0 else RED, **MONO}),
            html.Td(f"{d['spread_pct']:+.4f}%", style={**MONO, "color": BLUE}),
            html.Td(_secs_to_hms(secs),
                    style={**MONO,
                           "color": YELLOW if urgent else "white",
                           "fontWeight": "600" if urgent else "400"}),
            html.Td(status_cell),
        ], style={"borderBottom": f"1px solid {BORDER}"}))

    opp_table = html.Table(
        [html.Thead(opp_header), html.Tbody(opp_body_rows)],
        style={"width": "100%", "borderCollapse": "collapse",
               "background": CARD, "borderRadius": "10px", "overflow": "hidden"},
    )

    # ── Active positions table ────────────────────────────────────────────────
    positions = db.get_open_positions()

    pos_header = html.Tr([
        html.Th(h, style={**MONO, "fontSize": "10px", "color": GREY,
                          "borderBottom": f"1px solid {BORDER}", "padding": "6px 8px"})
        for h in ["COIN", "SIZE", "ENTRY Δ", "NOW Δ", "FUNDING", "HOLD"]
    ])

    pos_body_rows = []
    for p in positions:
        entry_spread   = p.get("entry_spread", 0)
        current_spread = p.get("current_spread", entry_spread)
        spread_delta   = current_spread - entry_spread    # negative = compressing (good)
        funding        = p.get("funding_collected", 0)

        min_hold_end   = p["next_funding_time"] + Config.MIN_HOLD_EXTRA_SECONDS
        hold_done      = now >= min_hold_end
        hold_remaining = max(0, min_hold_end - now)

        pos_body_rows.append(html.Tr([
            html.Td(p["symbol"].replace("USDT", ""),
                    style={**MONO, "color": "white", "fontWeight": "600",
                           "padding": "7px 8px"}),
            html.Td(f"${p['position_usdt']:,.0f}",   style={**MONO, "color": GREY}),
            html.Td(f"{entry_spread:+.3f}%",          style={**MONO, "color": BLUE}),
            html.Td(f"{current_spread:+.3f}%",
                    style={**MONO,
                           "color": GREEN if spread_delta < 0 else RED}),
            html.Td(f"${funding:.4f}",                style={**MONO, "color": GREEN}),
            html.Td(
                _badge("✓ FREE", GREEN) if hold_done
                else _badge(f"⏳ {_secs_to_hms(hold_remaining)}", YELLOW)
            ),
        ], style={"borderBottom": f"1px solid {BORDER}"}))

    if not pos_body_rows:
        pos_body_rows = [html.Tr([
            html.Td("No active positions", colSpan=6,
                    style={"textAlign": "center", "color": GREY, **MONO,
                           "padding": "20px"})
        ])]

    pos_table = html.Table(
        [html.Thead(pos_header), html.Tbody(pos_body_rows)],
        style={"width": "100%", "borderCollapse": "collapse",
               "background": CARD, "borderRadius": "10px", "overflow": "hidden"},
    )

    # ── Spread chart ──────────────────────────────────────────────────────────
    spread_data = db.get_spread_history(hours=24)
    fig = go.Figure()

    if spread_data:
        df = pd.DataFrame(spread_data)
        for sym in df["symbol"].unique():
            sub = df[df["symbol"] == sym]
            fig.add_trace(go.Scatter(
                x=sub["dt"], y=sub["spread"],
                name=sym.replace("USDT", ""),
                mode="lines",
                line={"width": 1.5},
            ))

    fig.add_hline(y=Config.EXIT_SPREAD_THRESHOLD,
                  line_dash="dash", line_color=GREEN, line_width=1,
                  annotation_text=f"Exit threshold ({Config.EXIT_SPREAD_THRESHOLD}%)",
                  annotation_font_color=GREEN)
    fig.add_hline(y=0, line_color=YELLOW, line_width=0.6,
                  annotation_text="0%", annotation_font_color=YELLOW)

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor=CARD,
        margin={"l": 0, "r": 0, "t": 10, "b": 0},
        yaxis={"title": "Spread %", "gridcolor": BORDER},
        xaxis={"title": "", "gridcolor": BORDER},
        legend={"bgcolor": "rgba(0,0,0,0)", "font": {"family": "JetBrains Mono"}},
        height=280,
        font={"family": "JetBrains Mono"},
    )

    # ── History table ─────────────────────────────────────────────────────────
    history = db.get_closed_positions(limit=20)

    hist_header = html.Tr([
        html.Th(h, style={**MONO, "fontSize": "10px", "color": GREY,
                          "borderBottom": f"1px solid {BORDER}", "padding": "6px 8px"})
        for h in ["COIN", "ENTRY", "EXIT", "ENTRY Δ", "EXIT Δ",
                  "FUNDING", "SPREAD P&L", "TOTAL P&L", "REASON"]
    ])

    hist_rows = []
    for h in history:
        pnl_color = GREEN if h["total_pnl"] >= 0 else RED
        hist_rows.append(html.Tr([
            html.Td(h["symbol"].replace("USDT", ""),
                    style={**MONO, "fontWeight": "600", "padding": "6px 8px"}),
            html.Td(datetime.fromtimestamp(h["entry_time"]).strftime("%m/%d %H:%M"),
                    style={**MONO, "color": GREY, "fontSize": "11px"}),
            html.Td(datetime.fromtimestamp(h["exit_time"]).strftime("%m/%d %H:%M") if h.get("exit_time") else "—",
                    style={**MONO, "color": GREY, "fontSize": "11px"}),
            html.Td(f"{h['entry_spread']:+.4f}%", style={**MONO, "color": BLUE}),
            html.Td(f"{h.get('exit_spread', 0):+.4f}%", style={**MONO, "color": GREY}),
            html.Td(f"${h.get('funding_collected', 0):.4f}", style={**MONO, "color": GREEN}),
            html.Td(f"${h.get('spread_pnl', 0):+.4f}",
                    style={**MONO, "color": GREEN if h.get("spread_pnl", 0) >= 0 else RED}),
            html.Td(f"${h['total_pnl']:+.4f}", style={**MONO, "color": pnl_color, "fontWeight": "600"}),
            html.Td(h.get("close_reason", "—") or "—",
                    style={**MONO, "fontSize": "11px", "color": GREY}),
        ], style={"borderBottom": f"1px solid {BORDER}"}))

    if not hist_rows:
        hist_rows = [html.Tr([
            html.Td("No completed trades yet", colSpan=9,
                    style={"textAlign": "center", "color": GREY, **MONO, "padding": "20px"})
        ])]

    hist_table = html.Table(
        [html.Thead(hist_header), html.Tbody(hist_rows)],
        style={"width": "100%", "borderCollapse": "collapse",
               "background": CARD, "borderRadius": "10px", "overflow": "hidden"},
    )

    # ── Log box ───────────────────────────────────────────────────────────────
    logs = db.get_recent_logs(limit=60)
    level_colors = {"INFO": BLUE, "WARNING": YELLOW, "ERROR": RED, "DEBUG": GREY}
    log_items = [
        html.Div([
            html.Span(f"[{lg['time']}] ",
                      style={"color": GREY, **MONO, "fontSize": "11px"}),
            html.Span(f"[{lg['level']}] ",
                      style={"color": level_colors.get(lg["level"], GREY),
                             **MONO, "fontSize": "11px", "fontWeight": "600"}),
            html.Span(lg["message"],
                      style={"color": "#c9d1d9", **MONO, "fontSize": "11px"}),
        ], style={"borderBottom": f"1px solid {BORDER}22", "paddingBottom": "3px",
                  "marginBottom": "3px"})
        for lg in logs
    ]

    return (
        status_badge, f"Last update: {dt}",
        stat_cards, opp_table, pos_table, fig, hist_table,
        log_items or [html.Div("No logs yet.", style={"color": GREY, **MONO, "fontSize": "12px"})],
    )


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Read-only dashboard mode (no live trading)
    print("Running dashboard in READ-ONLY mode on http://0.0.0.0:8050")
    app.run_server(debug=False, host="0.0.0.0", port=Config.DASHBOARD_PORT)
