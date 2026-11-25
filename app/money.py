"""Earnings summary, CSV export, printable invoice and Stripe webhook verification."""
from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import time

from .db import Database, now

ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "UGX", "XAF", "XOF", "PYG", "RWF"}


def summary(db: Database) -> dict:
    cur: dict[str, dict] = {}

    def row(c):
        return cur.setdefault(c, {"currency": c, "paid": 0.0, "pending": 0.0, "refunded": 0.0, "expenses": 0.0, "net": 0.0})

    for r in db.all("SELECT currency, status, SUM(amount) AS t FROM payments GROUP BY currency, status"):
        row(r["currency"])[r["status"] if r["status"] in ("paid", "pending", "refunded") else "pending"] += r["t"] or 0
    for r in db.all("SELECT currency, SUM(amount) AS t FROM expenses WHERE status='approved' GROUP BY currency"):
        row(r["currency"])["expenses"] += r["t"] or 0
    for v in cur.values():
        v["net"] = round(v["paid"] - v["expenses"], 2)
        for k in ("paid", "pending", "refunded", "expenses"):
            v[k] = round(v[k], 2)
    return {"currencies": sorted(cur.values(), key=lambda x: x["currency"]),
            "note": "Figures come from records you entered or from verified Stripe webhooks. They are not bank balances."}


def payments_csv(db: Database) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "date", "project_id", "amount", "currency", "status", "source", "reference"])
    for p in db.all("SELECT * FROM payments ORDER BY id"):
        w.writerow([p["id"], p["created"], p["project_id"] or "", p["amount"], p["currency"], p["status"], p["source"], p["reference"] or ""])
    return out.getvalue()


def invoice_html(db: Database, project_id: int, business: str, instructions: str) -> str | None:
    p = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not p:
        return None
    paid = db.one("SELECT COALESCE(SUM(amount),0) AS t FROM payments WHERE project_id=? AND status='paid'", (project_id,))["t"]
    e = html.escape
    balance = max(0.0, (p["price"] or 0) - paid)
    link = f'<p><b>Pay online:</b> <a href="{e(p["payment_link"])}">{e(p["payment_link"])}</a></p>' if p["payment_link"] else ""
    ins = f"<p><b>Payment instructions:</b><br>{e(instructions).replace(chr(10), '<br>')}</p>" if instructions else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Invoice INV-{project_id:04d}</title><style>body{{font:16px system-ui,sans-serif;max-width:640px;margin:24px auto;padding:0 16px;color:#111}}
table{{width:100%;border-collapse:collapse;margin:16px 0}}td,th{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}td:last-child,th:last-child{{text-align:right}}
.tot{{font-weight:700}}@media print{{button{{display:none}}}}</style></head><body>
<h1>Invoice INV-{project_id:04d}</h1><p>{e(business)}<br>Date: {now()[:10]}</p><p><b>Bill to:</b> {e(p["client"])}{'<br>' + e(p["email"]) if p["email"] else ''}</p>
<table><tr><th>Description</th><th>Amount ({e(p["currency"])})</th></tr>
<tr><td>{e((p["brief"] or "Video clipping services")[:200])}</td><td>{p["price"]:.2f}</td></tr>
<tr><td>Payments received</td><td>-{paid:.2f}</td></tr><tr class="tot"><td>Balance due</td><td>{balance:.2f}</td></tr></table>
{link}{ins}<button onclick="print()">Print / Save as PDF</button></body></html>"""


# ---------------------------------------------------------------- Stripe
class StripeError(ValueError):
    pass


def verify_stripe(payload: bytes, header: str, secret: str, tolerance: int = 300, now_ts: float | None = None) -> dict:
    if not secret:
        raise StripeError("Stripe webhook secret is not configured")
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    ts = parts.get("t")
    sigs = [v for k, v in (p.split("=", 1) for p in (header or "").split(",") if "=" in p) if k == "v1"]
    if not ts or not sigs:
        raise StripeError("Malformed Stripe-Signature header")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise StripeError("Signature mismatch")
    if abs((now_ts or time.time()) - int(ts)) > tolerance:
        raise StripeError("Timestamp outside tolerance (possible replay)")
    return json.loads(payload)


def handle_stripe_event(db: Database, event: dict) -> dict:
    t, obj = event.get("type"), (event.get("data") or {}).get("object") or {}
    if t not in {"checkout.session.completed", "checkout.session.async_payment_succeeded", "payment_intent.succeeded"}:
        return {"status": "ignored", "type": t}
    if t.startswith("checkout") and obj.get("payment_status") != "paid":
        return {"status": "ignored", "reason": "not paid yet"}
    cur = str(obj.get("currency", "usd")).upper()
    raw = obj.get("amount_total", obj.get("amount_received", obj.get("amount", 0))) or 0
    amount = raw if cur in ZERO_DECIMAL else raw / 100
    meta = obj.get("metadata") or {}
    pid = meta.get("project_id") or obj.get("client_reference_id")
    pid = int(pid) if str(pid or "").isdigit() and db.one("SELECT id FROM projects WHERE id=?", (int(pid),)) else None
    if amount <= 0:
        return {"status": "ignored", "reason": "zero amount"}
    try:
        db.run("INSERT INTO payments(project_id,amount,currency,status,reference,created,source,external_id) VALUES(?,?,?,?,?,?,?,?)",
               (pid, amount, cur, "paid", obj.get("id", ""), now(), "stripe", event.get("id")))
    except Exception as e:  # unique index -> already processed
        if "UNIQUE" in str(e).upper():
            return {"status": "duplicate"}
        raise
    return {"status": "recorded", "project_id": pid, "amount": amount, "currency": cur}
