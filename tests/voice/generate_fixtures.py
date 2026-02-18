import argparse
import json
from pathlib import Path
import urllib.request


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate voice fixtures from local piper service")
    parser.add_argument("--cases", default="tests/voice/cases.json")
    parser.add_argument("--output-dir", default="tests/voice/fixtures")
    parser.add_argument("--piper-url", default="http://127.0.0.1:8001/tts")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def _request_tts(url: str, text: str) -> bytes:
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


def main() -> int:
    args = _parse_args()
    cases_path = Path(args.cases)
    output_dir = Path(args.output_dir)

    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)

    generated = 0
    skipped = 0
    for case in cases:
        text = (case.get("tts_text") or "").strip()
        file_name = (case.get("file") or "").strip()
        case_id = case.get("id", "unknown")
        if not text or not file_name:
            raise RuntimeError(f"Case {case_id} must have non-empty 'tts_text' and 'file'")

        out_path = output_dir / file_name
        if out_path.exists() and not args.force:
            skipped += 1
            print(f"[skip] {case_id}: {out_path}")
            continue

        audio = _request_tts(args.piper_url, text)
        out_path.write_bytes(audio)
        generated += 1
        print(f"[ok] {case_id}: {out_path}")

    print(f"Done. generated={generated}, skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
