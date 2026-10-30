"""
Claim Rework Prioritizer
========================
Ranks denied/rejected healthcare claims so billing teams rework the ones
with the best payoff first.

Priority score = expected recovery ($) / effort (hours) x urgency multiplier

  expected recovery = billed amount x recovery probability (by denial code)
                      x 0.8 for every previous rework attempt
  urgency           = higher as the appeal / timely-filing deadline nears
  expired claims    = score 0, flagged EXPIRED

Python 3.9+, standard library only. Outputs a console table and an HTML report.

Usage:
    python claim_rework_prioritizer.py --demo
    python claim_rework_prioritizer.py --csv claims.csv --out report.html
    python claim_rework_prioritizer.py --write-sample claims.csv

CSV columns:
    claim_id,payer,denial_code,billed_amount,appeal_deadline,attempts
    (appeal_deadline = YYYY-MM-DD, attempts = number of prior reworks)
"""

from __future__ import annotations

import argparse
import csv
import html
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------
# Business rules: CARC denial code -> (recovery probability, effort hours, action)
# Tune these using your own historical rework outcomes.
# --------------------------------------------------------------------------
DENIAL_RULES = {
    "4":   (0.80, 0.25, "Fix modifier and resubmit"),
    "11":  (0.75, 0.50, "Correct diagnosis code and resubmit"),
    "16":  (0.85, 0.25, "Add missing info and resubmit"),
    "18":  (0.10, 0.25, "Duplicate - verify, likely write off"),
    "27":  (0.20, 1.00, "Coverage terminated - find other payer"),
    "29":  (0.05, 1.00, "Timely filing - appeal with proof only"),
    "50":  (0.45, 2.00, "Medical necessity - clinical appeal"),
    "197": (0.55, 1.50, "Prior auth missing - retro-auth request"),
}
DEFAULT_RULE = (0.40, 1.00, "Review denial and appeal")


@dataclass
class Claim:
    claim_id: str
    payer: str
    denial_code: str
    billed_amount: float
    appeal_deadline: date
    attempts: int = 0


@dataclass
class ScoredClaim:
    claim: Claim
    days_left: int
    probability: float
    expected_recovery: float
    effort_hours: float
    urgency: float
    score: float
    action: str
    tier: str = ""


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def urgency_multiplier(days_left: int) -> float:
    if days_left <= 7:
        return 2.0
    if days_left <= 14:
        return 1.5
    if days_left <= 30:
        return 1.2
    return 1.0


def score_claim(claim: Claim, today: date) -> ScoredClaim:
    prob, effort, action = DENIAL_RULES.get(claim.denial_code, DEFAULT_RULE)
    prob *= 0.8 ** claim.attempts
    days_left = (claim.appeal_deadline - today).days
    expected = claim.billed_amount * prob

    if days_left < 0:
        return ScoredClaim(claim, days_left, prob, expected, effort, 0.0, 0.0,
                           "Deadline passed - write off or escalate", "EXPIRED")

    urg = urgency_multiplier(days_left)
    return ScoredClaim(claim, days_left, prob, expected, effort, urg,
                       expected / effort * urg, action)


def prioritize(claims: list[Claim], today: date | None = None) -> list[ScoredClaim]:
    today = today or date.today()
    scored = [score_claim(c, today) for c in claims]
    live = sorted((s for s in scored if s.tier != "EXPIRED"),
                  key=lambda s: s.score, reverse=True)
    expired = [s for s in scored if s.tier == "EXPIRED"]

    # Tiers by rank: top third P1, next third P2, rest P3
    n = len(live)
    for i, s in enumerate(live):
        s.tier = "P1" if i < -(-n // 3) else "P2" if i < -(-2 * n // 3) else "P3"
    return live + expired


# --------------------------------------------------------------------------
# CSV I/O
# --------------------------------------------------------------------------
def load_claims(path: str) -> list[Claim]:
    claims: list[Claim] = []
    with open(path, newline="", encoding="utf-8") as f:
        for lineno, row in enumerate(csv.DictReader(f), start=2):
            try:
                claims.append(Claim(
                    claim_id=row["claim_id"].strip(),
                    payer=row["payer"].strip(),
                    denial_code=row["denial_code"].strip().upper().replace("CO-", ""),
                    billed_amount=float(row["billed_amount"]),
                    appeal_deadline=datetime.strptime(
                        row["appeal_deadline"].strip(), "%Y-%m-%d").date(),
                    attempts=int(row.get("attempts") or 0),
                ))
            except (KeyError, ValueError) as e:
                print(f"Skipping line {lineno}: {e!r}", file=sys.stderr)
    return claims


def sample_claims(today: date) -> list[Claim]:
    d = lambda n: today + timedelta(days=n)  # noqa: E731
    rows = [
        ("CLM-1001", "Aetna",     "16",  1250.00,  5, 0),
        ("CLM-1002", "UHC",       "50",  8400.00, 20, 1),
        ("CLM-1003", "Cigna",     "197", 3200.00, 12, 0),
        ("CLM-1004", "BCBS",      "4",    640.00, 45, 0),
        ("CLM-1005", "Medicare",  "18",   410.00, 30, 0),
        ("CLM-1006", "Humana",    "27",  2100.00, 25, 2),
        ("CLM-1007", "Aetna",     "11",  1800.00,  9, 0),
        ("CLM-1008", "UHC",       "29",  5600.00,  3, 0),
        ("CLM-1009", "Cigna",     "16",   980.00, -4, 0),
        ("CLM-1010", "BCBS",      "50", 12500.00, 60, 0),
        ("CLM-1011", "Medicaid",  "45",   300.00, 14, 0),
        ("CLM-1012", "Humana",    "197", 7400.00,  6, 1),
    ]
    return [Claim(i, p, c, a, d(days), n) for i, p, c, a, days, n in rows]


def write_sample_csv(path: str, today: date) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["claim_id", "payer", "denial_code", "billed_amount",
                    "appeal_deadline", "attempts"])
        for c in sample_claims(today):
            w.writerow([c.claim_id, c.payer, c.denial_code, f"{c.billed_amount:.2f}",
                        c.appeal_deadline.isoformat(), c.attempts])


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def print_table(results: list[ScoredClaim]) -> None:
    hdr = f"{'#':>2} {'Tier':<8}{'Claim':<10}{'Payer':<9}{'Code':<5}{'Billed':>10}" \
          f"{'ExpRecov':>10}{'Days':>6}{'Score':>9}  Action"
    print(hdr)
    print("-" * len(hdr))
    for i, s in enumerate(results, 1):
        c = s.claim
        print(f"{i:>2} {s.tier:<8}{c.claim_id:<10}{c.payer:<9}{c.denial_code:<5}"
              f"{c.billed_amount:>10,.0f}{s.expected_recovery:>10,.0f}"
              f"{s.days_left:>6}{s.score:>9,.0f}  {s.action}")


