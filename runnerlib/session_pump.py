"""Feed a running session its messages, and show its thinking and answers.

`runs.spawn` runs this beside every agent whose command reads stream-json on
stdin (`--input-format stream-json`, the seeded node command):

    pump > in.fifo &  claude -p --input-format stream-json ... < in.fifo

(a named pipe rather than `pump | claude`: the shell waits for every command
in a pipeline, and the pump waits for the run's exit file, which the shell
writes only once the agent is done.)

It has three jobs, all driven by two files in the run directory:

  in.jsonl   what the session reads. The round's prompt is the first line; the
             runner appends a line when the driver chats while the round runs
             (`runs.push`). Each line is {"message": <a stream-json user
             message>, "text": ..., "reply_url": ...}. The pump forwards every
             line to the session's stdin. Claude Code reads a message that
             arrives mid-turn at its next tool call and answers it inline,
             then carries on (measured 5 Nov 2026).
  out.json   what the session says (stream-json, one event per line). Every
             thinking block is posted to CADRE_THINKING_URL. A pushed line with
             a `reply_url` is answered by the first text the session writes
             after it READ that line — `--replay-user-messages` echoes each
             message back at the moment it is consumed — and that text is
             posted to the reply url as the agent's reply.

When the session's turn ends (a `result` event) and nothing is left to
forward, the pump closes stdin, so the session exits as `-p` always did, and
writes `in.closed` with how many lines it forwarded. A line appended after
that was never read; the reaper hands it on as a chat turn (`runs.unread`).

Without CADRE_RUN_IN it only relays thinking, for a command that takes its
prompt as an argument. Either way it stops once the run's exit file exists
and the output is read to the end. Best effort: a page that is not open answers
404, a page server that is down refuses, and neither may touch the run.
Stdlib only.
"""

import json
import os
import sys
import time
import urllib.request

POLL_SECONDS = 0.3
MAX_THINKING = 4000


def events(line: str) -> dict | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def blocks(event: dict, kind: str) -> list[str]:
    """The text of every `kind` block ("thinking" or "text") in an assistant event."""
    if event.get("type") != "assistant":
        return []
    field = "thinking" if kind == "thinking" else "text"
    content = (event.get("message") or {}).get("content") or []
    return [str(b.get(field) or "").strip() for b in content
            if isinstance(b, dict) and b.get("type") == kind and str(b.get(field) or "").strip()]


def thinking_texts(line: str) -> list[str]:
    event = events(line)
    return [t[:MAX_THINKING] for t in blocks(event, "thinking")] if event else []


def replayed_text(event: dict) -> str | None:
    """The text of a user message the session just consumed, echoed back."""
    if event.get("type") != "user" or not event.get("isReplay"):
        return None
    content = (event.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text") or "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return None


def post(url: str, text: str) -> bool:
    if not url:
        return False
    req = urllib.request.Request(
        url, data=json.dumps({"text": text}).encode(),
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


class Pump:
    def __init__(self, out_path, exit_path, inbox=None, thinking_url="",
                 stdin=None, sender=post):
        self.out_path, self.exit_path, self.inbox = out_path, exit_path, inbox
        self.thinking_url, self.stdin, self.send = thinking_url, stdin, sender
        self.out_pos, self.partial = 0, ""
        self.in_pos, self.in_partial = 0, ""
        self.forwarded = 0
        self.unread = {}            # text -> reply url, for pushed lines not yet consumed
        self.open = stdin is not None
        self.pending_in = []        # inbox lines read but not yet forwarded

    @staticmethod
    def _read(path, pos):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                return chunk, f.tell()
        except OSError:
            return "", pos

    def feed(self) -> None:
        if not self.inbox:
            return
        chunk, self.in_pos = self._read(self.inbox, self.in_pos)
        lines = (self.in_partial + chunk).split("\n")
        self.in_partial = lines.pop()
        self.pending_in += [l for l in lines if l.strip()]
        if not self.open:
            return
        while self.pending_in:
            entry = json.loads(self.pending_in[0])
            try:
                self.stdin.write(json.dumps(entry["message"]) + "\n")
                self.stdin.flush()
            except (BrokenPipeError, OSError):
                self.close()        # the session is gone; what is left is unread
                return
            self.pending_in.pop(0)
            if entry.get("reply_url"):
                self.unread[entry.get("text") or ""] = entry["reply_url"]
            self.forwarded += 1

    def close(self) -> None:
        if not self.open:
            return
        self.open = False
        try:
            self.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if self.inbox:
            with open(self.inbox + ".closed", "w") as f:
                f.write(str(self.forwarded))

    def relay(self) -> str:
        chunk, self.out_pos = self._read(self.out_path, self.out_pos)
        lines = (self.partial + chunk).split("\n")
        self.partial = lines.pop()
        for line in lines:
            self.handle(line)
        return chunk

    def handle(self, line: str) -> None:
        event = events(line)
        if not event:
            return
        for text in blocks(event, "thinking"):
            self.send(self.thinking_url, text[:MAX_THINKING])
        consumed = replayed_text(event)
        if consumed is not None:
            self.unread.pop(consumed, None)
        if event.get("type") == "result":
            # The turn is over. A line that just arrived still goes in, and a
            # pushed line the session has not read yet becomes its own turn
            # after this one; only with neither does stdin close, so the
            # session exits as `-p` always did.
            self.feed()
            if not self.pending_in and not self.unread:
                self.close()

    def run(self, sleep=time.sleep) -> None:
        while True:
            done = os.path.exists(self.exit_path)
            self.feed()
            chunk = self.relay()
            if done and not chunk:
                if self.partial:
                    self.handle(self.partial)
                self.close()
                return
            sleep(POLL_SECONDS)


if __name__ == "__main__":
    inbox = os.environ.get("CADRE_RUN_IN") or None
    Pump(os.environ["CADRE_RUN_OUT"], os.environ["CADRE_RUN_EXIT"], inbox=inbox,
         thinking_url=os.environ.get("CADRE_THINKING_URL", ""),
         # The pipe as a file this process owns: closing sys.stdout leaves
         # descriptor 1 open (Python opens it with closefd=False), and the
         # session would wait forever for an end of input that never comes.
         stdin=open(sys.stdout.fileno(), "w", closefd=True) if inbox else None).run()
