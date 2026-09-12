from flask_sqlalchemy import SQLAlchemy
from datetime import date

db = SQLAlchemy()


class Account(db.Model):
    """A credit card or line of credit being tracked for payoff."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    bank = db.Column(db.String(20), nullable=False)  # bmo, scotiabank, capital_one, loc, desjardins, other
    balance = db.Column(db.Float, nullable=False, default=0.0)
    balance_date = db.Column(db.Date, nullable=True)  # opening balance is as of this date (before transactions that day)
    credit_limit = db.Column(db.Float, nullable=True)
    debt_archive_date = db.Column(db.Date, nullable=True)  # charges before this date are collapsed into one lump total instead of itemized
    archived_paid_amount = db.Column(db.Float, nullable=False, default=0.0)  # how much of the pre-archive lump total has been paid down


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Float, nullable=False)  # negative = charge, positive = payment received
    category = db.Column(db.String(100))
    type = db.Column(db.String(20))  # income, expense (payment counts as income, charge as expense)
    source = db.Column(db.String(50))  # bmo, scotiabank, capital_one, loc, desjardins, manual
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"))
    debt_paid_amount = db.Column(db.Float, nullable=True, default=0.0)
    debt_used = db.Column(db.Boolean, default=False)  # True once a payment transaction has been applied to charges


class CategoryRule(db.Model):
    """Learned merchant -> category default, set the first time a merchant is categorized
    and only overwritten via an explicit 'change default' action."""
    id = db.Column(db.Integer, primary_key=True)
    merchant_key = db.Column(db.String(255), nullable=False, unique=True)
    category = db.Column(db.String(100), nullable=False)


class DebtAllocation(db.Model):
    """Audit trail linking a debt payment to the charge(s) it paid down.
    payment_id is null when the amount was entered manually with no specific payment
    transaction behind it."""
    id = db.Column(db.Integer, primary_key=True)
    charge_id = db.Column(db.Integer, db.ForeignKey("transaction.id"), nullable=False)
    payment_id = db.Column(db.Integer, db.ForeignKey("transaction.id"), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.Date, nullable=False, default=date.today)
