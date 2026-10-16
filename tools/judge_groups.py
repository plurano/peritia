#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Score skills in same-use overlap groups on fleet tasks and decide retention.

For every same-use group from tools/overlap.py, scores each member skill on tasks from
tools/eval-tasks.json using the eval-harness method (deterministic regex rules decide pass/fail,
advisory model judge scores a rubric 0-10) across k trials per (skill, task). Decides
keep-better (one member exceeds every other by >= 0.2 rule score, or by >= 2.0 judge score on
rule ties), keep-both (members win distinct tasks where the other fails), propose-merge (tie or
close score without distinct wins), or replace-proposed (an existing skill loses to a candidate;
existing skills are never dropped). Outputs verdicts.tsv (including decided_by) and trials.jsonl,
resuming previous trials without re-running.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

try:
    from classify import FUNCTIONS  # noqa: E402
except ImportError:
    FUNCTIONS = {}

DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_JUDGE_MODEL = "qwen2.5:7b"
DEFAULT_K = 3
DEFAULT_TEMPERATURE = 0.7
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 300.0
MAX_SKILL_CHARS = 6000
ATTEMPTS = 2

VERDICTS_FIELDS = ["group_id", "verdict", "decided_by", "kept", "dropped", "scores", "model", "judge_model"]

JUDGE_SYSTEM_PROMPT = (
    "You evaluate an AI assistant's response to an engineering task according to a rubric.\n"
    "Score the response from 0 to 10 based on how well it satisfies the rubric criteria.\n"
    "Answer in JSON with 'score' (number from 0 to 10) and 'reason' (brief explanation, at most 200 characters)."
)


def judge_schema() -> dict[str, Any]:
    """JSON schema constraining model judge answer to score and reason."""
    return {
        "type": "object",
        "properties": {
            "score": {"type": "number", "minimum": 0, "maximum": 10},
            "reason": {"type": "string", "maxLength": 200},
        },
        "required": ["score"],
    }


def make_judge_prompt(prompt: str, rubric: str, answer: str) -> str:
    """Builds prompt containing task prompt, rubric, and answer in markers."""
    return (
        f"<task_prompt>\n{prompt}\n</task_prompt>\n"
        f"<rubric>\n{rubric}\n</rubric>\n"
        f"<answer>\n{answer}\n</answer>"
    )


def is_existing_skill(member: str) -> bool:
    """True when skill is an existing repo skill under skills/."""
    return member.startswith("skills/")


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    must_match: tuple[str, ...]
    must_not_match: tuple[str, ...]
    rubric: str


def load_eval_tasks(eval_tasks_path: Path) -> dict[str, list[Task]]:
    """Loads eval tasks grouped by function class and validates regex compilation."""
    with eval_tasks_path.open(encoding="utf-8") as f:
        data = json.load(f)
    version = data.get("version")
    if version != 1:
        raise ValueError(f"unsupported eval-tasks version: {version}")
    classes = data.get("classes", {})
    result: dict[str, list[Task]] = {}
    for cls_name, tasks_list in classes.items():
        parsed_tasks = []
        for t in tasks_list:
            for pat in t.get("must_match", []):
                re.compile(pat, re.IGNORECASE)
            for pat in t.get("must_not_match", []):
                re.compile(pat, re.IGNORECASE)
            parsed_tasks.append(
                Task(
                    id=t["id"],
                    prompt=t["prompt"],
                    must_match=tuple(t.get("must_match", [])),
                    must_not_match=tuple(t.get("must_not_match", [])),
                    rubric=t.get("rubric", ""),
                )
            )
        result[cls_name] = parsed_tasks
    return result


def load_member_functions(
    candidates_tsv: Path | None = None,
    classified_tsv: Path | None = None,
) -> dict[str, set[str]]:
    """Maps skill identifiers/paths to set of assigned function labels."""
    mapping: dict[str, set[str]] = defaultdict(set)
    for path in (candidates_tsv, classified_tsv):
        if path is None or not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                funcs = {
                    f.strip()
                    for f in row.get("functions", "").split(";")
                    if f.strip() and f.strip() != "none"
                }
                source = row.get("source", "").strip()
                p = row.get("path", "").strip()
                name = row.get("skill_name", "").strip()
                blob = row.get("blob_id", "").strip()
                if source and p:
                    mapping[f"{source}:{p}"].update(funcs)
                if p:
                    mapping[p].update(funcs)
                    parts = Path(p).parts
                    if len(parts) >= 2 and parts[0] == "skills":
                        mapping[f"skills/{parts[1]}"].update(funcs)
                if name:
                    mapping[name].update(funcs)
                    mapping[f"skills/{name}"].update(funcs)
                if blob:
                    mapping[blob].update(funcs)
    return mapping


