#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Score skills in same-use overlap groups on fleet tasks and decide retention.

For every same-use group from tools/overlap.py, scores each member skill on tasks from
tools/eval-tasks.json using the eval-harness method (deterministic regex rules decide pass/fail,
advisory model judge scores a rubric 0-10) across k trials per (skill, task). Decides
copy-of before model calls, then evaluates only the primary function's three tasks.
Rule gaps >= 0.34 decide keep-better; opposite 3/3 versus 0/3 task wins decide keep-both.
Otherwise a person decides. Rubric scores are advisory; existing skills are never dropped.
Outputs verdicts.tsv and resumable trials.jsonl.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from classify import FUNCTIONS  # noqa: E402
from census import load_sources  # noqa: E402
from candidates import AGGREGATOR_UPSTREAM_KEYS, parse_github_repo  # noqa: E402
from check_skills import front_matter_or_empty  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from overlap import (  # noqa: E402
    Item, cosine_similarity, load_cache, load_candidates, load_existing_skills,
    normalize_name,
)

DEFAULT_MODEL = os.environ.get("PERITIA_LLM_MODEL")
DEFAULT_JUDGE_MODEL = os.environ.get("PERITIA_LLM_JUDGE_MODEL")
DEFAULT_K = 3
DEFAULT_TEMPERATURE = 0.7
DEFAULT_BASE_URL = os.environ.get("PERITIA_LLM_BASE_URL")
DEFAULT_TIMEOUT = 300.0
MAX_SKILL_CHARS = 6000
ATTEMPTS = 2

VERDICTS_FIELDS = ["group_id", "verdict", "decided_by", "kept", "dropped", "scores", "model", "judge_model",
                   "copies", "primary_class"]
DEFAULT_SOURCES = _TOOLS_DIR / "census-sources.json"

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


def primary_class(members: list[str], member_funcs: dict[str, set[str]]) -> str:
    """Vote once per member and function; taxonomy order breaks ties."""
    counts = Counter(fn for m in members for fn in member_funcs.get(m, set()) if fn != "none")
    eligible = [fn for fn in FUNCTIONS if counts[fn]]
    return max(eligible, key=lambda fn: counts[fn]) if eligible else ""


def _copy_original(
    a: str, b: str, items: dict[str, Item], metadata: dict[str, dict[str, str]],
    embeddings: dict[str, list[float]], ranks: dict[str, int],
) -> str | None:
    """Prefer explicit provenance, then source rank, for a cross-source near-copy pair."""
    sources = {m: m.split(":", 1)[0] if ":" in m else "" for m in (a, b)}
    if sources[a].lower() == sources[b].lower():
        return None
    named = set()
    for copy, original in ((a, b), (b, a)):
        for field in AGGREGATOR_UPSTREAM_KEYS:
            repo = parse_github_repo(metadata.get(copy, {}).get(field, ""))
            if repo and repo.lower() == sources[original].lower():
                named.add(original)
    if named:
        return next(iter(named)) if len(named) == 1 else None
    if a not in items or b not in items:
        return None
    name_a, name_b = normalize_name(items[a].skill_name), normalize_name(items[b].skill_name)
    if not name_a or name_a != name_b:
        return None
    va, vb = embeddings.get(items[a].blob_id), embeddings.get(items[b].blob_id)
    if not va or not vb or len(va) != len(vb) or not cosine_similarity(va, vb) >= 0.97:
        return None
    if any(sources[m] not in ranks for m in (a, b)):
        return None
    return min((a, b), key=lambda m: (ranks[sources[m]], sources[m], m))


def collapse_copies(
    members: list[str], items: dict[str, Item], metadata: dict[str, dict[str, str]],
    embeddings: dict[str, list[float]], ranks: dict[str, int],
) -> tuple[list[str], dict[str, str]]:
    """Resolve copy chains to one upstream; leave conflicts and cycles for evaluation."""
    targets: dict[str, set[str]] = {m: set() for m in members}
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            original = _copy_original(a, b, items, metadata, embeddings, ranks)
            if original:
                targets[b if original == a else a].add(original)
    resolved = {m: m for m in members if not targets[m]}
    for _ in range(len(members)):
        before = len(resolved)
        for m in members:
            if m in resolved or not targets[m].issubset(resolved):
                continue
            roots = {resolved[target] for target in targets[m]}
            if len(roots) == 1:
                resolved[m] = next(iter(roots))
        if len(resolved) == before:
            break
    copies = {m: f"copy-of:{root}" for m, root in resolved.items() if m != root}
    return [m for m in members if m not in copies], copies


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


