"""
Command-line tool για voice agent management.

Usage:
    python -m voice.cli healthcheck
    python -m voice.cli provision <slug> [--voice madlen] [--phone +30...] [--buy]
    python -m voice.cli deprovision <slug> [--release-number]
    python -m voice.cli list-numbers
    python -m voice.cli search-numbers --area-code 2831
    python -m voice.cli test-tts "Καλημέρα κύριε Παπαδόπουλε"
    python -m voice.cli call-summary <slug> [--limit 20]
    python -m voice.cli cost <slug>
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import argparse
import json
from voice import (
    vapi_client, twilio_client, elevenlabs_client,
    provisioner, cost_tracker,
)


def cmd_healthcheck(args):
    print("=== Vapi ===")
    print(json.dumps(vapi_client.healthcheck(), indent=2, ensure_ascii=False))
    print("\n=== Twilio ===")
    print(json.dumps(twilio_client.healthcheck(), indent=2, ensure_ascii=False))
    print("\n=== ElevenLabs ===")
    print(json.dumps(elevenlabs_client.healthcheck(), indent=2, ensure_ascii=False))


def cmd_setup_wizard(args):
    """Step-by-step diagnostic — λέει ακριβώς τι λείπει."""
    import os
    print("\n" + "="*60)
    print(" RAN-TEVU VOICE SETUP WIZARD")
    print("="*60 + "\n")

    checks = [
        ("VAPI_API_KEY",          "vapi.ai → Dashboard → API Keys → Copy *private* key"),
        ("VAPI_PUBLIC_KEY",       "vapi.ai → Dashboard → API Keys → Copy *public* key"),
        ("TWILIO_ACCOUNT_SID",    "twilio.com/console → Account Info → Account SID"),
        ("TWILIO_AUTH_TOKEN",     "twilio.com/console → Account Info → Auth Token"),
        ("ELEVENLABS_API_KEY",    "elevenlabs.io → Profile (top right) → API Keys"),
        ("PUBLIC_BASE_URL",       "Set to: https://www.ran-tevu.gr"),
        ("VAPI_WEBHOOK_SECRET",   "Any random string (πχ ran-tevu-voice-2026)"),
        ("VAPI_DEMO_ASSISTANT_ID","Run `python -m voice.cli provision-demo` after other keys set"),
    ]

    missing = []
    for env, hint in checks:
        val = os.environ.get(env, "").strip()
        status = "OK" if val else "MISSING"
        masked = (val[:8] + "...") if val else "(not set)"
        print(f"  [{status:>7}] {env:<30} = {masked}")
        if not val:
            missing.append((env, hint))

    print("\n" + "-"*60)
    if not missing:
        print("\n✅ All env vars set. Running live healthcheck...\n")
        cmd_healthcheck(args)
        print("\n✅ READY. Next: `python -m voice.cli provision <slug> --buy`")
        return

    print(f"\n❌ {len(missing)} env vars missing. Add to .env (local) AND Railway:")
    for env, hint in missing:
        print(f"\n  {env}=")
        print(f"    → {hint}")
    print("\n" + "-"*60)
    print("\nAfter adding to Railway: wait ~2 min for redeploy, then re-run this wizard.")
    print("For local testing: add to .env and re-run.")


def cmd_provision(args):
    try:
        result = provisioner.provision_voice_agent(
            business_slug=args.slug,
            voice_key=args.voice,
            assistant_name=args.name,
            twilio_phone=args.phone,
            purchase_new_number=args.buy,
            area_code=args.area_code,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except provisioner.ProvisionError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


def cmd_deprovision(args):
    result = provisioner.deprovision_voice_agent(
        args.slug, release_twilio_number=args.release_number
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_list_numbers(args):
    nums = twilio_client.list_owned_numbers()
    if not nums:
        print("No Twilio numbers owned.")
        return
    for n in nums:
        print(f"  {n['phone_number']:<20} SID={n['sid']}  ({n.get('friendly_name', '')})")


def cmd_search_numbers(args):
    avail = twilio_client.search_available_greek_numbers(args.area_code, limit=args.limit)
    if not avail:
        print(f"No available numbers for area code {args.area_code}.")
        return
    for n in avail:
        print(f"  {n['phone_number']:<20}  {n.get('friendly_name', '')}")


def cmd_test_tts(args):
    voice = args.voice or "madlen"
    out = args.out or f"tts_test_{voice}.mp3"
    print(f"Generating with voice '{voice}' → {out}")
    elevenlabs_client.generate_speech(args.text, voice_key=voice, output_path=out)
    print(f"Saved {out}")


def cmd_call_summary(args):
    import database as db
    with db._conn() as c:
        rows = c.execute("""
            SELECT id, vapi_call_id, from_number, started_at,
                   duration_seconds, cost_usd, summary, success_evaluation
            FROM voice_calls WHERE business_slug=?
            ORDER BY started_at DESC LIMIT ?
        """, (args.slug, args.limit)).fetchall()
    if not rows:
        print(f"No calls for {args.slug}.")
        return
    for r in rows:
        print(f"\n--- Call #{r['id']} ({r['started_at']}) ---")
        print(f"  From: {r['from_number']}  Duration: {r['duration_seconds']}s  Cost: ${r['cost_usd']:.3f}")
        print(f"  Summary: {r['summary'] or '—'}")
        print(f"  Success: {r['success_evaluation'] or '—'}")


def cmd_cost(args):
    print(json.dumps(cost_tracker.monthly_cost_for_business(args.slug), indent=2, ensure_ascii=False))


def cmd_provision_demo(args):
    from voice.demo_assistant import provision_demo
    print(json.dumps(provision_demo(), indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(prog="voice.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("healthcheck", help="Check all API integrations")

    sp = sub.add_parser("provision", help="Provision voice agent for a business")
    sp.add_argument("slug")
    sp.add_argument("--voice", choices=["madlen", "stefanos", "athina", "dimitris"])
    sp.add_argument("--name", default="Madlen", help="Assistant's self-name")
    sp.add_argument("--phone", help="Existing Twilio E.164 number")
    sp.add_argument("--buy", action="store_true", help="Buy new Greek number")
    sp.add_argument("--area-code", default="2831", help="Greek area code")
    sp.add_argument("--dry-run", action="store_true", help="Show config without API calls")

    sp = sub.add_parser("deprovision", help="Remove voice agent")
    sp.add_argument("slug")
    sp.add_argument("--release-number", action="store_true",
                    help="Also release Twilio number (stops billing)")

    sub.add_parser("list-numbers", help="List owned Twilio numbers")

    sp = sub.add_parser("search-numbers", help="Search available Greek numbers")
    sp.add_argument("--area-code", default="2831")
    sp.add_argument("--limit", type=int, default=10)

    sp = sub.add_parser("test-tts", help="Generate test TTS audio")
    sp.add_argument("text")
    sp.add_argument("--voice", help="madlen/stefanos/athina/dimitris")
    sp.add_argument("--out", help="Output MP3 path")

    sp = sub.add_parser("call-summary", help="Show recent calls")
    sp.add_argument("slug")
    sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("cost", help="Monthly cost summary")
    sp.add_argument("slug")

    sub.add_parser("provision-demo",
                   help="Provision the homepage public demo assistant")

    sub.add_parser("setup-wizard",
                   help="Step-by-step diagnostic: what's missing in env?")

    args = p.parse_args()
    handlers = {
        "healthcheck": cmd_healthcheck,
        "provision": cmd_provision,
        "deprovision": cmd_deprovision,
        "list-numbers": cmd_list_numbers,
        "search-numbers": cmd_search_numbers,
        "test-tts": cmd_test_tts,
        "call-summary": cmd_call_summary,
        "cost": cmd_cost,
        "provision-demo": cmd_provision_demo,
        "setup-wizard": cmd_setup_wizard,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
