"""Persistence: multiple tasks, each with tree versions and run records.

data/tasks/<task_id>/
  task.json                                  (name, description, created by whom)
  tree_versions/v0001.json, v0002.json, ...  (R-5.5: versioned, rollbackable)
  runs.jsonl                                 (R-4.2: append-only run records)
"""
import json
import os
import re

from . import logsetup
from .config import ROOT

log = logsetup.get("store")

DATA_DIR = os.path.join(ROOT, "data")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "task"
    return slug[:48]


def _tdir(task_id):
    return os.path.join(TASKS_DIR, task_id)


def _vdir(task_id):
    return os.path.join(_tdir(task_id), "tree_versions")


# ---------------------------------------------------------------- tasks
def list_tasks():
    if not os.path.isdir(TASKS_DIR):
        return []
    out = []
    for tid in sorted(os.listdir(TASKS_DIR)):
        meta_path = os.path.join(_tdir(tid), "task.json")
        meta = {"id": tid, "name": tid}
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta.update(json.load(f))
        meta["id"] = tid
        if os.path.isdir(_vdir(tid)):
            out.append(meta)
    return out


def task_exists(task_id):
    return os.path.isdir(_vdir(task_id))


def create_task(name, meta):
    base = slugify(name)
    task_id, n = base, 2
    while os.path.isdir(_tdir(task_id)):
        task_id = f"{base}-{n}"
        n += 1
    os.makedirs(_vdir(task_id), exist_ok=True)
    with open(os.path.join(_tdir(task_id), "task.json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, **meta}, f, indent=2)
    log.info("task registered: id='%s' name='%s'", task_id, name)
    return task_id


# ------------------------------------------------------------- versions
def list_versions(task_id):
    if not os.path.isdir(_vdir(task_id)):
        return []
    return sorted(f[:-5] for f in os.listdir(_vdir(task_id)) if f.endswith(".json"))


def latest_version(task_id):
    versions = list_versions(task_id)
    return versions[-1] if versions else None


def load_tree(task_id, version=None):
    version = version or latest_version(task_id)
    if version is None:
        return None, None
    with open(os.path.join(_vdir(task_id), version + ".json"), "r", encoding="utf-8") as f:
        return json.load(f), version


def save_tree(task_id, tree, reason):
    os.makedirs(_vdir(task_id), exist_ok=True)
    n = len(list_versions(task_id)) + 1
    version = f"v{n:04d}"
    tree = dict(tree)
    tree["_meta"] = {"version": version, "reason": reason}
    with open(os.path.join(_vdir(task_id), version + ".json"), "w", encoding="utf-8") as f:
        json.dump(tree, f, indent=2)
    log.info("tree saved: task='%s' %s | reason: %s", task_id, version, logsetup.preview(reason, 160))
    return version


# ----------------------------------------------------------------- runs
def _runs_path(task_id):
    return os.path.join(_tdir(task_id), "runs.jsonl")


def append_run(task_id, record):
    os.makedirs(_tdir(task_id), exist_ok=True)
    with open(_runs_path(task_id), "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def load_runs(task_id, limit=200):
    path = _runs_path(task_id)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return [json.loads(line) for line in lines[-limit:]]


def run_count(task_id):
    path = _runs_path(task_id)
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())
