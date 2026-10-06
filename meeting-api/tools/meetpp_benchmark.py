"""Score a Meet++ replay against the meeting's real report, and run the
benchmark end to end.

    score  --out DIR --reference REF.json [--json]
    run    --config BENCH.json [--label NAME]

`run` replays the meeting with tools/meetpp_replay.py (paths and options from
the config) into a fresh output directory and scores it. The reference and the
config hold real meeting content: keep them outside the repository (the
benchmark data lives on the Mac Studio, ~/meetpp-replay/bench/).

Reference (JSON):
    start            ISO time of the replay's minute 0
    transitions      {"<agenda number>": minutes, ...}: when the chair took each point
    decisions        [{"title", "status"}]: what the report records as decided
    not_decided      [titles]: put on the agenda but not taken
    new_actions      [titles]: actions raised at the meeting
    previous_actions [{"title", "status"}]: previous actions the report reports on
    minutes_words    words of the real minutes

Matching is by shared content words (a reference title is matched by the
replay item covering the largest share of its words, one to one); the
scorecard lists every pair so that a person can check the matching.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

STOP = set(
    "the and for with that this from its are was his her their our all one two three not but has have had will "
    "into onto than then them they who what when where which while would could should about after before over "
    "under upon also any each other such only more most very can may might must shall these those there here "
    "meeting item point board directors director".split()
)
ON_TIME_MINUTES = 1.5


def words(text: str | None) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) <= 2 or w in STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def similarity(ref: str, cand: str) -> float:
    """Dice overlap of content words (symmetric: a long candidate covering many
    words does not beat the candidate that is about the same thing)."""
    r, c = words(ref), words(cand)
    return 2 * len(r & c) / (len(r) + len(c)) if r and c else 0.0


def assign(refs: list[str], cands: list[tuple[str, str, str]], threshold: float) -> dict[int, tuple[int, float]]:
    """refs: titles; cands: (id, title, more text). Greedy one-to-one by the
    best title similarity, the fuller text breaking ties."""
    pairs = sorted(
        ((max(similarity(r, c[1]), 0.8 * similarity(r, f"{c[1]} {c[2]}")), i, j)
         for i, r in enumerate(refs) for j, c in enumerate(cands)),
        reverse=True,
    )
    used_r, used_c, out = set(), set(), {}
    for score, i, j in pairs:
        if score < threshold or i in used_r or j in used_c:
            continue
        out[i] = (j, score)
        used_r.add(i)
        used_c.add(j)
    return out


def minutes_words(path: Path) -> int:
    if not path.exists():
        return 0
    text = path.read_text()
    text = re.sub(r"[#>*_|`-]+", " ", text)
    return len(text.split())


def score(out: Path, ref: dict) -> dict:
    db = sqlite3.connect(out / "replay.db")
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    sid = q("select id from meetpp_sessions")[0][0]
    start = datetime.fromisoformat(ref["start"].replace("Z", "+00:00"))

    # Agenda transitions: when each point first became live.
    agenda = [r[0] for r in q(
        "select title from meetpp_sections where session_id=? and kind='agenda' and parent_id is null order by position", sid)]
    number = {title: str(i) for i, title in enumerate(agenda, start=1)}
    summary = json.loads((out / "summary.json").read_text())
    live: dict[str, float] = {}
    for when, title in summary.get("timeline", []):
        if title.startswith("PROPOSAL") or title not in number or number[title] in live:
            continue
        live[number[title]] = round((datetime.fromisoformat(when.replace("Z", "+00:00")) - start).total_seconds() / 60, 1)
    transitions = []
    for n, at in sorted(ref.get("transitions", {}).items(), key=lambda kv: int(kv[0])):
        got = live.get(n)
        transitions.append({"point": n, "reference": at, "replay": got,
                            "on_time": got is not None and abs(got - at) <= ON_TIME_MINUTES})

    # Decisions.
    decs = q("select ref, status, title, coalesce(resolution, '') from meetpp_decisions where session_id=?", sid)
    cands = [(d[0], d[2], d[3]) for d in decs]
    ref_dec = [d["title"] for d in ref.get("decisions", [])]
    m = assign(ref_dec, cands, 0.2)
    decisions = []
    for i, d in enumerate(ref.get("decisions", [])):
        hit = m.get(i)
        got = decs[hit[0]] if hit else None
        decisions.append({"reference": d["title"], "replay": f"{got[0]} {got[2]}" if got else None,
                          "status": got[1] if got else None, "ok": bool(got) and got[1] == d["status"]})
    matched = {decs[j][0] for j, _ in m.values()}
    nd = assign(ref.get("not_decided", []), cands, 0.2)
    wrongly_taken = [decs[j][0] for j, _ in nd.values() if decs[j][1] in ("adopted", "rejected")]
    extra_adopted = [f"{d[0]} {d[2]}" for d in decs
                     if d[1] == "adopted" and d[0] not in matched and d[0] not in {decs[j][0] for j, _ in nd.values()}]

    # New actions.
    acts = q("select ref, status, title, coalesce(description, '') from meetpp_actions where session_id=? and origin!='pdf'", sid)
    am = assign(ref.get("new_actions", []), [(a[0], a[2], a[3]) for a in acts], 0.2)
    new_actions = [{"reference": t, "replay": f"{acts[am[i][0]][0]} {acts[am[i][0]][2]}" if i in am else None}
                   for i, t in enumerate(ref.get("new_actions", []))]
    extra_actions = [f"{a[0]} {a[2]}" for j, a in enumerate(acts) if j not in {v[0] for v in am.values()}]

    # Previous actions reported on.
    prev = q("""select a.ref, a.status, a.title, coalesce(r.note, '') from meetpp_actions a
                left join meetpp_action_reports r on r.action_id = a.id and r.session_id = ?
                where a.origin = 'pdf'""", sid)
    pm = assign([p["title"] for p in ref.get("previous_actions", [])], [(p[0], p[2], "") for p in prev], 0.6)
    previous = []
    for i, p in enumerate(ref.get("previous_actions", [])):
        got = prev[pm[i][0]] if i in pm else None
        previous.append({"reference": p["title"], "ref_status": p["status"], "replay": got[0] if got else None,
                         "status": got[1] if got else None, "reported": bool(got and got[3].strip()),
                         "status_ok": bool(got) and got[1] == p["status"]})
    ref_done = {prev[pm[i][0]][0] for i, p in enumerate(ref.get("previous_actions", [])) if i in pm and p["status"] == "done"}
    wrongly_closed = [f"{p[0]} {p[2]}" for p in prev if p[1] == "done" and p[0] not in ref_done]

    words_replay = minutes_words(out / "minutes.md")
    words_ref = int(ref.get("minutes_words") or 0)

    def frac(items, key):
        return round(sum(1 for x in items if x[key]) / len(items), 3) if items else 0.0

    s_trans = frac(transitions, "on_time")
    s_dec = max(0.0, frac(decisions, "ok") - 0.2 * (len(extra_adopted) + len(wrongly_taken)) / max(1, len(decisions)))
    s_new = max(0.0, round(sum(1 for a in new_actions if a["replay"]) / max(1, len(new_actions)), 3)
                - 0.05 * max(0, len(extra_actions) - 2))
    s_prev = round(0.5 * frac(previous, "reported") + 0.5 * frac(previous, "status_ok"), 3)
    s_prev = max(0.0, s_prev - 0.1 * len(wrongly_closed))
    s_len = max(0.0, 1 - abs(words_replay - words_ref) / words_ref) if words_ref else 0.0
    total = round(100 * (0.25 * s_trans + 0.25 * s_dec + 0.2 * s_new + 0.2 * s_prev + 0.1 * s_len), 1)
    calls = q("select purpose, count(*), round(avg(latency_ms)) from meetpp_llm_calls group by purpose")
    return {
        "score": total,
        "parts": {"transitions": s_trans, "decisions": round(s_dec, 3), "new_actions": round(s_new, 3),
                  "previous_actions": round(s_prev, 3), "minutes_length": round(s_len, 3)},
        "transitions": transitions,
        "decisions": decisions, "extra_adopted": extra_adopted, "wrongly_taken": wrongly_taken,
        "new_actions": new_actions, "extra_actions": extra_actions,
        "previous_actions": previous, "wrongly_closed": wrongly_closed,
        "minutes_words": {"replay": words_replay, "reference": words_ref},
        "llm": {p: {"calls": n, "avg_ms": ms} for p, n, ms in calls},
    }


def render(card: dict) -> str:
    p = card["parts"]
    lines = [f"SCORE {card['score']} / 100   (transitions {p['transitions']:.2f} · decisions {p['decisions']:.2f} · "
             f"new actions {p['new_actions']:.2f} · previous actions {p['previous_actions']:.2f} · "
             f"minutes length {p['minutes_length']:.2f})", "", "Transitions (minutes):"]
    for t in card["transitions"]:
        lines.append(f"  {t['point']:>2}  ref {t['reference']:5.1f}  replay {t['replay'] if t['replay'] is not None else '—':>5}  "
                     f"{'ok' if t['on_time'] else 'OFF'}")
    lines.append("Decisions:")
    for d in card["decisions"]:
        lines.append(f"  {'ok ' if d['ok'] else 'NO '} {d['reference'][:60]:60} <- {d['replay'] or '—'} [{d['status'] or '—'}]")
    for x in card["extra_adopted"]:
        lines.append(f"  EXTRA adopted: {x}")
    for x in card["wrongly_taken"]:
        lines.append(f"  TAKEN but not decided: {x}")
    lines.append("New actions:")
    for a in card["new_actions"]:
        lines.append(f"  {'ok ' if a['replay'] else 'NO '} {a['reference'][:60]:60} <- {a['replay'] or '—'}")
    if card["extra_actions"]:
        lines.append(f"  extra ({len(card['extra_actions'])}): " + "; ".join(x[:50] for x in card["extra_actions"]))
    lines.append("Previous actions (reported / status):")
    for a in card["previous_actions"]:
        lines.append(f"  {'R' if a['reported'] else '-'}{'S' if a['status_ok'] else '-'} {a['reference'][:60]:60} "
                     f"ref {a['ref_status']:11} replay {a['status'] or '—'}")
    for x in card["wrongly_closed"]:
        lines.append(f"  WRONGLY CLOSED: {x}")
    w = card["minutes_words"]
    lines.append(f"Minutes: {w['replay']} words (reference {w['reference']})")
    lines.append("LLM: " + ", ".join(f"{k} {v['calls']}× {v['avg_ms']} ms" for k, v in card["llm"].items()))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("--out", required=True)
    s.add_argument("--reference", required=True)
    s.add_argument("--json", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--label", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    args = ap.parse_args()

    if args.cmd == "score":
        card = score(Path(args.out), json.loads(Path(args.reference).read_text()))
        print(json.dumps(card, indent=1) if args.json else render(card))
        return 0

    cfg_path = Path(args.config).resolve()
    cfg = json.loads(cfg_path.read_text())
    base = cfg_path.parent
    out = (base / cfg.get("runs", "runs") / args.label).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tool = Path(__file__).resolve().parent / "meetpp_replay.py"
    cmd = [sys.executable, "-u", str(tool), "--transcript", str(base / cfg["transcript"]), "--start", cfg["start"],
           "--out", str(out), "--people", cfg["people"], "--meeting-type", cfg.get("meeting_type", "board"),
           "--title", cfg.get("title", "Meeting")]
    for key, flag in (("agenda", "--agenda"), ("previous", "--previous")):
        if cfg.get(key):
            cmd += [flag, str(base / cfg[key])]
    for key, flag in (("chair", "--chair"), ("as_of", "--as-of")):
        if cfg.get(key):
            cmd += [flag, cfg[key]]
    log = out.parent / f"{args.label}.log"
    print(f"replaying into {out} (log {log}) …", flush=True)
    with open(log, "w") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=tool.parent.parent).returncode
    if rc != 0:
        print(f"replay failed (exit {rc}); see {log}")
        return rc
    card = score(out, json.loads((base / cfg["reference"]).read_text()))
    (out / "scorecard.json").write_text(json.dumps(card, indent=1))
    text = render(card)
    (out / "scorecard.txt").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
