#!/usr/bin/env python3
"""c2c mailbox backend — envelope store for the Stop-hook channel.

Handles the persistent message model behind `c2c` (threads, replies, delivery
receipts, broadcast bookkeeping). cmux/keystroke concerns stay in the bash
wrapper; this module only touches files under $C2C_HOME (default ~/.c2c).

Envelope (one JSON object per line):
    id          unique message id (uuid) — used for delivery receipts
    thread      conversation id (uuid) — groups a back-and-forth
    from        sender surface ref (e.g. "surface:17")
    from_uuid   sender surface uuid (stable)
    to_uuid     recipient surface uuid
    kind        "msg" | "reply"
    in_reply_to id of the message this answers (or null)
    ts          ISO-8601 local timestamp
    text        payload
"""
import argparse
import datetime
import json
import os
import sys
import uuid

HOME = os.environ.get("C2C_HOME") or os.path.expanduser("~/.c2c")


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _path(*parts):
    p = os.path.join(HOME, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _append(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _store(rec):
    """Persist a record to the recipient inbox and the thread transcript."""
    _append(_path("inbox", rec["to_uuid"] + ".jsonl"), rec)
    _append(_path("threads", rec["thread"] + ".jsonl"), rec)


MAX_HOPS = int(os.environ.get("C2C_MAX_HOPS", "8"))


def cmd_post(a):
    thread = a.thread or uuid.uuid4().hex[:12]
    rec = {
        "id": uuid.uuid4().hex[:12],
        "thread": thread,
        "from": a.self_ref,
        "from_uuid": a.self_uuid,
        "to_uuid": a.to_uuid,
        "kind": a.kind,
        "in_reply_to": a.in_reply_to,
        "hops": 0,
        "ts": _now(),
        "text": a.text,
    }
    _store(rec)
    print(json.dumps({"id": rec["id"], "thread": thread, "to_uuid": a.to_uuid, "hops": 0},
                     ensure_ascii=False))


def _reply_like(a, kind, text):
    """Post a reply/ack to the most recent message in a thread not from me.
    Carries a hop counter (parent + 1) to bound runaway auto-reply loops."""
    msgs = _read_jsonl(_path("threads", a.thread + ".jsonl"))
    if not msgs:
        print(f"c2c: unbekannter Thread: {a.thread}", file=sys.stderr)
        sys.exit(1)
    inbound = [m for m in msgs if m.get("from_uuid") != a.self_uuid]
    target = inbound[-1] if inbound else msgs[-1]
    rec = {
        "id": uuid.uuid4().hex[:12],
        "thread": a.thread,
        "from": a.self_ref,
        "from_uuid": a.self_uuid,
        "to_uuid": target.get("from_uuid"),
        "kind": kind,
        "in_reply_to": target.get("id"),
        "hops": int(target.get("hops", 0)) + 1,
        "ts": _now(),
        "text": text,
    }
    _store(rec)
    print(json.dumps({"id": rec["id"], "thread": a.thread, "to_uuid": rec["to_uuid"],
                      "to": target.get("from"), "hops": rec["hops"]}, ensure_ascii=False))


def cmd_reply(a):
    _reply_like(a, "reply", a.text)


def cmd_ack(a):
    """Acknowledge a thread. Delivered but framed as 'do NOT reply' -> ends
    the ping-pong instead of prolonging it."""
    _reply_like(a, "ack", a.text or "Kenntnisnahme.")


def cmd_deliver(a):
    """Stop-hook core: drain my inbox, emit the block/reason, write receipts.
    Acks are framed as no-reply; messages at/over the hop limit warn against
    auto-answering so autonomous exchanges can't loop forever."""
    inbox = _path("inbox", a.self_uuid + ".jsonl")
    msgs = _read_jsonl(inbox)
    if not msgs:
        sys.exit(0)
    # Archive + clear inbox first -> loop-safe.
    for m in msgs:
        _append(_path("processed", a.self_uuid + ".log"), m)
    open(inbox, "w").close()
    # Delivery receipt back to each sender (a quiet log, NOT a new message).
    for m in msgs:
        if m.get("from_uuid"):
            _append(_path("receipts", m["from_uuid"] + ".jsonl"), {
                "msg_id": m.get("id"),
                "thread": m.get("thread"),
                "kind": m.get("kind"),
                "delivered_at": _now(),
                "by": a.self_ref,
                "by_uuid": a.self_uuid,
            })
    lines = [f"Du hast {len(msgs)} neue c2c-Nachricht(en) von anderen Claude-Instanzen "
             f"(Kanal: Mailbox/Stop-Hook):"]
    for i, m in enumerate(msgs, 1):
        kind = m.get("kind")
        who = m.get("from", "?")
        thread = m.get("thread", "?")
        meta = f'thread {thread} · id {m.get("id","?")} ({m.get("ts","")})'
        text = m.get("text", "")
        if kind == "ack":
            lines.append(f'[{i}] ACK von {who} · {meta}: {text}  '
                         f'(nur Kenntnisnahme — NICHT antworten, Thread ist abgeschlossen)')
        elif int(m.get("hops", 0)) >= MAX_HOPS:
            lines.append(f'[{i}] Antwort von {who} · {meta}: {text}  '
                         f'⚠ Hop-Limit ({m.get("hops")}) erreicht — NICHT automatisch '
                         f'weiterantworten (Endlosschleife vermeiden); ggf. mit '
                         f'`c2c ack {thread}` abschliessen.')
        else:
            tag = "Antwort" if kind == "reply" else "Nachricht"
            lines.append(f'[{i}] {tag} von {who} · {meta}: {text}  '
                         f'→ antworte ggf. mit `c2c reply {thread} "…"` oder '
                         f'schliesse ab mit `c2c ack {thread}`.')
    reason = "\n".join(lines)
    # Two delivery styles (C2C_DELIVERY):
    #   block   (default) — forces the instance to process now; renders as a
    #                       "Stop hook feedback/error" line (Claude Code framing).
    #   context           — clean, non-error system-reminder injection, BUT the
    #                       turn completes: the note is seen at the next turn,
    #                       not acted on immediately.
    if os.environ.get("C2C_DELIVERY", "block") == "context":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop",
                                                  "additionalContext": reason}}, ensure_ascii=False))
    else:
        print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