class JudgeHarness:
    """Thin wrapper around LLMClient for task answers and rubric judging."""

    def __init__(
        self,
        client: LLMClient,
        model: str,
        judge_model: str,
    ) -> None:
        self.client = client
        self.model = model
        self.judge_model = judge_model

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = DEFAULT_TEMPERATURE, seed: int = 1) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self.client.chat(self.model, messages, temperature=temperature, seed=seed)

    def judge(self, prompt: str) -> float:
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        for _ in range(ATTEMPTS):
            try:
                content = self.client.chat_json(
                    self.judge_model, messages, schema=judge_schema(), schema_name="judge_rubric"
                )
                if isinstance(content, dict) and "score" in content:
                    score = float(content["score"])
                    return max(0.0, min(10.0, score))
            except (OSError, ValueError, KeyError, urllib.error.URLError):
                continue
        return 0.0


def _trial_state(records: list[dict[str, Any]], k: int) -> str:
    """Missing or duplicated trials cannot establish an all-pass or all-fail task."""
    if (len(records) != k or {r.get("trial") for r in records} != set(range(1, k + 1))
            or any(type(r.get("pass")) is not bool for r in records)):
        return "incomplete"
    passes = sum(r["pass"] for r in records)
    return "all-pass" if passes == k else "all-fail" if passes == 0 else "mixed"


def _compute_metrics(
    members: list[str], tasks: list[Task], trial_records: list[dict[str, Any]], k: int,
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], str]]:
    """Report pass@k and pass^k; preserve trial completeness and unanimous outcomes."""
    task_ids = {t.id for t in tasks}
    trials_by_mt: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for rec in trial_records:
        if rec["task_id"] in task_ids:
            trials_by_mt[(rec["member"], rec["task_id"])].append(rec)
    states, passed = {}, {}
    for m in members:
        for t in tasks:
            records = trials_by_mt[(m, t.id)]
            states[(m, t.id)] = _trial_state(records, k)
            passed[(m, t.id)] = float(any(r.get("pass") is True for r in records))
    stats = {}
    for m in members:
        rule_score = sum(passed[(m, t.id)] for t in tasks) / len(tasks) if tasks else 0.0
        reliable = sum(states[(m, t.id)] == "all-pass" for t in tasks) / len(tasks) if tasks else 0.0
        rubrics = [float(r.get("rubric_score", 0.0)) for t in tasks for r in trials_by_mt[(m, t.id)]]
        stats[m] = {
            "rule_score": rule_score,
            "judge_score": sum(rubrics) / len(rubrics) if rubrics else 0.0,
            "pass_at_k": rule_score,
            "pass_pow_k": reliable,
            "tasks_won": [t.id for t in tasks if states[(m, t.id)] == "all-pass"
                          and any(states[(o, t.id)] == "all-fail" for o in members if o != m)],
        }
    return stats, states


def _distinct_wins(
    a: str, b: str, tasks: list[Task], states: dict[tuple[str, str], str],
) -> list[str]:
    return [t.id for t in tasks if states[(a, t.id)] == "all-pass" and states[(b, t.id)] == "all-fail"]


