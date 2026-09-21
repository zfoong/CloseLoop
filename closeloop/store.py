"""Persistence: multiple tasks, each with a contract, versioned trees, runs,
and pinned fixtures.

data/tasks/<task_id>/
  task.json                                  name, description, contract (ports), active_version
  tree_versions/v0001.json, ...              versioned trees (R-5.5)
  runs.jsonl                                 append-only run records (R-4.2)
  fixtures.jsonl                             pinned regression fixtures (R-20.8)
"""
import json
import os
import re
import shutil

from . import logsetup
from .config import ROOT

log = logsetup.get("store")

DATA_DIR = os.path.join(ROOT, "data")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")


def slugify(name):
    return (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "task")[:48]


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
        if os.path.isdir(_vdir(tid)):
            out.append(load_task(tid))
    return out


def task_exists(task_id):
    return os.path.isdir(_vdir(task_id))


def load_task(task_id):
    meta = {"id": task_id, "name": task_id}
    p = os.path.join(_tdir(task_id), "task.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            meta.update(json.load(f))
    meta["id"] = task_id
    return meta


def _save_task(task_id, meta):
    with open(os.path.join(_tdir(task_id), "task.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def create_task(name, meta):
    base = slugify(name)
    task_id, n = base, 2
    while os.path.isdir(_tdir(task_id)):
        task_id = f"{base}-{n}"; n += 1
    os.makedirs(_vdir(task_id), exist_ok=True)
    _save_task(task_id, {"name": name, "active_version": None, "pinned": False, **meta})
    log.info("task registered: id='%s' name='%s'", task_id, name)
    return task_id


def update_task(task_id, name=None, description=None):
    """Edit task metadata (display name / description). The I/O contract is fixed."""
    meta = load_task(task_id)
    if name is not None and name.strip():
        meta["name"] = name.strip()
    if description is not None:
        meta["description"] = description.strip()
    _save_task(task_id, meta)
    log.info("task updated: id='%s'", task_id)
    return meta


def delete_task(task_id):
    """Permanently remove a task and all its versions/runs/fixtures."""
    d = _tdir(task_id)
    if os.path.isdir(d):
        shutil.rmtree(d)
        log.info("task deleted: id='%s'", task_id)
        return True
    return False


# ------------------------------------------------------------- versions
def list_versions(task_id):
    if not os.path.isdir(_vdir(task_id)):
        return []
    return sorted(f[:-5] for f in os.listdir(_vdir(task_id)) if f.endswith(".json"))


def latest_version(task_id):
    vs = list_versions(task_id)
    return vs[-1] if vs else None


def active_version(task_id):
    """The version currently serving runs (pinned/rolled-back or latest)."""
    return load_task(task_id).get("active_version") or latest_version(task_id)


def load_tree(task_id, version=None):
    version = version or active_version(task_id)
    if version is None:
        return None, None
    with open(os.path.join(_vdir(task_id), version + ".json"), "r", encoding="utf-8") as f:
        return json.load(f), version


def save_tree(task_id, tree, reason):
    os.makedirs(_vdir(task_id), exist_ok=True)
    version = f"v{len(list_versions(task_id)) + 1:04d}"
    tree = dict(tree)
    tree["_meta"] = {"version": version, "reason": reason}
    with open(os.path.join(_vdir(task_id), version + ".json"), "w", encoding="utf-8") as f:
        json.dump(tree, f, indent=2)
    meta = load_task(task_id)
    if not meta.get("pinned"):  # a pinned task does not auto-advance
        meta["active_version"] = version
        _save_task(task_id, meta)
    log.info("tree saved: task='%s' %s | reason: %s", task_id, version, logsetup.preview(reason, 160))
    return version


def set_active(task_id, version, pin=None):
    """Roll back / forward to a version; optionally pin (freeze) it."""
    if version not in list_versions(task_id):
        raise ValueError(f"unknown version '{version}'")
    meta = load_task(task_id)
    meta["active_version"] = version
    if pin is not None:
        meta["pinned"] = bool(pin)
    _save_task(task_id, meta)
    log.info("task='%s' active_version set to %s (pinned=%s)", task_id, version, meta.get("pinned"))
    return meta


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
    return [json.loads(x) for x in lines[-limit:]]


def run_count(task_id):
    path = _runs_path(task_id)
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for x in f if x.strip())


# ------------------------------------------------------------- fixtures
def _fix_path(task_id):
    return os.path.join(_tdir(task_id), "fixtures.jsonl")


def pin_fixture(task_id, fixture):
    with open(_fix_path(task_id), "a", encoding="utf-8") as f:
        f.write(json.dumps(fixture) + "\n")
    log.info("task='%s' pinned a regression fixture", task_id)


def load_fixtures(task_id, limit=20):
    path = _fix_path(task_id)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(x) for x in f.readlines()[-limit:]]