def cmd_inbox(a):
    msgs = _read_jsonl(_path("inbox", a.self_uuid + ".jsonl"))
    if not msgs:
        print(f"Inbox leer ({a.self_uuid})")
        return
    print(f"Inbox {a.self_uuid} ({len(msgs)}):")
    for m in msgs:
        print(f'  [{m.get("kind")}] thread {m.get("thread")} id {m.get("id")} '
              f'von {m.get("from")}: {m.get("text")}')


def cmd_status(a):
    receipts = _read_jsonl(_path("receipts", a.self_uuid + ".jsonl"))
    hit = [r for r in receipts if r.get("msg_id") == a.msg_id]
    if hit:
        r = hit[-1]
        print(f'zugestellt: msg {a.msg_id} an {r.get("by")} um {r.get("delivered_at")}')
    else:
        print(f"ausstehend: msg {a.msg_id} (noch keine Quittung)")


def cmd_receipts(a):
    receipts = _read_jsonl(_path("receipts", a.self_uuid + ".jsonl"))
    if not receipts:
        print(f"keine Quittungen ({a.self_uuid})")
        return
    print(f"Quittungen ({len(receipts)}):")
    for r in receipts[-a.limit:]:
        print(f'  msg {r.get("msg_id")} · thread {r.get("thread")} · zugestellt an '
              f'{r.get("by")} um {r.get("delivered_at")}')


def cmd_thread(a):
    msgs = _read_jsonl(_path("threads", a.thread + ".jsonl"))
    if not msgs:
        print(f"unbekannter Thread: {a.thread}")
        return
    print(f"Thread {a.thread} ({len(msgs)}):")
    for m in msgs:
        print(f'  {m.get("ts")} {m.get("from")} [{m.get("kind")}]: {m.get("text")}')


# --- groups (broadcast) ----------------------------------------------------
def _groups_path():
    return _path("groups.json")


def _load_groups():
    p = _groups_path()
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_groups(g):
    json.dump(g, open(_groups_path(), "w", encoding="utf-8"), indent=2, ensure_ascii=False)


def cmd_group(a):
    g = _load_groups()
    if a.action == "list":
        if not g:
            print("keine Gruppen")
        for name, members in g.items():
            print(f"{name}: {' '.join(members)}")
    elif a.action == "set":
        g[a.name] = a.members
        _save_groups(g)
        print(f"Gruppe {a.name} = {' '.join(a.members)}")
    elif a.action == "rm":
        g.pop(a.name, None)
        _save_groups(g)
        print(f"Gruppe {a.name} entfernt")
    elif a.action == "get":
        print(" ".join(g.get(a.name, [])))


def main():
    p = argparse.ArgumentParser(prog="c2c_mail")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("post")
    sp.add_argument("--self-uuid", dest="self_uuid", required=True)
    sp.add_argument("--self-ref", dest="self_ref", default="unknown")
    sp.add_argument("--to-uuid", dest="to_uuid", required=True)
    sp.add_argument("--text", required=True)
    sp.add_argument("--thread", default=None)
    sp.add_argument("--kind", default="msg")
    sp.add_argument("--in-reply-to", dest="in_reply_to", default=None)
    sp.set_defaults(func=cmd_post)

    sp = sub.add_parser("reply")
    sp.add_argument("--self-uuid", dest="self_uuid", required=True)
    sp.add_argument("--self-ref", dest="self_ref", default="unknown")
    sp.add_argument("--thread", required=True)
    sp.add_argument("--text", required=True)
    sp.set_defaults(func=cmd_reply)

    sp = sub.add_parser("ack")
    sp.add_argument("--self-uuid", dest="self_uuid", required=True)
    sp.add_argument("--self-ref", dest="self_ref", default="unknown")
    sp.add_argument("--thread", required=True)
    sp.add_argument("--text", default="")
    sp.set_defaults(func=cmd_ack)

    sp = sub.add_parser("deliver")
    sp.add_argument("--self-uuid", dest="self_uuid", required=True)
    sp.add_argument("--self-ref", dest="self_ref", default="unknown")
    sp.set_defaults(func=cmd_deliver)

    for name in ("inbox", "receipts"):
        sp = sub.add_parser(name)
        sp.add_argument("--self-uuid", dest="self_uuid", required=True)
        if name == "receipts":
            sp.add_argument("--limit", type=int, default=20)
        sp.set_defaults(func=cmd_inbox if name == "inbox" else cmd_receipts)

    sp = sub.add_parser("status")
    sp.add_argument("--self-uuid", dest="self_uuid", required=True)
    sp.add_argument("--msg-id", dest="msg_id", required=True)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("thread")
    sp.add_argument("--thread", required=True)
    sp.set_defaults(func=cmd_thread)

    sp = sub.add_parser("group")
    sp.add_argument("action", choices=["list", "set", "rm", "get"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("members", nargs="*")
    sp.set_defaults(func=cmd_group)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
