"""Flask app: item analysis for partially scanned answer cards.

Design notes
------------
* Every state-changing action (answer import, new form, code correction,
  review conclusion) is a job.  The job runs ``mutate -> full analysis ->
  persist new snapshot -> flip active_run_id`` inside one SQLite transaction.
  Either all of it commits or none of it does, so a failed save leaves the
  previous forms, codes, conclusions and statistics untouched.
* Jobs are processed by one background worker, FIFO.  While it runs, the
  previous snapshot remains the active one and stays fully readable
  ("重算期间可查原统计").
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import queue
import sqlite3
import threading
import uuid
from urllib.parse import quote

from flask import (
    Flask, Response, current_app, g, jsonify, redirect, render_template,
    request, url_for,
)

from . import engine
from .db import (
    active_run_id, connect, get_meta, init_db, reclaim_storage, set_meta,
    utcnow,
)

logger = logging.getLogger(__name__)

DEFAULT_DB = os.environ.get(
    "EXAM_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "app.db"),
)


def create_app(db_path: str | None = None, sync_jobs: bool | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or DEFAULT_DB
    staging_dir = os.environ.get("EXAM_STAGING_DIR")
    if not staging_dir:
        staging_dir = os.path.join(
            os.path.dirname(os.path.abspath(app.config["DB_PATH"])), "staging"
        )
    app.config["STAGING_DIR"] = staging_dir
    os.makedirs(app.config["STAGING_DIR"], exist_ok=True)
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
    # Tests may force synchronous job execution for determinism.
    if sync_jobs is None:
        sync_jobs = os.environ.get("EXAM_SYNC_JOBS") == "1"
    app.config["SYNC_JOBS"] = sync_jobs
    init_db(app.config["DB_PATH"])
    # Restart-safe cleanup: strip raw CSVs from finished jobs, remove orphaned
    # staging files and truncate the WAL so the data volume stays reusable.
    try:
        conn = connect(app.config["DB_PATH"])
        try:
            reclaim_storage(conn, app.config["STAGING_DIR"])
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        logger.exception("启动时清理暂存空间失败，将在下次启动重试")
    app.worker = JobWorker(
        app.config["DB_PATH"], app.config["STAGING_DIR"],
        sync=app.config["SYNC_JOBS"],
    )
    app.teardown_appcontext(close_db)
    register_routes(app)
    return app


# --------------------------------------------------------------------------
# DB connection per request
# --------------------------------------------------------------------------

def current_db_path() -> str:
    return current_app.config["DB_PATH"]


def get_db():
    if "db" not in g:
        g.db = connect(current_db_path())
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --------------------------------------------------------------------------
# Serialized job worker
# --------------------------------------------------------------------------

class JobWorker:
    def __init__(self, db_path: str, staging_dir: str, sync: bool = False):
        self.db_path = db_path
        self.staging_dir = staging_dir
        self.sync = sync
        self._queue: "queue.Queue[int]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        os.makedirs(self.staging_dir, exist_ok=True)
        if not sync:
            self._resume_queued()

    # -- staging files -----------------------------------------------------

    def _staging_path(self, name: str) -> str:
        # Never trust a stored name enough to leave the staging directory.
        return os.path.join(self.staging_dir, os.path.basename(name))

    def _write_staging(self, kind: str, text: str) -> str:
        safe_kind = "".join(ch for ch in kind if ch.isalnum() or ch == "_") or "job"
        name = f"{safe_kind}-{uuid.uuid4().hex}.csv"
        # Write to a temporary name first so an interrupted upload never leaves
        # a half-written file that could be mistaken for a valid CSV.
        tmp = f"{name}.tmp"
        with open(self._staging_path(tmp), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(self._staging_path(tmp), self._staging_path(name))
        return name

    def _load_csv_text(self, payload: dict) -> str | None:
        if "csv" in payload:  # legacy payload written by older versions
            return payload["csv"]
        name = payload.get("csv_file")
        if not name:
            return None
        try:
            with open(self._staging_path(name), "r",
                      encoding="utf-8", newline="") as fh:
                return fh.read()
        except FileNotFoundError:
            raise RuntimeError(f"暂存的 CSV 原文已不存在：{name}，请重新上传")
        except OSError as exc:
            raise RuntimeError(f"读取暂存 CSV 失败：{exc}")

    def _release_staging(self, conn, job_id: int) -> None:
        """Drop the raw CSV of a finished job so the staging space is reusable."""
        row = conn.execute(
            "SELECT payload FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return
        name = None
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            name = payload.get("csv_file")
            if "csv" in payload or "csv_file" in payload:
                payload.pop("csv", None)
                payload.pop("csv_file", None)
                try:
                    conn.execute(
                        "UPDATE jobs SET payload = ? WHERE id = ?",
                        (json.dumps(payload, ensure_ascii=False), job_id),
                    )
                except sqlite3.DatabaseError:
                    pass
        if name:
            try:
                os.remove(self._staging_path(name))
            except FileNotFoundError:
                pass
            except OSError:
                # Retried on the next startup by reclaim_storage().
                pass

    # -- queue -------------------------------------------------------------

    def _resume_queued(self) -> None:
        """Re-enqueue jobs that were accepted but not processed before a restart."""
        conn = connect(self.db_path)
        try:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM jobs WHERE status = 'queued' ORDER BY id"
            )]
        finally:
            conn.close()
        if not ids:
            return
        self._ensure_thread()
        for job_id in ids:
            self._queue.put(job_id)

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, daemon=True, name="analysis-worker",
                )
                self._thread.start()

    def submit(self, kind: str, payload: dict) -> int:
        # Raw CSV lives in the staging directory while the job is queued or
        # running; only its file name is persisted in the database, keeping
        # SQLite small enough for the next import even after repeated uploads.
        if "csv" in payload:
            name = self._write_staging(kind, payload["csv"])
            payload = {**payload, "csv_file": name}
            payload.pop("csv", None)
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO jobs(kind, payload, status, created_at) "
                "VALUES (?, ?, 'queued', ?)",
                (kind, json.dumps(payload, ensure_ascii=False), utcnow()),
            )
            job_id = cur.lastrowid
        finally:
            conn.close()
        if self.sync:
            self._process(job_id)
        else:
            self._ensure_thread()
            self._queue.put(job_id)
        return job_id

    def _loop(self):
        while True:
            job_id = self._queue.get()
            try:
                self._process(job_id)
            except Exception:
                pass
            finally:
                self._queue.task_done()

    def _process(self, job_id: int) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None or job["status"] != "queued":
                conn.execute("ROLLBACK")
                return
            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
                (utcnow(), job_id),
            )
            conn.execute("COMMIT")

            try:
                payload = json.loads(job["payload"])
                csv_text = self._load_csv_text(payload)
                if csv_text is not None:
                    payload["csv"] = csv_text
                run_id = _apply_mutation(conn, job["kind"], payload)
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? "
                    "WHERE id = ?",
                    (f"{type(exc).__name__}: {exc}", utcnow(), job_id),
                )
                return
            conn.execute(
                "UPDATE jobs SET status = 'succeeded', run_id = ?, finished_at = ? "
                "WHERE id = ?",
                (run_id, utcnow(), job_id),
            )
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            try:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? "
                    "WHERE id = ?",
                    (f"{type(exc).__name__}: {exc}", utcnow(), job_id),
                )
            except sqlite3.DatabaseError:
                pass
        finally:
            # The job has reached a terminal state: its raw CSV is no longer
            # needed and the staging space must be reusable.
            try:
                self._release_staging(conn, job_id)
            except Exception:
                logger.exception("释放作业 %s 的暂存 CSV 失败", job_id)
            conn.close()


def _apply_mutation(conn, kind: str, payload: dict) -> int:
    """Mutate + analyse + persist in one transaction. Returns new run id."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if kind == "upload_answers":
            _do_upload_answers(conn, payload)
        elif kind == "upload_form":
            _do_upload_form(conn, payload)
        elif kind == "correct_code":
            _do_correct_code(conn, payload)
        elif kind == "set_decision":
            _do_set_decision(conn, payload)
        else:
            raise ValueError(f"未知任务类型：{kind}")

        analysis = engine.compute(conn)
        run_id = engine.persist_run(conn, analysis, utcnow())
        set_meta(conn, "active_run_id", str(run_id))
        set_meta(conn, "state_version",
                 str(int(get_meta(conn, "state_version", "0")) + 1))
        engine.prune_runs(conn, keep=6)
        conn.execute("COMMIT")
        return run_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------

