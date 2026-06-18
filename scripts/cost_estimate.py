#!/usr/bin/env python3
"""
Interview-cost estimator
========================
Projects the monthly bill for running the AI Interview Agent at a given volume.

Pure standard library — run with any Python 3:
    python scripts/cost_estimate.py                  # presets: 500/1000/2000/5000
    python scripts/cost_estimate.py -n 1000 -m 8     # custom: 1000 interviews, 8 min avg
    python scripts/cost_estimate.py -n 1000 --avatar # include Simli avatar
    python scripts/cost_estimate.py -n 2000 -m 10 --self-host-livekit --on-box-db

All unit prices are ESTIMATES (Jan 2026) — verify against current pricing pages.
Edit the RATES block below as prices/plans change.
"""

import argparse

# ════════════════════════════════════════════════════════════════════════════
# RATES — edit these as vendor pricing changes. All USD.
# ════════════════════════════════════════════════════════════════════════════

# ElevenLabs: turbo/flash v2.5 = 0.5 credits per character. Plans = (name, $/mo, credits).
EL_CREDITS_PER_CHAR = 0.5
EL_PLANS = [
    ("Creator",  22,    100_000),
    ("Pro",      99,    500_000),
    ("Scale",    330,  2_000_000),
    ("Business", 1320, 11_000_000),
]

DEEPGRAM_PER_MIN  = 0.0058    # nova-2 streaming, $/audio-minute
CLAUDE_PER_MIN    = 0.006     # Haiku conversation proxy, $/interview-minute
CLAUDE_FIXED      = 0.015     # strategy call + evaluation call, $/interview (≈flat)
LIVEKIT_PER_PPMIN = 0.0025    # LiveKit Cloud, $/participant-minute
LIVEKIT_PARTS     = 3         # agent + candidate + simli-avatar
SIMLI_PER_MIN     = 0.009     # avatar rendering, $/minute

# Fixed monthly infrastructure
EC2_MONTHLY       = 100       # FastAPI server + agent worker host
MANAGED_DB_MONTHLY = 40       # RDS small (0 if co-located on the EC2 box)
S3_EMAIL_MONTHLY  = 10        # transcript storage + invite emails

DEFAULT_CHARS_PER_MIN = 375   # Sarah's speech: ~3,000 chars over an 8-min interview


def elevenlabs_cost(total_credits: float) -> tuple[str, float]:
    """Pick the cheapest plan that covers the credits; estimate overage beyond the top plan."""
    for name, price, included in EL_PLANS:
        if total_credits <= included:
            return name, float(price)
    # Beyond the largest plan → rough enterprise estimate (scale the top plan's $/credit)
    top_name, top_price, top_credits = EL_PLANS[-1]
    est = top_price * (total_credits / top_credits)
    return f"{top_name}+ (enterprise est.)", round(est)


def estimate(interviews: int, minutes: float, chars_per_min: float,
             avatar: bool, self_host_livekit: bool, on_box_db: bool) -> dict:
    chars_per_iv = minutes * chars_per_min
    el_credits   = chars_per_iv * EL_CREDITS_PER_CHAR * interviews
    el_plan, el_cost = elevenlabs_cost(el_credits)

    rows = {
        f"ElevenLabs ({el_plan})": el_cost,
        "Deepgram (STT)":  DEEPGRAM_PER_MIN * minutes * interviews,
        "Claude (Haiku)":  (CLAUDE_PER_MIN * minutes + CLAUDE_FIXED) * interviews,
        "LiveKit":         0.0 if self_host_livekit
                           else LIVEKIT_PER_PPMIN * LIVEKIT_PARTS * minutes * interviews,
        "Simli avatar":    SIMLI_PER_MIN * minutes * interviews if avatar else 0.0,
        "Hosting (EC2)":   float(EC2_MONTHLY),
        "PostgreSQL":      0.0 if on_box_db else float(MANAGED_DB_MONTHLY),
        "S3 + email":      float(S3_EMAIL_MONTHLY),
    }
    total = sum(rows.values())
    return {
        "rows": rows, "total": total,
        "per_interview": total / interviews if interviews else 0,
        "chars_per_iv": chars_per_iv, "el_credits": el_credits,
    }


def render(label: str, r: dict) -> None:
    print(f"\n  {label}")
    print("  " + "-" * 46)
    for name, cost in r["rows"].items():
        print(f"    {name:<28} ${cost:>9,.0f}/mo")
    print("  " + "-" * 46)
    print(f"    {'TOTAL':<28} ${r['total']:>9,.0f}/mo")
    print(f"    {'Per interview':<28} ${r['per_interview']:>9.2f}")
    print(f"    (ElevenLabs credits used: {r['el_credits']:,.0f})")


def main() -> None:
    ap = argparse.ArgumentParser(description="AI Interview Agent monthly cost estimator")
    ap.add_argument("-n", "--interviews", type=int, help="interviews per month")
    ap.add_argument("-m", "--minutes", type=float, default=8.0, help="avg interview minutes (default 8)")
    ap.add_argument("--chars-per-min", type=float, default=DEFAULT_CHARS_PER_MIN,
                    help=f"Sarah's chars/min (default {DEFAULT_CHARS_PER_MIN})")
    ap.add_argument("--avatar", action="store_true", help="include Simli avatar cost")
    ap.add_argument("--self-host-livekit", action="store_true", help="LiveKit on your EC2 → $0 line")
    ap.add_argument("--on-box-db", action="store_true", help="PostgreSQL co-located on EC2 → $0 line")
    args = ap.parse_args()

    print("\n  AI Interview Agent - Monthly Cost Estimate")
    print(f"  (avg {args.minutes:g} min/interview | {args.chars_per_min:g} chars/min | "
          f"avatar={'on' if args.avatar else 'off'})")

    if args.interviews:
        r = estimate(args.interviews, args.minutes, args.chars_per_min,
                     args.avatar, args.self_host_livekit, args.on_box_db)
        render(f"{args.interviews:,} interviews / month", r)
    else:
        # No -n given → show presets so you see how cost scales
        for n in (500, 1000, 2000, 5000):
            r = estimate(n, args.minutes, args.chars_per_min,
                         args.avatar, args.self_host_livekit, args.on_box_db)
            render(f"{n:,} interviews / month", r)

    print("\n  Note: estimates only - verify vendor pricing; ask for volume/committed-use\n"
          "  discounts at 1,000+/month. ElevenLabs & avg interview length are the big levers.\n")


if __name__ == "__main__":
    main()