def decide_group(
    members: list[str], tasks: list[Task], trial_records: list[dict[str, Any]], k: int = DEFAULT_K,
) -> tuple[str, list[str], list[str], dict[str, Any], str]:
    """Settle only a clear rule gap or mutual unanimous task wins; rubric is advisory."""
    if k < 1:
        raise ValueError("k must be positive")
    member_stats, states = _compute_metrics(members, tasks, trial_records, k)
    scores = {m: {key: round(value, 4) if isinstance(value, float) else value
                  for key, value in stats.items()} for m, stats in member_stats.items()}
    undecided = ("person-decides", list(members), [], scores, "none")
    if not members or not tasks or "incomplete" in states.values():
        return undecided
    if len(members) == 1:
        return "keep-better", members, [], scores, "rule"
    ordered = sorted(members, key=lambda m: (-member_stats[m]["rule_score"], m))
    top, others = ordered[0], ordered[1:]
    top_score = Decimal(str(member_stats[top]["rule_score"]))
    if all(top_score - Decimal(str(member_stats[o]["rule_score"])) >= Decimal("0.34") for o in others):
        verdict = "replace-proposed" if any(is_existing_skill(o) for o in others) else "keep-better"
        return verdict, [top], [o for o in others if not is_existing_skill(o)], scores, "rule"
    # Every pair must have evidence; a single pair cannot justify dropping a third member.
    if k == 3:
        wins = {m: {o: _distinct_wins(m, o, tasks, states) for o in members if o != m} for m in members}
        if all(task_ids for opponents in wins.values() for task_ids in opponents.values()):
            for m in members:
                scores[m]["distinct_use_vs"] = wins[m]
            return "keep-both", list(members), [], scores, "rule"
    return undecided


def _run_single_group(
    g: dict[str, Any],
    eval_tasks: dict[str, list[Task]],
    member_funcs: dict[str, set[str]],
    done_trials: dict[tuple[str, str, int], dict[str, Any]],
    checkouts: Path, skills_dir: Path, trials_out: Path,
    chat_fn: Callable[[str, str, float, int], str],
    judge_fn: Callable[[str], float],
    model: str, judge_model: str, k: int, temperature: float,
    items: dict[str, Item],
    embeddings: dict[str, list[float]],
    ranks: dict[str, int],
) -> dict[str, Any]:
    """Evaluates a single group across tasks, recording new trials and returning a verdict row."""
    group_id, members = g["group_id"], g["members"]
    texts = {m: get_skill_text(m, checkouts, skills_dir) for m in members}
    metadata = {m: front_matter_or_empty(text) for m, text in texts.items()}
    members, copies = collapse_copies(members, items, metadata, embeddings, ranks)
    primary = primary_class(members, member_funcs)
    group_tasks = eval_tasks.get(primary, [])
    if primary and len(group_tasks) != 3:
        raise ValueError(f"{primary}: expected exactly three evaluation tasks")

    group_trials: list[dict[str, Any]] = []
    for m in members:
        skill_text = texts[m]
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
    if verdict == "keep-better" and any(is_existing_skill(m) for m in copies):
        verdict = "replace-proposed"
    return {
        "group_id": group_id,
        "verdict": verdict,
        "decided_by": decided_by,
        "kept": ",".join(kept),
        "dropped": ",".join(dropped),
        "scores": json.dumps(scores, sort_keys=True),
        "model": model,
        "judge_model": judge_model,
        "copies": json.dumps(copies, sort_keys=True),
        "primary_class": primary,
    }


def _init_callables(
    base_url: str | None,
    model: str,
    judge_model: str,
    timeout: float,
    chat_fn: Callable[[str, str, float, int], str] | None,
    judge_fn: Callable[[str], float] | None,
) -> tuple[Callable[[str, str, float, int], str], Callable[[str], float]]:
    """Initialises chat and judge functions using LLMClient if either is absent."""
    if chat_fn is not None and judge_fn is not None:
        return chat_fn, judge_fn
    base_url_str = base_url or os.environ.get("PERITIA_LLM_BASE_URL") or ""
    if not base_url_str:
        raise ValueError("missing base_url (or set PERITIA_LLM_BASE_URL)")
    client = LLMClient(base_url_str, timeout, api_key=os.environ.get("PERITIA_LLM_API_KEY"))
    harness = JudgeHarness(client, model, judge_model)
    return chat_fn or harness.chat, judge_fn or harness.judge