def _do_upload_answers(conn, payload):
    rows = engine.parse_answer_csv(payload["csv"].encode("utf-8"))
    conn.execute("DELETE FROM answer_rows")
    conn.executemany(
        "INSERT INTO answer_rows(card_no, q_no, letter, scanned_code) "
        "VALUES (?, ?, ?, ?)",
        [(r["card_no"], r["q_no"], r["letter"], r["scanned_code"]) for r in rows],
    )


def _do_upload_form(conn, payload):
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("卷式名称不能为空")
    mapped = engine.parse_form_csv(payload["csv"].encode("utf-8"))
    existing = conn.execute("SELECT id FROM forms WHERE name = ?", (name,)).fetchone()
    if existing:
        form_id = existing["id"]
        conn.execute("DELETE FROM form_mappings WHERE form_id = ?", (form_id,))
    else:
        form_id = conn.execute(
            "INSERT INTO forms(name, created_at) VALUES (?, ?)", (name, utcnow())
        ).lastrowid
    conn.executemany(
        "INSERT INTO form_mappings(form_id, q_no, letter, std_code, std_option) "
        "VALUES (?, ?, ?, ?, ?)",
        [(form_id, *row) for row in mapped],
    )


def _do_correct_code(conn, payload):
    form_id = int(payload.get("form_id") or 0)
    q_no = (payload.get("q_no") or "").strip()
    new_code = (payload.get("new_code") or "").strip()
    new_option = (payload.get("new_option") or "").strip()
    if not form_id or not q_no or not new_code:
        raise ValueError("请指定卷式、题号和新的标准题码")
    rows = conn.execute(
        "SELECT letter, std_code, std_option FROM form_mappings "
        "WHERE form_id = ? AND q_no = ?",
        (form_id, q_no),
    ).fetchall()
    if not rows:
        raise ValueError(f"该卷式中没有题号 {q_no}")
    old_code = rows[0]["std_code"]
    if any(r["std_code"] != old_code for r in rows):
        raise ValueError(f"题号 {q_no} 在当前卷式中映射不一致，请先补齐卷式")
    if new_option and len(rows) != 1:
        raise ValueError("该题号有多个选项字母，无法同时订正标准选项")
    conn.execute(
        "UPDATE form_mappings SET std_code = ? WHERE form_id = ? AND q_no = ?",
        (new_code, form_id, q_no),
    )
    if new_option:
        conn.execute(
            "UPDATE form_mappings SET std_option = ? WHERE form_id = ? AND q_no = ?",
            (new_option, form_id, q_no),
        )
    # A correction must not make the form itself invalid (e.g. two printed
    # questions mapping to one standard code).  Rolling back is handled by the
    # caller's transaction.
    after = conn.execute(
        "SELECT q_no, letter, std_code, std_option FROM form_mappings "
        "WHERE form_id = ? ORDER BY q_no, letter",
        (form_id,),
    ).fetchall()
    engine.validate_form_rows([tuple(r) for r in after])
    # Carry human conclusions across only if the old code vanished entirely.
    still = conn.execute(
        "SELECT 1 FROM form_mappings WHERE std_code = ? LIMIT 1", (old_code,)
    ).fetchone()
    if not still:
        conn.execute(
            "UPDATE OR REPLACE review_decisions SET std_code = ? WHERE std_code = ?",
            (new_code, old_code),
        )


