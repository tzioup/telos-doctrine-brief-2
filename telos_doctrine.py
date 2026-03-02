import os
import re
import math
import json
import time
from typing import List, Tuple
import requests
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# ----------------------------
# Helpers
# ----------------------------

def extract_video_id(url: str) -> str:
    """
    Accepts common YouTube URL formats and returns the video ID.
    Raises ValueError if it can't find one.
    """
    url = url.strip()
    # youtu.be/<id>
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", url)
    if m:
        return m.group(1)

    # youtube.com/watch?v=<id>
    m = re.search(r"v=([A-Za-z0-9_-]{6,})", url)
    if m:
        return m.group(1)

    # youtube.com/shorts/<id>
    m = re.search(r"youtube\.com/shorts/([A-Za-z0-9_-]{6,})", url)
    if m:
        return m.group(1)

    raise ValueError(f"Could not extract video id from URL: {url}")


def fetch_transcript_text(video_id: str, preferred_langs: List[str]) -> str:
    """
    Fetch transcript. Prefers manual transcript in preferred_langs, else auto transcript.
    """
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
    except TranscriptsDisabled as e:
        raise RuntimeError(f"Transcripts disabled for video {video_id}") from e

    # Try preferred manual transcripts first
    for lang in preferred_langs:
        try:
            t = transcript_list.find_manually_created_transcript([lang])
            return " ".join([x["text"] for x in t.fetch()])
        except Exception:
            pass

    # Try preferred auto transcripts
    for lang in preferred_langs:
        try:
            t = transcript_list.find_generated_transcript([lang])
            return " ".join([x["text"] for x in t.fetch()])
        except Exception:
            pass

    # Last resort: any transcript available
    try:
        t = transcript_list.find_transcript(transcript_list._TranscriptList__transcripts.keys())  # internal, but works often
        return " ".join([x["text"] for x in t.fetch()])
    except Exception as e:
        raise RuntimeError(f"No usable transcript found for video {video_id}") from e


def normalize_text(text: str) -> str:
    # Remove common noise
    text = re.sub(r"\s+", " ", text).strip()
    return text


def chunk_words(text: str, chunk_size_words: int = 1500, overlap_words: int = 100) -> List[str]:
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = words[i:i + chunk_size_words]
        chunks.append(" ".join(chunk))
        i += max(1, chunk_size_words - overlap_words)
    return chunks


# ----------------------------
# Anthropic API call (Claude)
# ----------------------------

def call_anthropic(messages: List[dict], model: str, max_tokens: int = 800) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY env var")

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }

    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=120)
    if resp.status_code >= 300:
        raise RuntimeError(f"Anthropic API error {resp.status_code}: {resp.text}")

    data = resp.json()
    # content is a list of blocks, typically [{"type":"text","text":"..."}]
    blocks = data.get("content", [])
    text_out = "".join([b.get("text", "") for b in blocks if b.get("type") == "text"]).strip()
    return text_out


def load_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def main():
    urls_raw = os.environ.get("YOUTUBE_URLS", "").strip()
    if not urls_raw:
        raise RuntimeError("Set YOUTUBE_URLS env var (newline or space separated URLs).")

    preferred_langs = os.environ.get("PREFERRED_LANGS", "en").split(",")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")

    chunk_prompt = load_prompt("prompts/chunk_prompt.txt")
    merge_prompt = load_prompt("prompts/merge_prompt.txt")

    urls = [u for u in re.split(r"[\n\s]+", urls_raw) if u.strip()]
    video_ids = [extract_video_id(u) for u in urls]

    all_chunk_doctrine: List[str] = []

    for vid in video_ids:
        print(f"Fetching transcript for {vid} ...")
        transcript = fetch_transcript_text(vid, preferred_langs)
        transcript = normalize_text(transcript)

        chunks = chunk_words(transcript, chunk_size_words=1500, overlap_words=100)
        print(f"Chunked {vid} into {len(chunks)} chunks.")

        for idx, ch in enumerate(chunks, start=1):
            print(f"Summarising chunk {idx}/{len(chunks)} ...")
            msg = [
                {"role": "user", "content": f"{chunk_prompt}\n\nCHUNK:\n{ch}"}
            ]
            doctrine = call_anthropic(msg, model=model, max_tokens=500)
            all_chunk_doctrine.append(doctrine)

    # Merge pass
    print("Merging doctrine extracts ...")
    merged_input = "\n\n---\n\n".join(all_chunk_doctrine)
    msg = [
        {"role": "user", "content": f"{merge_prompt}\n\nDOCTRINE EXTRACTS:\n{merged_input}"}
    ]
    doctrine_brief = call_anthropic(msg, model=model, max_tokens=1200)

    out_path = "TELOS_DOCTRINE_BRIEF.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doctrine_brief.strip() + "\n")

    print(f"Done. Wrote {out_path}")


if __name__ == "__main__":
    main()
