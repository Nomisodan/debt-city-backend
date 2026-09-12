from flask import Flask, jsonify, request
from flask_cors import CORS
from models import db, Account, Transaction, CategoryRule, DebtAllocation
from parsers import parse_csv, normalize_merchant_key
from datetime import date
import os

app = Flask(__name__)
CORS(app, origins=os.environ.get("FRONTEND_URL", "*"))

basedir = os.path.abspath(os.path.dirname(__file__))
database_url = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(basedir, 'debt.db')}")
# Railway supplies postgres:// but SQLAlchemy requires postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)


def _learned_category(description):
    rule = CategoryRule.query.filter_by(merchant_key=normalize_merchant_key(description)).first()
    return rule.category if rule else None


def _apply_category_edit(t, new_category, force_default=False):
    """Set a transaction's category and update the learned merchant rule.
    - force_default=True: always overwrite the rule.
    - force_default=False: only create the rule if none exists yet."""
    t.category = new_category
    if not new_category:
        return
    key = normalize_merchant_key(t.description)
    rule = CategoryRule.query.filter_by(merchant_key=key).first()
    if rule:
        if force_default:
            rule.category = new_category
    else:
        db.session.add(CategoryRule(merchant_key=key, category=new_category))


def _parse_date_field(value):
    if not value:
        return None
    from dateutil import parser as dp
    return dp.parse(str(value)).date()


def _balance_history(account, as_of_date):
    """The anchor balance plus every transaction between the anchor and as_of_date, so the
    walk-forward (or back) to as_of_date can be both computed and shown itemized.
    Returns (anchor_balance, anchor_date, txns, sign) — balance_as_of = anchor_balance + sign * sum(txns)."""
    anchor_balance = account.balance or 0.0
    anchor_date = account.balance_date if account.balance_date else as_of_date

    if anchor_date <= as_of_date:
        txns = Transaction.query.filter(
            Transaction.account_id == account.id,
            Transaction.date >= anchor_date,
            Transaction.date < as_of_date,
        ).order_by(Transaction.date).all()
        return anchor_balance, anchor_date, txns, 1

    txns = Transaction.query.filter(
        Transaction.account_id == account.id,
        Transaction.date >= as_of_date,
        Transaction.date < anchor_date,
    ).order_by(Transaction.date).all()
    return anchor_balance, anchor_date, txns, -1


def _balance_as_of(account, as_of_date):
    """What the account's anchor balance implies was owed at the start of as_of_date."""
    anchor_balance, _anchor_date, txns, sign = _balance_history(account, as_of_date)
    return anchor_balance + sign * sum(t.amount for t in txns)


def _account_dict(a):
    return {
        "id": a.id, "name": a.name, "bank": a.bank,
        "balance": a.balance,
        "balance_date": a.balance_date.isoformat() if a.balance_date else None,
        "credit_limit": a.credit_limit,
        "debt_archive_date": a.debt_archive_date.isoformat() if a.debt_archive_date else None,
    }


def _txn_dict(t):
    return {
        "id": t.id,
        "date": t.date.isoformat(),
        "description": t.description,
        "amount": t.amount,
        "category": t.category,
        "type": t.type,
        "source": t.source,
        "account_id": t.account_id,
    }


@app.route("/api/accounts")
def accounts():
    accts = Account.query.order_by(Account.name).all()
    return jsonify([_account_dict(a) for a in accts])