def _write_verdicts(verdicts_out: Path, verdicts_rows: list[dict[str, Any]]) -> None:
    """Writes verdict rows to the verdicts TSV file."""
    verdicts_out.parent.mkdir(parents=True, exist_ok=True)
    with verdicts_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=VERDICTS_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for r in verdicts_rows:
            writer.writerow(r)


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
    model: str | None = DEFAULT_MODEL,
    judge_model: str | None = DEFAULT_JUDGE_MODEL,
    base_url: str | None = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    k: int = DEFAULT_K,
    temperature: float = DEFAULT_TEMPERATURE,
    limit: int | None = None,
    cache_path: Path | None = None,
    sources_path: Path = DEFAULT_SOURCES,
) -> list[dict[str, Any]]:
    """Runs evaluation on groups, writing verdicts.tsv and trials.jsonl."""
    if k < 1:
        raise ValueError("k must be positive")
    groups = load_groups(groups_tsv)
    if limit is not None:
        groups = groups[:limit]

    eval_tasks = load_eval_tasks(eval_tasks_json)
    member_funcs = load_member_functions(candidates_tsv, classified_tsv)
    done_trials = load_done_trials(trials_out)
    ranks = {s.repository: s.rank for s in load_sources(sources_path)}
    if cache_path is not None and not cache_path.is_file():
        raise ValueError(f"embedding cache not found: {cache_path}")
    embeddings = load_cache(cache_path)
    candidates = load_candidates(candidates_tsv, checkouts) if candidates_tsv is not None else []
    items = {item.item_id: item for item in candidates + load_existing_skills(skills_dir)}

    model_str = model or os.environ.get("PERITIA_LLM_MODEL") or ""
    judge_model_str = judge_model or os.environ.get("PERITIA_LLM_JUDGE_MODEL") or ""
    chat_fn, judge_fn = _init_callables(base_url, model_str, judge_model_str, timeout, chat_fn, judge_fn)

    verdicts_rows = [
        _run_single_group(
            g, eval_tasks, member_funcs, done_trials, checkouts, skills_dir,
            trials_out, chat_fn, judge_fn, model_str, judge_model_str, k, temperature, items, embeddings, ranks,
        )
        for g in groups
    ]

    _write_verdicts(verdicts_out, verdicts_rows)
    return verdicts_rows


def summary(verdicts_path: Path) -> str:
    """Summarises verdicts: counts per verdict, kept/dropped counts, and models used."""
    if not verdicts_path.exists():
        return "groups: 0\nkeep-better: 0\nkeep-both: 0\nperson-decides: 0\nreplace-proposed: 0\ntotal kept skills: 0\ntotal dropped skills: 0\nmodels: none\njudge models: none"
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
        f"person-decides: {counts['person-decides']}",
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
    run_cmd.add_argument("--cache", type=Path, default=None, help="overlap stage blob-id embedding cache")
    run_cmd.add_argument("--sources", type=Path, default=DEFAULT_SOURCES, help="ranked source manifest")
    run_cmd.add_argument("--checkouts", type=Path, default=Path("checkouts"), help="folder of checkouts")
    run_cmd.add_argument("--skills-dir", type=Path, default=Path("skills"), help="existing skills folder")
    run_cmd.add_argument("--eval-tasks", type=Path, default=Path("tools/eval-tasks.json"), help="eval tasks JSON")
    run_cmd.add_argument("--out", type=Path, default=Path("verdicts.tsv"), help="output verdicts TSV")
    run_cmd.add_argument("--trials-out", type=Path, default=Path("trials.jsonl"), help="output trials JSONL")
    run_cmd.add_argument("--model", default=DEFAULT_MODEL, help="model name for answering tasks (PERITIA_LLM_MODEL)")
    run_cmd.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL, help="model name for rubric judging (PERITIA_LLM_JUDGE_MODEL)")
    run_cmd.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenAI-compatible base URL (PERITIA_LLM_BASE_URL)")
    run_cmd.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="request timeout in seconds")
    run_cmd.add_argument("-k", "--k", type=int, default=DEFAULT_K, help="trials per task")
    run_cmd.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="temperature for generation")
    run_cmd.add_argument("--limit", type=int, default=None, help="limit number of groups to evaluate")

    sum_cmd = commands.add_parser("summary", help="summarise verdicts TSV")
    sum_cmd.add_argument("--verdicts", type=Path, default=Path("verdicts.tsv"), help="verdicts TSV file")

    args = parser.parse_args(argv)
    if args.command == "run":
        if not args.base_url:
            parser.error("missing --base-url (or set PERITIA_LLM_BASE_URL)")
        if not args.model:
            parser.error("missing --model (or set PERITIA_LLM_MODEL)")
        if not args.judge_model:
            parser.error("missing --judge-model (or set PERITIA_LLM_JUDGE_MODEL)")
    return args


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
        cache_path=args.cache,
        sources_path=args.sources,
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