def build_html(results: list[ScoredClaim], today: date) -> str:
    live = [s for s in results if s.tier != "EXPIRED"]
    total_billed = sum(s.claim.billed_amount for s in results)
    total_exp = sum(s.expected_recovery for s in live)
    urgent = sum(1 for s in live if s.days_left <= 7)
    expired = len(results) - len(live)

    rows = []
    for i, s in enumerate(results, 1):
        c = s.claim
        esc = html.escape
        rows.append(
            f"<tr class='{s.tier.lower()}'><td>{i}</td>"
            f"<td><span class='badge {s.tier.lower()}'>{s.tier}</span></td>"
            f"<td>{esc(c.claim_id)}</td><td>{esc(c.payer)}</td>"
            f"<td>{esc(c.denial_code)}</td>"
            f"<td class='num'>${c.billed_amount:,.0f}</td>"
            f"<td class='num'>${s.expected_recovery:,.0f}</td>"
            f"<td class='num'>{s.days_left}</td>"
            f"<td class='num'>{s.score:,.0f}</td>"
            f"<td>{esc(s.action)}</td></tr>")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claim Rework Prioritizer</title>
<style>
  :root {{ --bg:#f7f8fa; --card:#fff; --text:#1d2330; --muted:#6b7280; --line:#e5e7eb;
          --p1:#dc2626; --p2:#d97706; --p3:#2563eb; --exp:#6b7280; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#14171c; --card:#1d222a; --text:#e8eaee; --muted:#9aa3b2; --line:#2c333d; }} }}
  body {{ margin:0; padding:24px; background:var(--bg); color:var(--text);
         font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
  h1 {{ margin:0 0 4px; font-size:1.5rem; }}
  .sub {{ color:var(--muted); margin-bottom:20px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
           gap:12px; margin-bottom:20px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }}
  .card b {{ display:block; font-size:1.4rem; margin-top:4px; }}
  .wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }}
  table {{ border-collapse:collapse; width:100%; min-width:820px; }}
  th,td {{ padding:10px 12px; text-align:left; border-bottom:1px solid var(--line); }}
  th {{ font-size:.8rem; text-transform:uppercase; color:var(--muted); }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .badge {{ padding:2px 8px; border-radius:99px; color:#fff; font-size:.75rem; font-weight:600; }}
  .badge.p1 {{ background:var(--p1); }} .badge.p2 {{ background:var(--p2); }}
  .badge.p3 {{ background:var(--p3); }} .badge.expired {{ background:var(--exp); }}
  tr.expired td {{ color:var(--muted); }}
</style></head><body>
<h1>Claim Rework Prioritizer</h1>
<div class="sub">Generated for {today.isoformat()} &middot; ranked by expected recovery per effort hour, weighted by deadline urgency</div>
<div class="cards">
  <div class="card">Claims in queue<b>{len(results)}</b></div>
  <div class="card">Total billed<b>${total_billed:,.0f}</b></div>
  <div class="card">Expected recovery<b>${total_exp:,.0f}</b></div>
  <div class="card">Due within 7 days<b>{urgent}</b></div>
  <div class="card">Expired<b>{expired}</b></div>
</div>
<div class="wrap"><table>
<thead><tr><th>#</th><th>Tier</th><th>Claim</th><th>Payer</th><th>Code</th>
<th class="num">Billed</th><th class="num">Exp. recovery</th><th class="num">Days left</th>
<th class="num">Score</th><th>Recommended action</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table></div>
</body></html>"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Claim Rework Prioritizer")
    ap.add_argument("--demo", action="store_true", help="use built-in sample claims")
    ap.add_argument("--csv", help="input CSV of denied claims")
    ap.add_argument("--out", default="rework_report.html", help="HTML report path")
    ap.add_argument("--write-sample", metavar="PATH", help="write a sample CSV and exit")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD)")
    args = ap.parse_args()

    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()

    if args.write_sample:
        write_sample_csv(args.write_sample, today)
        print(f"Sample written to {args.write_sample}")
        return

    if args.csv:
        claims = load_claims(args.csv)
    elif args.demo:
        claims = sample_claims(today)
    else:
        ap.error("use --demo or --csv FILE")

    if not claims:
        sys.exit("No valid claims found.")

    results = prioritize(claims, today)
    print_table(results)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(build_html(results, today))
    print(f"\nHTML report saved to {args.out}")


if __name__ == "__main__":
    main()