def get_skill_text(member: str, checkouts: Path, skills_dir: Path) -> str:
    """Reads first MAX_SKILL_CHARS of member's SKILL.md confined to checkouts or skills_dir."""
    if is_existing_skill(member):
        rel = member[len("skills/"):].strip("/")
        target = (skills_dir / rel).resolve()
        if target.is_dir():
            target = (target / "SKILL.md").resolve()
        elif not target.is_file() and (skills_dir / rel / "SKILL.md").is_file():
            target = (skills_dir / rel / "SKILL.md").resolve()
        resolved_skills = skills_dir.resolve()
        if resolved_skills not in target.parents and target != resolved_skills:
            raise ValueError(f"{member}: path leaves skills dir")
        if target.is_file():
            with target.open(encoding="utf-8", errors="replace") as f:
                return f.read(MAX_SKILL_CHARS)
        return ""
    else:
        if ":" in member:
            source, path_str = member.split(":", 1)
        else:
            source, path_str = "", member
        root = (checkouts / source.replace("/", "__")).resolve()
        target = (root / path_str).resolve()
        if root not in target.parents and target != root:
            raise ValueError(f"{member}: path leaves checkout")
        if target.is_file():
            with target.open(encoding="utf-8", errors="replace") as f:
                return f.read(MAX_SKILL_CHARS)
        return ""


def grade_answer(
    answer: str,
    must_match: list[str] | tuple[str, ...],
    must_not_match: list[str] | tuple[str, ...],
) -> tuple[bool, list[str]]:
    """Deterministic rule assertions; all must_match must match, none of must_not_match may match."""
    failed: list[str] = []
    for pat in must_match:
        try:
            if not re.search(pat, answer, re.IGNORECASE):
                failed.append(f"missing: {pat}")
        except re.error as e:
            failed.append(f"invalid regex {pat}: {e}")
    for pat in must_not_match:
        try:
            if re.search(pat, answer, re.IGNORECASE):
                failed.append(f"unexpected: {pat}")
        except re.error as e:
            failed.append(f"invalid regex {pat}: {e}")
    return len(failed) == 0, failed


def load_groups(groups_tsv: Path) -> list[dict[str, Any]]:
    """Loads groups from groups.tsv."""
    groups: list[dict[str, Any]] = []
    if not groups_tsv.exists():
        return groups
    with groups_tsv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            members = [m.strip() for m in row.get("members", "").split(",") if m.strip()]
            groups.append({
                "group_id": row.get("group_id", ""),
                "members": members,
                "edges": row.get("edges", ""),
                "has_existing": row.get("has_existing", "false") == "true",
            })
    return groups


