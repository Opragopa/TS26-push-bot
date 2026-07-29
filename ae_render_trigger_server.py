#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small HTTP trigger for local AE rendering without continuous sheet polling."""

import argparse
import hmac
import ipaddress
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
MAX_REQUEST_BYTES = 64 * 1024
MAX_FIELD_CHARS = 500

_worker_lock = threading.Lock()


def is_loopback_host(host):
    text = str(host or "").strip()
    if text in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


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
    run_inline = str(os.environ.get("AE_RENDER_TRIGGER_DRAIN_QUEUE", "false")).strip().lower()
    if run_inline not in {"1", "true", "yes", "y", "on"}:
        return
    thread = threading.Thread(target=drain_queue, args=(config, retry_interval), daemon=True)
    thread.start()


def enqueue_payload(config, payload):
    if not isinstance(payload, dict):
        raise ValueError("тело запроса должно быть JSON-объектом")
    kind = str(payload.get("kind") or "plaque").strip()
    if kind != "plaque":
        raise ValueError("trigger server пока принимает только kind=plaque")
    name = str(payload.get("name") or "").strip()
    position = str(payload.get("position") or "").strip()
    if not name or not position:
        raise ValueError("нужны поля name и position")
    if len(name) > MAX_FIELD_CHARS or len(position) > MAX_FIELD_CHARS:
        raise ValueError("поля name и position ограничены {} символами".format(MAX_FIELD_CHARS))
    row = str(payload.get("sheet_row") or payload.get("row") or "").strip()
    if len(row) > 32:
        raise ValueError("поле sheet_row слишком длинное")
    source_key = str(payload.get("source_key") or "").strip()
    if len(source_key) > MAX_FIELD_CHARS:
        raise ValueError("поле source_key слишком длинное")
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
        # This endpoint is only called for a deliberate user action, so a repeat
        # request after a failed render is a retry, not a duplicate.
        dedupe_statuses=ae_render_queue.USER_RETRY_DEDUPE_STATUSES,
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
            # Only reachable when the server was explicitly started on loopback
            # without a token; main() refuses that combination on any other host.
            return True
        presented = self.headers.get("X-AE-Trigger-Token", "")
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            presented = auth[len("Bearer "):]
        # Constant-time compare so a caller cannot recover the token byte by byte.
        return hmac.compare_digest(str(presented), token)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            raise ValueError("некорректный Content-Length")
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("тело запроса больше {} байт".format(MAX_REQUEST_BYTES))
        if not length:
            return {}
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("тело запроса оборвалось")
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        if self.path.rstrip("/") in {"/health", ""}:
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
            payload = self.read_json_body()
        except (ValueError, UnicodeDecodeError) as exc:
            self.send_json(400, {"ok": False, "error": "некорректный запрос: {}".format(exc)})
            return
        try:
            project_error = ae_render_worker.expected_project_error(self.server.config)
            if project_error:
                self.send_json(409, {"ok": False, "error": project_error, "code": "wrong_project"})
                return
            job, created = enqueue_payload(self.server.config, payload)
        except ValueError as exc:
            # Validation errors are caller-facing and safe to echo back.
            self.send_json(400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - internal failure, do not leak details
            print("[trigger] внутренняя ошибка обработки /render: {!r}".format(exc), flush=True)
            self.send_json(500, {"ok": False, "error": "внутренняя ошибка сервера"})
            return
        try:
            status = queue_status(self.server.config, job.get("id"))
        except Exception as exc:  # noqa: BLE001 - status is best effort
            print("[trigger] не удалось прочитать статус очереди: {!r}".format(exc), flush=True)
            status = {}
        trigger_worker(self.server.config, self.server.retry_interval)
        response = {"ok": True, "status": "queued" if created else "existing", "job_id": job.get("id")}
        response.update(status)
        self.send_json(200, response)


def main(argv=None):
    parser = argparse.ArgumentParser(description="HTTP-trigger для локального AE render worker.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--host", default=os.environ.get("AE_RENDER_TRIGGER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AE_RENDER_TRIGGER_PORT", "8765")))
    parser.add_argument("--token", default=os.environ.get("AE_RENDER_TRIGGER_TOKEN", ""))
    parser.add_argument("--retry-interval", type=int, default=int(os.environ.get("AE_RENDER_TRIGGER_RETRY_INTERVAL", "60")))
    args = parser.parse_args(argv)

    token = str(args.token or "").strip()
    if not token and not is_loopback_host(args.host):
        raise SystemExit(
            "Отказ в запуске: сервер слушает {} без токена — любой в сети смог бы ставить задания в очередь.\n"
            "Задайте AE_RENDER_TRIGGER_TOKEN (или --token), либо оставьте --host 127.0.0.1.".format(args.host)
        )
    if not token:
        print(
            "[trigger] ВНИМАНИЕ: токен не задан. Сервер принимает любые запросы с этой машины. "
            "Задайте AE_RENDER_TRIGGER_TOKEN, чтобы включить проверку.",
            flush=True,
        )

    config = ae_render_worker.load_config(args.config)
    recovered = ae_render_queue.recover_expired_jobs(config["queue_path"])
    if recovered:
        print("[trigger] recovered expired jobs: {}".format(len(recovered)), flush=True)
    server = ThreadingHTTPServer((args.host, args.port), TriggerHandler)
    server.config = config
    server.trigger_token = token
    server.retry_interval = args.retry_interval
    print("[trigger] listening on http://{}:{}".format(args.host, args.port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
