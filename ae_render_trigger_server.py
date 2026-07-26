#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small HTTP trigger for local AE rendering without continuous sheet polling."""

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ae_render_queue
import ae_render_worker


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "ae_render_config.json"

_worker_lock = threading.Lock()


def has_queued_jobs(queue_path):
    data = ae_render_queue.load_queue_unlocked(Path(queue_path).expanduser())
    return any(job.get("status") == "queued" for job in data.get("jobs", []))


def queue_status(config, job_id):
    queue_path = config["queue_path"]
    data = ae_render_queue.load_queue_unlocked(Path(queue_path).expanduser())
    active_statuses = {"queued", "preparing", "rendering"}
    counts = {}
    ahead = 0
    found = None
    for job in data.get("jobs", []):
        status = job.get("status", "")
        counts[status] = counts.get(status, 0) + 1
        if job.get("id") == job_id:
            found = job
            continue
        if found is None and status in active_statuses:
            ahead += 1
    busy = False
    try:
        busy = ae_render_worker.renderer_busy(config)
    except Exception:
        busy = bool(counts.get("rendering") or counts.get("preparing"))
    return {
        "job_status": found.get("status", "") if found else "",
        "queue_ahead": ahead if found and found.get("status") in active_statuses else 0,
        "queued_total": counts.get("queued", 0),
        "preparing_total": counts.get("preparing", 0),
        "rendering_total": counts.get("rendering", 0),
        "renderer_busy": busy,
    }


def drain_queue(config, retry_interval):
    if not _worker_lock.acquire(blocking=False):
        return
    try:
        while has_queued_jobs(config["queue_path"]):
            processed = ae_render_worker.run_once(config)
            if not processed:
                time.sleep(max(5, retry_interval))
    finally:
        _worker_lock.release()


def trigger_worker(config, retry_interval):
    thread = threading.Thread(target=drain_queue, args=(config, retry_interval), daemon=True)
    thread.start()


def enqueue_payload(config, payload):
    kind = str(payload.get("kind") or "plaque").strip()
    if kind != "plaque":
        raise ValueError("trigger server пока принимает только kind=plaque")
    name = str(payload.get("name") or "").strip()
    position = str(payload.get("position") or "").strip()
    if not name or not position:
        raise ValueError("нужны поля name и position")
    row = str(payload.get("sheet_row") or payload.get("row") or "").strip()
    source_key = str(payload.get("source_key") or "").strip()
    if not source_key:
        source_key = "trigger-plaque:{}:{}:{}".format(row or "no-row", name, position)
    job, created = ae_render_queue.enqueue(
        config["queue_path"],
        "plaque",
        {
            "name": name,
            "position": position,
            "sheet_row": row,
            "ae_id": str(payload.get("ae_id") or source_key),
        },
        source_key=source_key,
    )
    return job, created


class TriggerHandler(BaseHTTPRequestHandler):
    server_version = "TS26AERenderTrigger/1.0"

    def log_message(self, format, *args):
        print("[trigger] " + format % args, flush=True)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def token_ok(self):
        token = str(self.server.trigger_token or "").strip()
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == "Bearer " + token or self.headers.get("X-AE-Trigger-Token", "") == token

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"ok": True})
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/render":
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        if not self.token_ok():
            self.send_json(401, {"ok": False, "error": "bad token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
            job, created = enqueue_payload(self.server.config, payload)
            status = queue_status(self.server.config, job.get("id"))
            trigger_worker(self.server.config, self.server.retry_interval)
            response = {"ok": True, "status": "queued" if created else "existing", "job_id": job.get("id")}
            response.update(status)
            self.send_json(200, response)
        except Exception as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})


def main(argv=None):
    parser = argparse.ArgumentParser(description="HTTP-trigger для локального AE render worker.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--host", default=os.environ.get("AE_RENDER_TRIGGER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AE_RENDER_TRIGGER_PORT", "8765")))
    parser.add_argument("--token", default=os.environ.get("AE_RENDER_TRIGGER_TOKEN", ""))
    parser.add_argument("--retry-interval", type=int, default=int(os.environ.get("AE_RENDER_TRIGGER_RETRY_INTERVAL", "60")))
    args = parser.parse_args(argv)

    config = ae_render_worker.load_config(args.config)
    server = ThreadingHTTPServer((args.host, args.port), TriggerHandler)
    server.config = config
    server.trigger_token = args.token
    server.retry_interval = args.retry_interval
    print("[trigger] listening on http://{}:{}".format(args.host, args.port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
