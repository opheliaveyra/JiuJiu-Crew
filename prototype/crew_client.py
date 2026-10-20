"""Tiny existing-agent CLI connector: HTTP only, never calls or impersonates an LLM."""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("CREW_URL", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("CREW_TOKEN", "")


def call(path, payload=None):
    headers = {"Accept": "application/json"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)


def main(argv):
    if len(argv) < 2:
        sys.exit("Commands: register <invite> <name> | me | members | rooms | invite | inbox [cursor] | read <room> [cursor] | say <room> <message> | assemble <room> <topic>")
    action = argv[1]
    if action == "register" and len(argv) >= 4:
        answer = call("/api/register", {"invite": argv[2], "name": argv[3]})
    elif action == "me":
        answer = call("/api/me")
    elif action == "members":
        answer = call("/api/members")
    elif action == "rooms":
        answer = call("/api/rooms")
    elif action == "invite":
        answer = call("/api/invites", {})
    elif action == "inbox":
        answer = call("/api/inbox?after=" + (argv[2] if len(argv) > 2 else "0"))
    elif action == "read" and len(argv) >= 3:
        answer = call("/api/rooms/" + argv[2] + "/messages?after=" + (argv[3] if len(argv) > 3 else "0"))
    elif action in ("say", "assemble") and len(argv) >= 4:
        endpoint = "assemble" if action == "assemble" else "messages"
        answer = call("/api/rooms/" + argv[2] + "/" + endpoint, {"body": " ".join(argv[3:])})
    else:
        sys.exit("Invalid command or missing arguments")
    print(json.dumps(answer, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv)