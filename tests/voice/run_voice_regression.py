import argparse
import json
import os
import sqlite3
import string
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Play voice fixtures in Discord and verify STT logs")
    parser.add_argument("--compose-file", default="docker-compose.yml")
    parser.add_argument("--cases", default="tests/voice/cases.json")
    parser.add_argument("--fixtures-dir", default="tests/voice/fixtures")
    parser.add_argument("--db", default="data/memory.db")
    parser.add_argument("--guild-id", default=os.getenv("DISCORD_TEST_GUILD_ID", ""))
    parser.add_argument("--channel-id", default=os.getenv("DISCORD_TEST_CHANNEL_ID", ""))
    parser.add_argument("--token-env", default="DISCORD_TEST_BOT_TOKEN")
    parser.add_argument("--match-threshold", type=float, default=0.72)
    parser.add_argument("--generate-fixtures", action="store_true")
    parser.add_argument("--piper-url", default="http://127.0.0.1:8001/tts")
    return parser.parse_args()


def _normalize(text: str) -> str:
    lowered = text.lower().replace("ё", "е")
    cleaned = lowered.translate(str.maketrans("", "", string.punctuation + "«»…"))
    return " ".join(cleaned.split())


def _ensure_prerequisites(args: argparse.Namespace) -> None:
    if not args.guild_id or not args.channel_id:
        raise RuntimeError("Set --guild-id/--channel-id or DISCORD_TEST_GUILD_ID/DISCORD_TEST_CHANNEL_ID")
    token = os.getenv(args.token_env, "").strip()
    if not token:
        raise RuntimeError(f"Environment variable {args.token_env} is required")

    for path in (args.compose_file, args.cases, args.db):
        if not Path(path).exists():
            raise RuntimeError(f"Required file not found: {path}")


def _generate_fixtures(args: argparse.Namespace) -> None:
    cmd = [
        "python3",
        "tests/voice/generate_fixtures.py",
        "--cases",
        args.cases,
        "--output-dir",
        args.fixtures_dir,
        "--piper-url",
        args.piper_url,
    ]
    subprocess.run(cmd, check=True)


def _load_cases(path: str) -> list[dict[str, Any]]:
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise RuntimeError("Cases file must be a non-empty JSON array")
    return cases


def _run_sender(compose_file: str, guild_id: str, channel_id: str, fixture_file: Path, label: str) -> None:
    local_python = Path(".venv/bin/python")
    python_bin = str(local_python) if local_python.exists() else "python3"
    cmd = [
        python_bin,
        "tools/voice_test_sender/send_voice.py",
        "--guild-id",
        str(guild_id),
        "--channel-id",
        str(channel_id),
        "--file",
        str(fixture_file),
        "--label",
        label,
    ]
    subprocess.run(cmd, check=True)


def _fetch_recent_voice_logs(db_path: str, channel_id: str, start_ts: str) -> list[tuple[str, str, str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT text, timestamp, username, user_id
            FROM voice_logs
            WHERE platform = 'discord'
              AND channel_id = ?
              AND timestamp >= ?
            ORDER BY id DESC
            LIMIT 25
            """,
            (str(channel_id), start_ts),
        )
        rows = cursor.fetchall()
        return [(r[0] or "", r[1] or "", r[2] or "", r[3] or "") for r in rows]
    finally:
        conn.close()


def _match(expected: str, candidate: str, threshold: float) -> bool:
    from difflib import SequenceMatcher

    exp = _normalize(expected)
    got = _normalize(candidate)
    if not exp or not got:
        return False
    if exp in got:
        return True
    ratio = SequenceMatcher(None, exp, got).ratio()
    return ratio >= threshold


def _wait_for_match(
    db_path: str,
    channel_id: str,
    start_ts: str,
    expected: str,
    timeout_seconds: float,
    match_threshold: float,
) -> tuple[bool, str, list[str]]:
    deadline = time.time() + timeout_seconds
    last_lines: list[str] = []
    while time.time() < deadline:
        rows = _fetch_recent_voice_logs(db_path, channel_id, start_ts)
        if rows:
            last_lines = [f"{ts} | {user or user_id}: {text}" for text, ts, user, user_id in rows]
        for text, ts, user, user_id in rows:
            if _match(expected, text, match_threshold):
                who = user or user_id
                return True, f"{ts} | {who}: {text}", last_lines
        time.sleep(1.0)
    return False, "", last_lines


def main() -> int:
    args = _parse_args()
    _ensure_prerequisites(args)
    if args.generate_fixtures:
        _generate_fixtures(args)

    cases = _load_cases(args.cases)
    fixtures_dir = Path(args.fixtures_dir)

    passed = 0
    failed = 0

    print("=== Voice Regression Start ===")
    print(f"guild_id={args.guild_id} channel_id={args.channel_id}")
    print(f"cases={len(cases)}")

    for case in cases:
        case_id = case.get("id", "unknown")
        file_name = case.get("file", "")
        expected = case.get("expected_contains", "")
        timeout_seconds = float(case.get("timeout_seconds", 45))

        fixture_path = fixtures_dir / file_name
        if not fixture_path.exists():
            print(f"[FAIL] {case_id}: fixture not found: {fixture_path}")
            failed += 1
            continue

        start_ts = datetime.now().isoformat()
        print(f"[RUN ] {case_id}: play={fixture_path.name} expected='{expected}'")
        try:
            _run_sender(
                compose_file=args.compose_file,
                guild_id=str(args.guild_id),
                channel_id=str(args.channel_id),
                fixture_file=fixture_path,
                label=case_id,
            )
        except subprocess.CalledProcessError as exc:
            print(f"[FAIL] {case_id}: sender exit={exc.returncode}")
            failed += 1
            continue

        ok, matched_line, last_lines = _wait_for_match(
            db_path=args.db,
            channel_id=str(args.channel_id),
            start_ts=start_ts,
            expected=expected,
            timeout_seconds=timeout_seconds,
            match_threshold=args.match_threshold,
        )
        if ok:
            print(f"[PASS] {case_id}: {matched_line}")
            passed += 1
        else:
            print(f"[FAIL] {case_id}: expected '{expected}' not found in {timeout_seconds:.0f}s")
            if last_lines:
                print("       recent transcripts:")
                for line in last_lines[:5]:
                    print(f"       - {line}")
            failed += 1

    print("=== Voice Regression Done ===")
    print(f"passed={passed} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