def _do_set_decision(conn, payload):
    card_no = (payload.get("card_no") or "").strip()
    code = (payload.get("std_code") or "").strip()
    resolution = (payload.get("resolution") or "").strip()
    note = (payload.get("note") or "").strip()
    if not card_no or not code:
        raise ValueError("卡号和标准题码不能为空")
    if resolution not in ("skip", "blank") and not resolution.startswith("opt:"):
        raise ValueError("无效的核对结论")
    conn.execute(
        "INSERT INTO review_decisions(card_no, std_code, resolution, note, decided_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(card_no, std_code) DO UPDATE SET "
        "resolution = excluded.resolution, note = excluded.note, "
        "decided_at = excluded.decided_at",
        (card_no, code, resolution, note, utcnow()),
    )


# --------------------------------------------------------------------------
# Read helpers
# --------------------------------------------------------------------------

def read_run(conn, run_id: int) -> dict | None:
    if not run_id:
        return None
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return None

    stats: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT std_code, counted, blank, flagged FROM run_question_stats "
        "WHERE run_id = ? ORDER BY std_code",
        (run_id,),
    ):
        stats[r["std_code"]] = {
            "counted": r["counted"], "blank": r["blank"],
            "flagged": r["flagged"], "options": {},
        }
    for r in conn.execute(
        "SELECT std_code, std_option, cnt FROM run_option_counts WHERE run_id = ?",
        (run_id,),
    ):
        if r["std_code"] in stats:
            stats[r["std_code"]]["options"][r["std_option"]] = r["cnt"]

    cards: dict[str, list] = {}
    flagged: list[dict] = []
    for r in conn.execute(
        "SELECT card_no, std_code, status, std_option, detail FROM run_card_results "
        "WHERE run_id = ? AND std_code <> '' ORDER BY card_no, std_code",
        (run_id,),
    ):
        row = dict(r)
        cards.setdefault(r["card_no"], []).append(row)
        if r["status"] == "flagged":
            flagged.append(row)

    unmatched = [
        dict(r) for r in conn.execute(
            "SELECT card_no, detail FROM run_unmatched WHERE run_id = ? ORDER BY card_no",
            (run_id,),
        )
    ]

    return {
        "id": run["id"],
        "created_at": run["created_at"],
        "summary": json.loads(run["summary_json"] or "{}"),
        "stats": {code: stats[code] for code in sorted(stats, key=engine._natural_key)},
        "cards": cards,
        "flagged": flagged,
        "unmatched": unmatched,
        "all_options": sorted(
            {o for s in stats.values() for o in s["options"]},
            key=engine._natural_key,
        ),
        "decisions": {
            (r["card_no"], r["std_code"]): dict(r)
            for r in conn.execute(
                "SELECT * FROM review_decisions WHERE card_no IN "
                "(SELECT card_no FROM run_card_results WHERE run_id = ?) "
                "AND std_code IN (SELECT std_code FROM run_question_stats WHERE run_id = ?)",
                (run_id, run_id),
            )
        },
    }