@app.route("/api/accounts", methods=["POST"])
def create_account():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    bank = (body.get("bank") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not bank:
        return jsonify({"error": "bank is required"}), 400
    balance = float(body.get("balance") or 0)
    balance_date = _parse_date_field(body.get("balance_date"))
    credit_limit_raw = body.get("credit_limit")
    credit_limit = float(credit_limit_raw) if credit_limit_raw not in (None, "") else None
    a = Account(name=name, bank=bank, balance=balance, balance_date=balance_date, credit_limit=credit_limit)
    db.session.add(a)
    db.session.commit()
    return jsonify(_account_dict(a)), 201


@app.route("/api/accounts/<int:account_id>", methods=["PATCH"])
def update_account(account_id):
    account = Account.query.get(account_id)
    if not account:
        return jsonify({"error": "Account not found"}), 404

    body = request.get_json(silent=True) or {}
    if "balance" in body:
        account.balance = float(body["balance"])
    if "name" in body:
        account.name = str(body["name"]).strip()
    if "balance_date" in body:
        account.balance_date = _parse_date_field(body["balance_date"])
    if "credit_limit" in body:
        val = body["credit_limit"]
        account.credit_limit = float(val) if val not in (None, "") else None
    if "debt_archive_date" in body:
        new_date = _parse_date_field(body["debt_archive_date"])
        if new_date != account.debt_archive_date:
            account.archived_paid_amount = 0.0
        account.debt_archive_date = new_date

    db.session.commit()
    return jsonify(_account_dict(account))


@app.route("/api/accounts/<int:account_id>", methods=["DELETE"])
def delete_account(account_id):
    a = Account.query.get(account_id)
    if not a:
        return jsonify({"error": "Not found"}), 404
    Transaction.query.filter_by(account_id=account_id).delete()
    db.session.delete(a)
    db.session.commit()
    return jsonify({"deleted": account_id})


@app.route("/api/upload", methods=["POST"])
def upload_csv():
    account_id = request.form.get("account_id", type=int)
    file = request.files.get("file")

    if not account_id:
        return jsonify({"error": "account_id is required"}), 400
    if not file:
        return jsonify({"error": "file is required"}), 400

    account = Account.query.get(account_id)
    if not account:
        return jsonify({"error": f"Account {account_id} not found"}), 404

    try:
        import csv as _csv, io as _io
        raw = file.stream.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        detected_headers = list(_csv.DictReader(_io.StringIO(text)).fieldnames or [])
        rows = parse_csv(account.bank, text)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Parse error: {e}"}), 422

    learned_rules = {r.merchant_key: r.category for r in CategoryRule.query.all()}

    inserted = 0
    skipped = 0
    for row in rows:
        exists = Transaction.query.filter_by(
            date=row["date"],
            description=row["description"],
            amount=row["amount"],
            account_id=account_id,
        ).first()
        if exists:
            skipped += 1
            continue

        category = learned_rules.get(normalize_merchant_key(row["description"]), row["category"])

        db.session.add(Transaction(
            date=row["date"],
            description=row["description"],
            amount=row["amount"],
            category=category,
            type=row["type"],
            source=account.bank,
            account_id=account_id,
        ))
        inserted += 1

    db.session.commit()
    return jsonify({"inserted": inserted, "skipped": skipped, "total": len(rows),
                    "detected_headers": detected_headers, "bank": account.bank})


@app.route("/api/upload/preview", methods=["POST"])
def preview_csv():
    """Return the raw headers and first 5 rows of a CSV without importing anything."""
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "file is required"}), 400
    try:
        import csv, io
        text = file.stream.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        rows = []
        for i, row in enumerate(reader):
            if i >= 5:
                break
            rows.append(dict(row))
        return jsonify({"headers": headers, "sample_rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 422


@app.route("/api/transactions/<int:txn_id>", methods=["GET"])
def get_transaction(txn_id):
    t = Transaction.query.get(txn_id)
    if not t:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_txn_dict(t))


@app.route("/api/transactions/<int:txn_id>", methods=["PATCH"])
def update_transaction(txn_id):
    t = Transaction.query.get(txn_id)
    if not t:
        return jsonify({"error": "Not found"}), 404
    body = request.get_json(silent=True) or {}
    if "category" in body:
        _apply_category_edit(t, body["category"], force_default=bool(body.get("update_default")))
    if "description" in body and body["description"]:
        t.description = body["description"].strip()
    if "amount" in body:
        t.amount = round(float(body["amount"]), 2)
    db.session.commit()
    return jsonify(_txn_dict(t))


@app.route("/api/transactions/<int:txn_id>", methods=["DELETE"])
def delete_transaction(txn_id):
    t = Transaction.query.get(txn_id)
    if not t:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(t)
    db.session.commit()
    return jsonify({"deleted": txn_id})


@app.route("/api/transactions/range", methods=["GET", "DELETE"])
def transaction_range():
    """Preview or delete transactions by account + date range."""
    account_id = request.args.get("account_id", type=int)
    date_from  = request.args.get("date_from")
    date_to    = request.args.get("date_to")
    if not account_id or not date_from or not date_to:
        return jsonify({"error": "account_id, date_from, date_to required"}), 400
    try:
        from_d = date.fromisoformat(date_from)
        to_d   = date.fromisoformat(date_to)
    except ValueError:
        return jsonify({"error": "Invalid date format (use YYYY-MM-DD)"}), 400

    q = Transaction.query.filter(
        Transaction.account_id == account_id,
        Transaction.date >= from_d,
        Transaction.date <= to_d,
    )

    if request.method == "DELETE":
        deleted = q.delete(synchronize_session=False)
        db.session.commit()
        return jsonify({"deleted": deleted})

    return jsonify({"count": q.count()})


@app.route("/api/debt")
def debt_view():
    credit_accounts = Account.query.order_by(Account.name).all()
    # Payments referenced by a DebtAllocation are already visible via `links` below —
    # anything dismissed without a charge link has no allocation row at all.
    allocated_payment_ids = {row[0] for row in db.session.query(DebtAllocation.payment_id).filter(
        DebtAllocation.payment_id.isnot(None)
    ).all()}
    result = []
    for acct in credit_accounts:
        archive_date = acct.debt_archive_date

        # Charges (negative = money owed)
        charge_q = Transaction.query.filter(
            Transaction.account_id == acct.id,
            Transaction.amount < 0,
        )
        if archive_date:
            charge_q = charge_q.filter(Transaction.date >= archive_date)
        charge_txns = charge_q.order_by(Transaction.date.desc()).all()

        items = []
        for t in charge_txns:
            paid = t.debt_paid_amount or 0.0
            owed = abs(t.amount)
            items.append({
                'id': t.id,
                'date': t.date.isoformat(),
                'description': t.description,
                'amount': round(owed, 2),
                'paid_amount': round(paid, 2),
                'remaining': round(owed - paid, 2),
                'category': t.category,
                'is_paid': paid >= owed - 0.005,
            })

        # Payments received (positive = credit card payment coming in, not yet applied)
        payment_q = Transaction.query.filter(
            Transaction.account_id == acct.id,
            Transaction.amount > 0,
            (Transaction.debt_used == False) | (Transaction.debt_used == None),
        )
        if archive_date:
            payment_q = payment_q.filter(Transaction.date >= archive_date)
        payment_txns = payment_q.order_by(Transaction.date.desc()).all()

        payments = [{
            'id': t.id,
            'date': t.date.isoformat(),
            'description': t.description,
            'amount': round(t.amount, 2),
        } for t in payment_txns]

        # Payments marked "already counted, dismiss" — debt_used but never allocated to a
        # charge, so they'd otherwise vanish with no trace. Surfaced next to `links` so they
        # stay visible and can be restored.
        dismissed_q = Transaction.query.filter(
            Transaction.account_id == acct.id,
            Transaction.amount > 0,
            Transaction.debt_used == True,
        )
        if archive_date:
            dismissed_q = dismissed_q.filter(Transaction.date >= archive_date)
        dismissed_payments = [{
            'id': t.id,
            'date': t.date.isoformat(),
            'description': t.description,
            'amount': round(t.amount, 2),
        } for t in dismissed_q.order_by(Transaction.date.desc()).all() if t.id not in allocated_payment_ids]

        # Full payment<->charge audit trail for this account, regardless of archive cutoff —
        # lets the user see and correct every link, not just what's currently visible above.
        all_charge_ids = [row[0] for row in db.session.query(Transaction.id).filter(
            Transaction.account_id == acct.id, Transaction.amount < 0
        ).all()]
        link_rows = DebtAllocation.query.filter(
            DebtAllocation.charge_id.in_(all_charge_ids)
        ).order_by(DebtAllocation.id.desc()).all() if all_charge_ids else []

        linked_charge_ids = {l.charge_id for l in link_rows}
        linked_payment_ids = {l.payment_id for l in link_rows if l.payment_id}
        txn_lookup = {t.id: t for t in Transaction.query.filter(
            Transaction.id.in_(linked_charge_ids | linked_payment_ids)
        ).all()} if link_rows else {}

        def _link_txn(t):
            if not t:
                return None
            return {'id': t.id, 'date': t.date.isoformat(), 'description': t.description, 'amount': round(abs(t.amount), 2)}

        links = [{
            'id': l.id,
            'amount': round(l.amount, 2),
            'created_at': l.created_at.isoformat() if l.created_at else None,
            'charge': _link_txn(txn_lookup.get(l.charge_id)),
            'payment': _link_txn(txn_lookup.get(l.payment_id)) if l.payment_id else None,
        } for l in link_rows]

        archived_debt = None
        archived_paid = 0.0
        archived_remaining = None
        since_archive_debt = None
        total_debt = None
        archived_item = None
        if archive_date:
            # What was owed at the start of the archive date, per the account's balance anchor
            # plus every transaction between the anchor and the cutoff — kept itemized so the
            # total is auditable, not just a number, even though it's paid as one lump.
            anchor_balance, anchor_date, hist_txns, hist_sign = _balance_history(acct, archive_date)
            archived_debt = round(-(anchor_balance + hist_sign * sum(t.amount for t in hist_txns)), 2)
            archived_paid = round(acct.archived_paid_amount or 0.0, 2)
            archived_remaining = round(archived_debt - archived_paid, 2)
            since_archive_net = db.session.query(db.func.sum(Transaction.amount)).filter(
                Transaction.account_id == acct.id,
                Transaction.date >= archive_date,
            ).scalar() or 0.0
            since_archive_debt = round(-since_archive_net, 2)
            total_debt = round(archived_remaining + since_archive_debt, 2)

            # Exposed as a single pseudo-charge so a payment can be applied against the
            # collapsed pre-archive lump, the same way it's applied to an itemized charge.
            if archived_remaining > 0.005:
                archived_item = {
                    'id': f'archived-{acct.id}',
                    'description': 'Outstanding balance',
                    'amount': archived_debt,
                    'paid_amount': archived_paid,
                    'remaining': archived_remaining,
                    'is_paid': False,
                    'anchor_balance': round(anchor_balance, 2),
                    'anchor_date': anchor_date.isoformat(),
                    'history': [{
                        'id': t.id,
                        'date': t.date.isoformat(),
                        'description': t.description,
                        'amount': round(t.amount, 2),
                    } for t in hist_txns],
                }

        result.append({
            'id': acct.id,
            'name': acct.name,
            'bank': acct.bank,
            'balance': acct.balance,
            'balance_date': acct.balance_date.isoformat() if acct.balance_date else None,
            'credit_limit': acct.credit_limit,
            'debt_archive_date': archive_date.isoformat() if archive_date else None,
            'archived_debt': archived_debt,
            'archived_paid_amount': archived_paid,
            'archived_remaining': archived_remaining,
            'since_archive_debt': since_archive_debt,
            'total_debt': total_debt,
            'archived_item': archived_item,
            'items': items,
            'payments': payments,
            'dismissed_payments': dismissed_payments,
            'links': links,
        })
    return jsonify(result)


@app.route("/api/debt/payment", methods=["POST"])
def apply_debt_payment():
    body = request.get_json(silent=True) or {}
    allocations = body.get("allocations", [])
    payment_ids = body.get("payment_ids")
    if payment_ids is None:
        payment_ids = [body["payment_id"]] if body.get("payment_id") else []
    if not allocations and not payment_ids:
        return jsonify({"error": "allocations or payment_ids required"}), 400

    # Payments drain in the order given — a charge can be funded by more than one payment,
    # and a payment can fund more than one charge. Each contribution becomes its own
    # DebtAllocation row so the pairing stays visible and editable afterwards.
    pool = []
    for pid in payment_ids:
        pt = Transaction.query.get(pid)
        if pt:
            pool.append({"txn": pt, "remaining": round(pt.amount, 2)})
    pool_idx = 0

    updated = []
    for alloc in allocations:
        alloc_id = alloc.get("id")

        # The collapsed pre-archive lump total is a pseudo-charge (id "archived-<account_id>"),
        # not a real transaction, so it's tracked on the account instead of via DebtAllocation.
        if isinstance(alloc_id, str) and alloc_id.startswith("archived-"):
            account = Account.query.get(alloc_id.split("-", 1)[1])
            if not account or not account.debt_archive_date:
                continue
            archived_debt = round(-_balance_as_of(account, account.debt_archive_date), 2)
            current_paid = account.archived_paid_amount or 0.0
            amount_to_apply = round(min(float(alloc.get("amount", 0)), archived_debt - current_paid), 2)
            if amount_to_apply <= 0:
                continue
            account.archived_paid_amount = round(current_paid + amount_to_apply, 2)

            remaining_to_fund = amount_to_apply
            while remaining_to_fund > 0.005 and pool_idx < len(pool):
                entry = pool[pool_idx]
                take = round(min(entry["remaining"], remaining_to_fund), 2)
                if take > 0:
                    entry["remaining"] = round(entry["remaining"] - take, 2)
                    remaining_to_fund = round(remaining_to_fund - take, 2)
                if entry["remaining"] <= 0.005:
                    pool_idx += 1

            updated.append({
                'id': alloc_id,
                'paid_amount': account.archived_paid_amount,
                'remaining': round(archived_debt - account.archived_paid_amount, 2),
                'is_paid': account.archived_paid_amount >= archived_debt - 0.005,
            })
            continue

        t = Transaction.query.get(alloc_id)
        if not t:
            continue
        current_paid = t.debt_paid_amount or 0.0
        amount_to_apply = round(min(float(alloc.get("amount", 0)), abs(t.amount) - current_paid), 2)
        if amount_to_apply <= 0:
            continue
        t.debt_paid_amount = round(current_paid + amount_to_apply, 2)

        remaining_to_fund = amount_to_apply
        while remaining_to_fund > 0.005 and pool_idx < len(pool):
            entry = pool[pool_idx]
            take = round(min(entry["remaining"], remaining_to_fund), 2)
            if take > 0:
                db.session.add(DebtAllocation(charge_id=t.id, payment_id=entry["txn"].id, amount=take))
                entry["remaining"] = round(entry["remaining"] - take, 2)
                remaining_to_fund = round(remaining_to_fund - take, 2)
            if entry["remaining"] <= 0.005:
                pool_idx += 1
        if remaining_to_fund > 0.005:
            # Amount exceeds the selected payments (e.g. a manually-typed total) — record
            # it as an unlinked contribution rather than silently dropping the difference.
            db.session.add(DebtAllocation(charge_id=t.id, payment_id=None, amount=remaining_to_fund))

        updated.append({
            'id': t.id,
            'paid_amount': t.debt_paid_amount,
            'remaining': round(abs(t.amount) - t.debt_paid_amount, 2),
            'is_paid': t.debt_paid_amount >= abs(t.amount) - 0.005,
        })

    for pid in payment_ids:
        pt = Transaction.query.get(pid)
        if pt:
            pt.debt_used = True

    db.session.commit()
    return jsonify({"updated": updated})


@app.route("/api/debt/links/<int:link_id>", methods=["PATCH"])
def update_debt_link(link_id):
    """Correct the amount recorded for a payment<->charge link."""
    link = DebtAllocation.query.get(link_id)
    if not link:
        return jsonify({"error": "Not found"}), 404

    body = request.get_json(silent=True) or {}
    if body.get("amount") is None:
        return jsonify({"error": "amount is required"}), 400
    new_amount = round(float(body["amount"]), 2)
    if new_amount <= 0:
        return jsonify({"error": "amount must be greater than zero"}), 400

    delta = round(new_amount - link.amount, 2)
    charge = Transaction.query.get(link.charge_id)
    if charge:
        new_charge_paid = round((charge.debt_paid_amount or 0.0) + delta, 2)
        if new_charge_paid > abs(charge.amount) + 0.005:
            return jsonify({"error": "Amount exceeds the charge total"}), 400
        charge.debt_paid_amount = max(0.0, new_charge_paid)

    link.amount = new_amount
    db.session.commit()
    return jsonify({"id": link.id, "amount": link.amount})


@app.route("/api/debt/links/<int:link_id>", methods=["DELETE"])
def delete_debt_link(link_id):
    """Unlink a payment from a charge — reverses the charge's paid amount, and if the
    payment now has no other links, it reappears as unapplied."""
    link = DebtAllocation.query.get(link_id)
    if not link:
        return jsonify({"error": "Not found"}), 404

    charge = Transaction.query.get(link.charge_id)
    if charge:
        charge.debt_paid_amount = max(0.0, round((charge.debt_paid_amount or 0.0) - link.amount, 2))

    payment_id = link.payment_id
    db.session.delete(link)
    db.session.flush()

    if payment_id:
        remaining_links = DebtAllocation.query.filter_by(payment_id=payment_id).count()
        if remaining_links == 0:
            payment = Transaction.query.get(payment_id)
            if payment:
                payment.debt_used = False

    db.session.commit()
    return jsonify({"deleted": link_id})


@app.route("/api/debt/payments/<int:payment_id>/undismiss", methods=["POST"])
def undismiss_debt_payment(payment_id):
    """Reverse 'already counted, dismiss' — the payment reappears as unapplied."""
    payment = Transaction.query.get(payment_id)
    if not payment:
        return jsonify({"error": "Not found"}), 404
    payment.debt_used = False
    db.session.commit()
    return jsonify({"id": payment.id, "debt_used": payment.debt_used})


@app.route("/api/debt/reset", methods=["POST"])
def reset_debt_payment():
    """Reset debt tracking fields and remove payment<->charge links. Pass account_id,
    transaction_ids, or neither to reset all accounts."""
    body = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    txn_ids    = body.get("transaction_ids", [])
    reset_vals = {"debt_paid_amount": 0.0, "debt_used": False}

    if txn_ids:
        scope_ids = list(txn_ids)
        archived_scope_accounts = []
    elif account_id:
        scope_ids = [t.id for t in Transaction.query.filter(Transaction.account_id == account_id).all()]
        archived_scope_accounts = [account_id]
    else:
        all_ids = [a.id for a in Account.query.all()]
        scope_ids = [t.id for t in Transaction.query.filter(Transaction.account_id.in_(all_ids)).all()] if all_ids else []
        archived_scope_accounts = all_ids

    if archived_scope_accounts:
        Account.query.filter(Account.id.in_(archived_scope_accounts)).update(
            {"archived_paid_amount": 0.0}, synchronize_session=False
        )

    if scope_ids:
        Transaction.query.filter(Transaction.id.in_(scope_ids)).update(reset_vals, synchronize_session=False)
        DebtAllocation.query.filter(
            (DebtAllocation.charge_id.in_(scope_ids)) | (DebtAllocation.payment_id.in_(scope_ids))
        ).delete(synchronize_session=False)

    db.session.commit()
    return jsonify({"ok": True})


def _self_fk_columns(table):
    """Column names on `table` whose foreign key points back at `table` itself —
    these need a second insert pass on restore since the row they point to may not exist yet."""
    return {fk.parent.name for fk in table.foreign_keys if fk.column.table is table}


@app.route("/api/backup", methods=["GET"])
def backup_data():
    """Downloads every table in the database as JSON, in restorable form."""
    from datetime import date, datetime

    def serialize(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

    tables = {}
    for table in db.metadata.sorted_tables:
        rows = db.session.execute(table.select()).mappings().all()
        tables[table.name] = [{k: serialize(v) for k, v in row.items()} for row in rows]

    payload = {"version": 1, "exported_at": datetime.utcnow().isoformat(), "tables": tables}
    resp = jsonify(payload)
    resp.headers["Content-Disposition"] = f'attachment; filename="debt-city-backup-{date.today().isoformat()}.json"'
    return resp


@app.route("/api/restore", methods=["POST"])
def restore_data():
    """Wipes the entire database and reloads it from a backup produced by /api/backup.
    Requires an explicit confirm flag so it can't be triggered by an accidental request."""
    from sqlalchemy import text

    body = request.get_json(silent=True) or {}
    if not body.get("confirm"):
        return jsonify({"error": "confirm is required"}), 400

    tables_data = (body.get("backup") or {}).get("tables")
    if not isinstance(tables_data, dict):
        return jsonify({"error": "invalid backup file"}), 400

    with db.engine.begin() as conn:
        for table in reversed(db.metadata.sorted_tables):
            conn.execute(table.delete())

        for table in db.metadata.sorted_tables:
            rows = tables_data.get(table.name) or []
            if not rows:
                continue
            skip_cols = _self_fk_columns(table)
            insert_cols = [c.name for c in table.columns if c.name not in skip_cols]
            col_list = ", ".join(f'"{c}"' for c in insert_cols)
            placeholders = ", ".join(f":{c}" for c in insert_cols)
            conn.execute(
                text(f'INSERT INTO "{table.name}" ({col_list}) VALUES ({placeholders})'),
                [{c: row.get(c) for c in insert_cols} for row in rows],
            )

        # second pass: now that every row exists, fill in self-referential FKs
        for table in db.metadata.sorted_tables:
            self_fk_cols = _self_fk_columns(table)
            if not self_fk_cols:
                continue
            rows = tables_data.get(table.name) or []
            pk_col = table.primary_key.columns.values()[0].name
            for col in self_fk_cols:
                updates = [{"pk": row[pk_col], "val": row[col]} for row in rows if row.get(col) is not None]
                if updates:
                    conn.execute(text(f'UPDATE "{table.name}" SET "{col}" = :val WHERE "{pk_col}" = :pk'), updates)

    return jsonify({"ok": True})


# Runs on import — both `python app.py` (dev) and `gunicorn app:app` (Railway/prod) need the
# schema created before the first request comes in.
with app.app_context():
    db.create_all()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=os.environ.get("FLASK_ENV") != "production", host="0.0.0.0", port=port)