def load_done_trials(trials_path: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Loads completed trials mapping (member, task_id, trial) -> record."""
    done: dict[tuple[str, str, int], dict[str, Any]] = {}
    if not trials_path.exists():
        return done
    with trials_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                member = record["member"]
                task_id = record["task_id"]
                trial_num = int(record.get("trial", record.get("seed", 1)))
                done[(member, task_id, trial_num)] = record
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
    return done


def append_trial(trials_path: Path, record: dict[str, Any]) -> None:
    """Appends one trial JSON record to trials.jsonl."""
    trials_path.parent.mkdir(parents=True, exist_ok=True)
    with trials_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()


class OllamaClient:
    """The only network boundary: /api/chat calls for task answers and rubric judging."""

    def __init__(
        self,
        base_url: str,
        model: str,
        judge_model: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.judge_model = judge_model
        self.timeout = timeout

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = DEFAULT_TEMPERATURE, seed: int = 1) -> str:
        body = {
            "model": self.model,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {"temperature": temperature, "seed": seed},
        }
        request = urllib.request.Request(
            self.base_url + "/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:  # noqa: S310
            reply = json.loads(resp.read().decode("utf-8"))
        return reply["message"]["content"]

    def judge(self, prompt: str) -> float:
        for _ in range(ATTEMPTS):
            try:
                body = {
                    "model": self.judge_model,
                    "stream": False,
                    "think": False,
                    "messages": [
                        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "format": judge_schema(),
                    "options": {"temperature": 0, "seed": 1},
                }
                request = urllib.request.Request(
                    self.base_url + "/api/chat",
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:  # noqa: S310
                    reply = json.loads(resp.read().decode("utf-8"))
                content = json.loads(reply["message"]["content"])
                if isinstance(content, dict) and "score" in content:
                    score = float(content["score"])
                    return max(0.0, min(10.0, score))
            except (OSError, ValueError, KeyError, urllib.error.URLError):
                continue
        return 0.0


def _compute_metrics(
    members: list[str],
    tasks: list[Task],
    trial_records: list[dict[str, Any]],
    k: int,
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], float]]:
    """Computes pass_at_k, pass_pow_k, and member summary stats."""
    trials_by_mt: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for rec in trial_records:
        trials_by_mt[(rec["member"], rec["task_id"])].append(rec)

    pass_at_k: dict[tuple[str, str], float] = {}
    pass_pow_k: dict[tuple[str, str], float] = {}
    for m in members:
        for t in tasks:
            t_trials = trials_by_mt.get((m, t.id), [])
            passes = [bool(r.get("pass", False)) for r in t_trials]
            pass_at_k[(m, t.id)] = 1.0 if any(passes) else 0.0
            pass_pow_k[(m, t.id)] = 1.0 if (len(passes) >= k and all(passes)) else 0.0

    stats: dict[str, dict[str, Any]] = {}
    num_tasks = len(tasks)
    for m in members:
        rule_score = (sum(pass_at_k[(m, t.id)] for t in tasks) / num_tasks) if num_tasks > 0 else 0.0
        pass_pow = (sum(pass_pow_k[(m, t.id)] for t in tasks) / num_tasks) if num_tasks > 0 else 0.0
        all_trials = [r for r in trial_records if r.get("member") == m]
        rubrics = [float(r.get("rubric_score", 0.0)) for r in all_trials]
        judge_score = (sum(rubrics) / len(rubrics)) if rubrics else 0.0
        others = [o for o in members if o != m]
        tasks_won = [
            t.id for t in tasks
            if pass_at_k[(m, t.id)] == 1.0 and any(pass_at_k[(o, t.id)] == 0.0 for o in others)
        ]
        stats[m] = {
            "rule_score": rule_score,
            "judge_score": judge_score,
            "pass_at_k": rule_score,
            "pass_pow_k": pass_pow,
            "tasks_won": tasks_won,
        }
    return stats, pass_at_k


def _find_distinct_pair(
    members: list[str],
    tasks: list[Task],
    pass_at_k: dict[tuple[str, str], float],
) -> tuple[str, str] | None:
    """Finds two members who each win at least one task where the other fails."""
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            a_wins = any(pass_at_k[(a, t.id)] > pass_at_k[(b, t.id)] for t in tasks)
            b_wins = any(pass_at_k[(b, t.id)] > pass_at_k[(a, t.id)] for t in tasks)
            if a_wins and b_wins:
                return (a, b)
    return None


def _judge_tie_break(
    members: list[str],
    member_stats: dict[str, Any],
    scores_out: dict[str, Any],
) -> tuple[str, list[str], list[str], dict[str, Any], str] | None:
    """Breaks a rule-score tie if top judge score exceeds others by >= 2.0."""
    sorted_by_judge = sorted(
        members,
        key=lambda m: (-round(member_stats[m]["judge_score"], 6), -round(member_stats[m]["rule_score"], 6), m),
    )
    top_judge, others_judge = sorted_by_judge[0], sorted_by_judge[1:]
    top_judge_score = member_stats[top_judge]["judge_score"]
    if all(round(top_judge_score - member_stats[o]["judge_score"], 6) >= 2.0 for o in others_judge):
        losing_existing = [o for o in others_judge if is_existing_skill(o)]
        dropped = [o for o in others_judge if not is_existing_skill(o)]
        verdict = "replace-proposed" if losing_existing else "keep-better"
        return verdict, [top_judge], dropped, scores_out, "judge"
    return None


def decide_group(
    members: list[str],
    tasks: list[Task],
    trial_records: list[dict[str, Any]],
    k: int = DEFAULT_K,
) -> tuple[str, list[str], list[str], dict[str, Any], str]:
    """Decides keep-better, keep-both, propose-merge, or replace-proposed for a group."""
    if not members:
        return "propose-merge", [], [], {}, "none"

    member_stats, pass_at_k = _compute_metrics(members, tasks, trial_records, k)
    scores_out = {
        m: {
            "rule_score": round(member_stats[m]["rule_score"], 4),
            "judge_score": round(member_stats[m]["judge_score"], 4),
            "pass_at_k": round(member_stats[m]["pass_at_k"], 4),
            "pass_pow_k": round(member_stats[m]["pass_pow_k"], 4),
            "tasks_won": member_stats[m]["tasks_won"],
        }
        for m in members
    }
    if len(members) == 1:
        return "keep-better", members, [], scores_out, "rule"

    sorted_members = sorted(
        members,
        key=lambda m: (-round(member_stats[m]["rule_score"], 6), -round(member_stats[m]["judge_score"], 6), m),
    )
    top, others = sorted_members[0], sorted_members[1:]
    top_score = member_stats[top]["rule_score"]

    if all(round(top_score - member_stats[o]["rule_score"], 6) >= 0.20 for o in others):
        losing_existing = [o for o in others if is_existing_skill(o)]
        dropped = [o for o in others if not is_existing_skill(o)]
        verdict = "replace-proposed" if losing_existing else "keep-better"
        return verdict, [top], dropped, scores_out, "rule"

    # Rule gap < 0.2: check if top judge score exceeds every other member by >= 2.0
    judge_decision = _judge_tie_break(members, member_stats, scores_out)
    if judge_decision is not None:
        return judge_decision

    distinct_pair = _find_distinct_pair(members, tasks, pass_at_k)
    if distinct_pair:
        a, b = distinct_pair
        a_over_b = [t.id for t in tasks if pass_at_k[(a, t.id)] > pass_at_k[(b, t.id)]]
        b_over_a = [t.id for t in tasks if pass_at_k[(b, t.id)] > pass_at_k[(a, t.id)]]
        scores_out[a]["distinct_use_vs"] = {b: a_over_b}
        scores_out[b]["distinct_use_vs"] = {a: b_over_a}
        kept = [a, b]
        dropped = [m for m in members if m not in kept and not is_existing_skill(m)]
        return "keep-both", kept, dropped, scores_out, "rule"

    return "propose-merge", list(members), [], scores_out, "none"


def _run_single_group(
    g: dict[str, Any],
    eval_tasks: dict[str, list[Task]],
    member_funcs: dict[str, set[str]],
    done_trials: dict[tuple[str, str, int], dict[str, Any]],
    checkouts: Path,
    skills_dir: Path,
    trials_out: Path,
    chat_fn: Callable[[str, str, float, int], str],
    judge_fn: Callable[[str], float],
    model: str,
    judge_model: str,
    k: int,
    temperature: float,
) -> dict[str, Any]:
    """Evaluates a single group across tasks, recording new trials and returning a verdict row."""
    group_id, members = g["group_id"], g["members"]
    union_funcs: set[str] = set()
    for m in members:
        union_funcs.update(member_funcs.get(m, set()))

    group_tasks: list[Task] = []
    for fn in sorted(union_funcs):
        if fn in eval_tasks:
            group_tasks.extend(eval_tasks[fn])

    group_trials: list[dict[str, Any]] = []
    for m in members:
        skill_text = get_skill_text(m, checkouts, skills_dir)
        for t in group_tasks:
            for trial_idx in range(1, k + 1):
                key = (m, t.id, trial_idx)
                if key in done_trials:
                    rec = done_trials[key]
                else:
                    ans = chat_fn(skill_text, t.prompt, temperature, trial_idx)
                    passed, failed = grade_answer(ans, t.must_match, t.must_not_match)
                    score = judge_fn(make_judge_prompt(t.prompt, t.rubric, ans))
                    rec = {
                        "group_id": group_id, "member": m, "task_id": t.id,
                        "trial": trial_idx, "seed": trial_idx, "answer_length": len(ans),
                        "pass": passed, "failed_assertions": failed, "rubric_score": score,
                        "model": model, "judge_model": judge_model,
                    }
                    append_trial(trials_out, rec)
                    done_trials[key] = rec
                group_trials.append(rec)

    verdict, kept, dropped, scores, decided_by = decide_group(members, group_tasks, group_trials, k=k)
    return {
        "group_id": group_id,
        "verdict": verdict,
        "decided_by": decided_by,
        "kept": ",".join(kept),
        "dropped": ",".join(dropped),
        "scores": json.dumps(scores, sort_keys=True),
        "model": model,
        "judge_model": judge_model,
    }


def run(
    groups_tsv: Path,
    eval_tasks_json: Path,
    checkouts: Path,
    skills_dir: Path,
    verdicts_out: Path,
    trials_out: Path,
    candidates_tsv: Path | None = None,
    classified_tsv: Path | None = None,
    chat_fn: Callable[[str, str, float, int], str] | None = None,
    judge_fn: Callable[[str], float] | None = None,
    model: str = DEFAULT_MODEL,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    k: int = DEFAULT_K,
    temperature: float = DEFAULT_TEMPERATURE,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Runs evaluation on groups, writing verdicts.tsv and trials.jsonl."""
    groups = load_groups(groups_tsv)
    if limit is not None:
        groups = groups[:limit]

    eval_tasks = load_eval_tasks(eval_tasks_json)
    member_funcs = load_member_functions(candidates_tsv, classified_tsv)
    done_trials = load_done_trials(trials_out)

    if chat_fn is None or judge_fn is None:
        client = OllamaClient(base_url, model, judge_model, timeout)
        chat_fn = chat_fn or client.chat
        judge_fn = judge_fn or client.judge

    verdicts_rows = [
        _run_single_group(
            g, eval_tasks, member_funcs, done_trials, checkouts, skills_dir,
            trials_out, chat_fn, judge_fn, model, judge_model, k, temperature,
        )
        for g in groups
    ]

    verdicts_out.parent.mkdir(parents=True, exist_ok=True)
    with verdicts_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=VERDICTS_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for r in verdicts_rows:
            writer.writerow(r)

    return verdicts_rows


def summary(verdicts_path: Path) -> str:
    """Summarises verdicts: counts per verdict, kept/dropped counts, and models used."""
    if not verdicts_path.exists():
        return "groups: 0\nkeep-better: 0\nkeep-both: 0\npropose-merge: 0\nreplace-proposed: 0\ntotal kept skills: 0\ntotal dropped skills: 0\nmodels: none\njudge models: none"
    counts: Counter = Counter()
    total_kept = 0
    total_dropped = 0
    models: set[str] = set()
    judge_models: set[str] = set()
    total_groups = 0
    with verdicts_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            total_groups += 1
            v = row.get("verdict", "")
            counts[v] += 1
            kept = [m for m in row.get("kept", "").split(",") if m.strip()]
            dropped = [m for m in row.get("dropped", "").split(",") if m.strip()]
            total_kept += len(kept)
            total_dropped += len(dropped)
            if row.get("model"):
                models.add(row["model"])
            if row.get("judge_model"):
                judge_models.add(row["judge_model"])
    lines = [
        f"groups: {total_groups}",
        f"keep-better: {counts['keep-better']}",
        f"keep-both: {counts['keep-both']}",
        f"propose-merge: {counts['propose-merge']}",
        f"replace-proposed: {counts['replace-proposed']}",
        f"total kept skills: {total_kept}",
        f"total dropped skills: {total_dropped}",
        f"models: {', '.join(sorted(models)) if models else 'none'}",
        f"judge models: {', '.join(sorted(judge_models)) if judge_models else 'none'}",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="score group skills on eval tasks and decide retention")
    run_cmd.add_argument("--groups", type=Path, default=Path("groups.tsv"), help="groups TSV from overlap.py")
    run_cmd.add_argument("--candidates", type=Path, default=Path("candidates.tsv"), help="candidates TSV")
    run_cmd.add_argument("--classified", type=Path, default=Path("classified.tsv"), help="classified TSV")
    run_cmd.add_argument("--checkouts", type=Path, default=Path("checkouts"), help="folder of checkouts")
    run_cmd.add_argument("--skills-dir", type=Path, default=Path("skills"), help="existing skills folder")
    run_cmd.add_argument("--eval-tasks", type=Path, default=Path("tools/eval-tasks.json"), help="eval tasks JSON")
    run_cmd.add_argument("--out", type=Path, default=Path("verdicts.tsv"), help="output verdicts TSV")
    run_cmd.add_argument("--trials-out", type=Path, default=Path("trials.jsonl"), help="output trials JSONL")
    run_cmd.add_argument("--model", default=DEFAULT_MODEL, help="model name for answering tasks")
    run_cmd.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL, help="model name for rubric judging")
    run_cmd.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Ollama base URL")
    run_cmd.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="request timeout in seconds")
    run_cmd.add_argument("-k", "--k", type=int, default=DEFAULT_K, help="trials per task")
    run_cmd.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="temperature for generation")
    run_cmd.add_argument("--limit", type=int, default=None, help="limit number of groups to evaluate")

    sum_cmd = commands.add_parser("summary", help="summarise verdicts TSV")
    sum_cmd.add_argument("--verdicts", type=Path, default=Path("verdicts.tsv"), help="verdicts TSV file")

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "summary":
        print(summary(args.verdicts))
        return 0
    run(
        groups_tsv=args.groups,
        eval_tasks_json=args.eval_tasks,
        checkouts=args.checkouts,
        skills_dir=args.skills_dir,
        verdicts_out=args.out,
        trials_out=args.trials_out,
        candidates_tsv=args.candidates,
        classified_tsv=args.classified,
        model=args.model,
        judge_model=args.judge_model,
        base_url=args.base_url,
        timeout=args.timeout,
        k=args.k,
        temperature=args.temperature,
        limit=args.limit,
    )
    print(summary(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
