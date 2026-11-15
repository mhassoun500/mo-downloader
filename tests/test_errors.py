"""Unit tests for how MO Downloader reads error messages (no app start needed).

The message decides the next attempt: with the browser's login, through the proxy, or as a real browser.
A word that is too general sends the user wrong advice and makes pointless retries."""
import ast
import sys
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "src" / "server.py").read_text(encoding="utf-8")
LISTS = {}
for node in ast.parse(SRC).body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and getattr(node.targets[0], "id", "") in (
            "AUTH_ERRORS", "NET_ERRORS", "BLOCK_ERRORS", "NO_MEDIA_ERRORS"):
        LISTS[node.targets[0].id] = ast.literal_eval(node.value)


def has(kind, msg):
    return any(k in msg.lower() for k in LISTS[kind])


CASES = [  # (message, login?, network?, blocked?)
    ("ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser", True, False, False),
    ("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.", True, False, False),
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", True, False, False),
    ("ERROR: [youtube] abc: Join this channel to get access to members-only content", True, False, False),
    ("ERROR: [vimeo] 123: HTTP Error 401: Unauthorized", True, False, False),
    ("ERROR: [youtube] abc: This video is no longer available because the YouTube account associated with this video has been terminated.", False, False, False),
    ("ERROR: [youtube] abc: Video unavailable. This video contains content from SME, who has blocked it on copyright grounds", False, False, False),
    ("WARNING: Loaded 12 cookies from chrome", False, False, False),
    ("ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>", False, True, True),
    ("ERROR: unable to download video data: <urlopen error [Errno 111] Connection refused>", False, True, False),
    ("ERROR: Unable to download webpage: The read operation timed out", False, True, True),
    ("ERROR: [youtube] abc: The uploader has not made this video available in your country", False, True, False),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", False, False, True),
    ("ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests", False, False, True),
    ("ERROR: [generic] Unsupported URL: https://example.com/page", False, False, False),
    ("ERROR: Requested format is not available. Use --list-formats for a list of available formats", False, False, False),
    ("ERROR: Postprocessing: Conversion failed! (socket_timeout setting ignored)", False, False, False),
]


def main():
    bad = []
    for msg, login, net, blocked in CASES:
        got = (has("AUTH_ERRORS", msg), has("NET_ERRORS", msg), has("BLOCK_ERRORS", msg))
        if got != (login, net, blocked):
            bad.append(f"{msg[:70]}…  expected login/net/blocked={(login, net, blocked)} got {got}")
    for b in bad:
        print("FAIL", b)
    print(f"{len(CASES) - len(bad)}/{len(CASES)} error messages classified correctly")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
