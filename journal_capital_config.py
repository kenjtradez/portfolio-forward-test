"""
CAPITAL PORTFOLIO - reference risk percentages, NOT currently active in journal.py.

Same Sharpe-weighted proportions as the FUNDED configuration in journal.py,
at full scale - for capital with no funded-account drawdown constraint.
Backtested result: CAGR 38.45%, MaxDD -48.13%, Sharpe 1.753 (identical
Sharpe to FUNDED - only the scale differs, not the shape of returns,
since this is a uniform multiplier on the same underlying strategies).

To actually run this configuration, replace STRATEGY_RISK_PCT in
journal.py with the values below. Do NOT run both configurations
simultaneously against the same account - these are two SEPARATE,
mutually exclusive sizing schemes for two separate accounts/purposes.
"""

CAPITAL_STRATEGY_RISK_PCT = {
    "Pivot S/R": 0.00526,
    "Donchian(20)": 0.00266,
    "Connors RSI": 0.00445,
    "Monday Effect": 0.00982,
    "RSI(2) Mean Reversion": 0.01242,
    "Overnight Extension": 0.01040,
    "Divergence-Fade": 0.005,
    "COT Positioning Extreme": 0.0037,
    "VIX Shock-Fade": 0.0045,
}