def latest_job(conn):
    # Only summary columns: the polling endpoint must never parse or ship the
    # raw CSV payload.
    return conn.execute(
        "SELECT id, kind, status, error, created_at, started_at, finished_at, "
        "run_id FROM jobs ORDER BY id DESC LIMIT 1"
    ).fetchone()


JOB_SUMMARY_COLUMNS = (
    "id, kind, status, error, created_at, started_at, finished_at, run_id"
)


def job_view(job) -> dict | None:
    if job is None:
        return None
    return {
        "id": job["id"], "kind": job["kind"], "status": job["status"],
        "error": job["error"], "created_at": job["created_at"],
        "started_at": job["started_at"], "finished_at": job["finished_at"],
        "run_id": job["run_id"],
    }


def is_recomputing(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM jobs WHERE status IN ('queued', 'running') LIMIT 1"
    ).fetchone() is not None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def register_routes(app: Flask):

    @app.context_processor
    def inject_status():
        conn = get_db()
        return {
            "version": int(get_meta(conn, "state_version", "0")),
            "recomputing": is_recomputing(conn),
        }

    @app.get("/")
    def index():
        conn = get_db()
        current = active_run_id(conn)
        view = read_run(conn, current)
        runs = [
            {**dict(r), "summary": json.loads(r["summary_json"] or "{}")}
            for r in conn.execute(
                "SELECT id, created_at, summary_json FROM runs ORDER BY id DESC LIMIT 10"
            )
        ]
        forms = [dict(r) for r in conn.execute(
            "SELECT f.id, f.name, f.created_at, COUNT(m.q_no) AS n, "
            "COUNT(DISTINCT m.q_no) AS q_count "
            "FROM forms f LEFT JOIN form_mappings m ON m.form_id = f.id "
            "GROUP BY f.id ORDER BY f.name"
        )]
        card_count = conn.execute(
            "SELECT COUNT(DISTINCT card_no) AS n FROM answer_rows"
        ).fetchone()["n"]
        return render_template(
            "index.html", run=view, runs=runs, forms=forms, card_count=card_count,
            job=job_view(latest_job(conn)), recomputing=is_recomputing(conn),
            error=request.args.get("error"), message=request.args.get("message"),
        )

    @app.get("/runs/<int:run_id>")
    def run_detail(run_id: int):
        conn = get_db()
        view = read_run(conn, run_id)
        if view is None:
            return render_template("missing.html"), 404
        current = active_run_id(conn)
        review_card = request.args.get("card", "").strip()
        review_code = request.args.get("code", "").strip()
        review_rows = []
        if review_card:
            for cell in view["cards"].get(review_card, []):
                if review_code and cell["std_code"] != review_code:
                    continue
                review_rows.append(cell)
        return render_template(
            "run.html", run=view, is_current=(run_id == current),
            job=job_view(latest_job(conn)), recomputing=is_recomputing(conn),
            active_run_id=current,
            forms=conn.execute("SELECT id, name FROM forms ORDER BY name").fetchall(),
            review_card=review_card,
            review_code=review_code, review_rows=review_rows,
            error=request.args.get("error"), message=request.args.get("message"),
        )

    @app.get("/cards")
    def cards():
        conn = get_db()
        current = active_run_id(conn)
        view = read_run(conn, current)
        return render_template(
            "cards.html", run=view,
            error=request.args.get("error"), message=request.args.get("message"),
        )

    @app.post("/answers")
    def upload_answers():
        file = request.files.get("file")
        if file is None or not file.filename:
            return _back("请选择答题 CSV 文件")
        raw = file.read()
        try:
            rows = engine.parse_answer_csv(raw)
        except engine.ValidationError as exc:
            return _back(str(exc))
        try:
            app.worker.submit(
                "upload_answers", {"csv": engine.decode_csv(raw)}
            )
        except OSError as exc:
            return _back(f"暂存空间不足，无法保存 CSV 原文：{exc}")
        return _back(None, f"已受理 {len(rows)} 行答题数据，正在重算")

    @app.post("/forms")
    def upload_form():
        file = request.files.get("file")
        name = (request.form.get("name") or "").strip()
        if file is None or not file.filename:
            return _back("请选择卷式 CSV 文件")
        if not name:
            name = os.path.splitext(os.path.basename(file.filename))[0]
        raw = file.read()
        try:
            engine.parse_form_csv(raw)
        except engine.ValidationError as exc:
            return _back(str(exc))
        try:
            app.worker.submit(
                "upload_form",
                {"name": name, "csv": engine.decode_csv(raw)},
            )
        except OSError as exc:
            return _back(f"暂存空间不足，无法保存 CSV 原文：{exc}")
        return _back(None, f"卷式「{name}」已受理，正在重算")

    @app.post("/forms/correct")
    def correct_code():
        payload = {
            "form_id": request.form.get("form_id", ""),
            "q_no": (request.form.get("q_no") or "").strip(),
            "new_code": (request.form.get("new_code") or "").strip(),
            "new_option": (request.form.get("new_option") or "").strip(),
        }
        if not payload["form_id"] or not payload["q_no"] or not payload["new_code"]:
            return _back("请填写卷式、题号和新的标准题码")
        app.worker.submit("correct_code", payload)
        return _back(None, f"题号 {payload['q_no']} 的题码订正已受理，正在重算")

    @app.post("/review")
    def review():
        card_no = (request.form.get("card_no") or "").strip()
        code = (request.form.get("std_code") or "").strip()
        resolution = (request.form.get("resolution") or "").strip()
        note = (request.form.get("note") or "").strip()
        if not card_no or not code:
            return _back("卡号和标准题码不能为空")
        if resolution not in ("skip", "blank") and not resolution.startswith("opt:"):
            return _back("无效的核对结论")
        app.worker.submit("set_decision", {
            "card_no": card_no, "std_code": code,
            "resolution": resolution, "note": note,
        })
        return _back(None, f"卡号 {card_no} / 题码 {code} 的核对结论已受理")

    @app.get("/api/status")
    def api_status():
        conn = get_db()
        # Polling reads job summaries only (queued/running included); payloads
        # with raw CSV references are never parsed here or sent to the browser.
        jobs = [
            job_view(r) for r in conn.execute(
                f"SELECT {JOB_SUMMARY_COLUMNS} FROM jobs "
                "ORDER BY id DESC LIMIT 6"
            )
        ]
        return jsonify({
            "version": int(get_meta(conn, "state_version", "0")),
            "active_run_id": active_run_id(conn),
            "recomputing": is_recomputing(conn),
            "jobs": jobs,
        })

    @app.get("/export/<int:run_id>.csv")
    def export_run(run_id: int):
        conn = get_db()
        view = read_run(conn, run_id)
        if view is None:
            return "not found", 404
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["题码", "计入人数", "漏答人数", "待核人数"] + view["all_options"])
        for code, s in view["stats"].items():
            writer.writerow([code, s["counted"], s["blank"], s["flagged"]]
                            + [s["options"].get(o, 0) for o in view["all_options"]])
        resp = Response("﻿" + out.getvalue(), mimetype="text/csv; charset=utf-8")
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="item-analysis-run{run_id}.csv"'
        )
        return resp


def _back(error: str | None = None, message: str | None = None):
    target = request.referrer or url_for("index")
    params = []
    if error:
        params.append("error=" + quote(error, safe=""))
    if message:
        params.append("message=" + quote(message, safe=""))
    if params:
        target += ("&" if "?" in target else "?") + "&".join(params)
    return redirect(target, code=303)


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
            debug=False, threaded=True)
