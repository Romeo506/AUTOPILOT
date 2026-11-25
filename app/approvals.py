"""Human-approval gate. The app never sends a message, accepts a contract, makes a
commitment (price/deadline) or records spending unless the user approves the
specific request here. Executors run only from `decide(..., approve=True)`."""
from __future__ import annotations

import json
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .config import Settings
from .db import Database, loads, now

KINDS = {"send_message", "accept_contract", "commitment", "deliver", "spend_money"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_ATTACH = 20 * 1024 * 1024


class ApprovalError(ValueError):
    pass


def create(db: Database, settings: Settings, kind: str, title: str, payload: dict, project_id=None, lead_id=None) -> int:
    if kind not in KINDS:
        raise ApprovalError("Unknown approval type")
    payload = dict(payload)
    if kind in {"send_message", "deliver"}:
        channel = payload.setdefault("channel", "manual")
        if channel not in {"email", "manual"}:
            raise ApprovalError("channel must be 'email' or 'manual'")
        if channel == "email":
            if not settings.smtp_configured:
                raise ApprovalError("Email sending is not configured (set SMTP_* in .env). Use channel 'manual' instead.")
            if not EMAIL.match(str(payload.get("to", ""))):
                raise ApprovalError("A valid recipient email address is required")
    if kind == "send_message" and not str(payload.get("body", "")).strip():
        raise ApprovalError("Message body is empty")
    if kind == "deliver":
        _validate_delivery(db, payload, project_id)
    if kind in {"accept_contract", "commitment", "deliver"} and not (project_id and db.one("SELECT id FROM projects WHERE id=?", (project_id,))):
        raise ApprovalError("Project not found")
    if kind == "spend_money":
        try:
            if float(payload.get("amount", 0)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ApprovalError("Amount must be greater than zero") from None
    if kind == "commitment" and not (payload.get("price") is not None or payload.get("deadline") or payload.get("text")):
        raise ApprovalError("Nothing to commit to: give a price, deadline or text")
    aid, _ = db.run("INSERT INTO approvals(kind,title,payload_json,status,project_id,lead_id,created) VALUES(?,?,?,?,?,?,?)",
                    (kind, title[:200], json.dumps(payload), "pending", project_id, lead_id, now()))
    return aid


def _validate_delivery(db, payload, project_id):
    ids = payload.get("clip_ids") or []
    if not ids or not isinstance(ids, list):
        raise ApprovalError("Select at least one clip to deliver")
    bad = []
    for cid in ids:
        c = db.one("SELECT * FROM clips WHERE id=?", (cid,))
        if not c or c["project_id"] != project_id:
            raise ApprovalError(f"Clip {cid} is not attached to this project")
        if c["qa_status"] not in ("pass", "warn") and not payload.get("override_qa"):
            bad.append(f"{c['filename']} ({c['qa_status']})")
    if bad:
        raise ApprovalError("These clips have not passed QA: " + ", ".join(bad) + ". Fix them, re-run QA, or explicitly override.")


def edit(db: Database, approval_id: int, changes: dict):
    a = get(db, approval_id)
    if a["status"] != "pending":
        raise ApprovalError("Only pending requests can be edited")
    p = a["payload"]
    for k in ("to", "subject", "body", "message", "text"):
        if k in changes:
            p[k] = str(changes[k])
    db.update("approvals", approval_id, payload_json=json.dumps(p))


def get(db: Database, approval_id: int) -> dict:
    a = db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not a:
        raise ApprovalError("Approval not found")
    a["payload"] = loads(a.pop("payload_json"), {})
    return a


def decide(db: Database, settings: Settings, approval_id: int, approve: bool) -> dict:
    a = get(db, approval_id)
    if a["status"] not in ("pending", "failed"):
        raise ApprovalError(f"Request is already {a['status']}")
    if not approve:
        db.update("approvals", approval_id, status="rejected", decided=now(), result="Rejected by you. Nothing was done.")
        return get(db, approval_id)
    try:
        status, result = EXECUTORS[a["kind"]](db, settings, a)
    except (ApprovalError, OSError, smtplib.SMTPException) as e:
        db.update("approvals", approval_id, status="failed", decided=now(), result=f"Not executed: {e}")
        return get(db, approval_id)
    db.update("approvals", approval_id, status=status, decided=now(), result=result)
    return get(db, approval_id)


def mark_sent(db: Database, approval_id: int):
    a = get(db, approval_id)
    if a["status"] != "approved":
        raise ApprovalError("Only approved manual messages can be marked as sent")
    db.update("approvals", approval_id, status="executed", result=(a["result"] or "") + " Marked sent by you.")


# ---------------------------------------------------------------- executors
def send_email(settings: Settings, to: str, subject: str, body: str, attachments: list[Path] | None = None):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject or "(no subject)"
    msg.set_content(body)
    for p in attachments or []:
        msg.add_attachment(p.read_bytes(), maintype="video", subtype="mp4", filename=p.name)
    if settings.smtp_ssl:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ssl.create_default_context(), timeout=30)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    with server:
        if not settings.smtp_ssl:
            try:
                server.starttls(context=ssl.create_default_context())
            except smtplib.SMTPNotSupportedError:
                pass
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


def _x_send_message(db, s, a):
    p = a["payload"]
    if p.get("channel") == "email":
        send_email(s, p["to"], p.get("subject", ""), p["body"])
        return "executed", f"Email sent to {p['to']}."
    return "approved", "Approved. Copy the message, send it yourself on the platform, then press 'Mark sent'."


def _x_accept(db, s, a):
    db.update("projects", a["project_id"], status="in_progress", accepted_at=now())
    return "executed", "Contract accepted. Project is now in progress. (Accept the job on the marketplace yourself if it is hosted there.)"


def _x_commit(db, s, a):
    p, f = a["payload"], {}
    if p.get("price") is not None:
        f["price"] = float(p["price"])
    if p.get("currency"):
        f["currency"] = str(p["currency"]).upper()[:5]
    if p.get("deadline"):
        f["deadline"] = str(p["deadline"])[:10]
    if p.get("text"):
        proj = db.one("SELECT notes FROM projects WHERE id=?", (a["project_id"],))
        f["notes"] = ((proj["notes"] or "") + f"\n[{now()[:10]}] Commitment: {p['text']}").strip()
    if f.get("status") != "quoted":
        cur = db.one("SELECT status FROM projects WHERE id=?", (a["project_id"],))
        if cur and cur["status"] == "lead":
            f["status"] = "quoted"
    db.update("projects", a["project_id"], **f)
    return "executed", "Commitment recorded on the project. Communicate it to the client through an approved message."


def _x_deliver(db, s, a):
    p = a["payload"]
    _validate_delivery(db, p, a["project_id"])
    clips = [db.one("SELECT * FROM clips WHERE id=?", (i,)) for i in p["clip_ids"]]
    method = "manual"
    if p.get("channel") == "email":
        files = [Path(c["path"]) for c in clips]
        if sum(f.stat().st_size for f in files if f.exists()) > MAX_ATTACH:
            raise ApprovalError("Clips are larger than 20 MB combined; use manual delivery (share the files from your phone).")
        send_email(s, p["to"], p.get("subject", "Your clips"), p.get("message", "Your clips are attached."), files)
        method = "email"
    db.run("INSERT INTO deliveries(project_id,clip_ids,message,method,approval_id,created) VALUES(?,?,?,?,?,?)",
           (a["project_id"], json.dumps(p["clip_ids"]), p.get("message", ""), method, a["id"], now()))
    db.update("projects", a["project_id"], status="delivered", delivered_at=now())
    return "executed", ("Delivered by email." if method == "email" else "Approved. Project marked delivered - send the files to the client yourself.")


def _x_spend(db, s, a):
    p = a["payload"]
    db.run("INSERT INTO expenses(project_id,amount,currency,description,status,approval_id,created) VALUES(?,?,?,?,?,?,?)",
           (a["project_id"], float(p["amount"]), str(p.get("currency", "USD")).upper()[:5], str(p.get("description", ""))[:300], "approved", a["id"], now()))
    return "executed", "Expense recorded. The app never moves money - you pay it yourself."


EXECUTORS = {"send_message": _x_send_message, "accept_contract": _x_accept, "commitment": _x_commit,
             "deliver": _x_deliver, "spend_money": _x_spend}
