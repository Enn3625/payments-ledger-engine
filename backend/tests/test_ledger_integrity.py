"""The core correctness story: a transaction can never leave the ledger unbalanced.

Every test here commits, because the guarantee is enforced by a deferred
constraint trigger that only runs at COMMIT.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Account,
    EntryDirection,
    LedgerEntry,
    Transaction,
    TransactionStatus,
)
from app.services.ledger import (
    EntryDraft,
    InvalidEntryError,
    UnbalancedTransactionError,
    account_balance,
    credit,
    debit,
    post_transaction,
    trial_balance,
)
from tests.conftest import make_account


def _raw_transaction(status: TransactionStatus = TransactionStatus.POSTED) -> Transaction:
    """A Transaction built directly, bypassing every service-layer check."""
    return Transaction(
        id=uuid.uuid4(),
        description="hand-written transaction",
        status=status,
        posted_at=datetime.now(UTC) if status is TransactionStatus.POSTED else None,
    )


def _entry(
    txn: Transaction, account: Account, amount: int, direction: EntryDirection
) -> LedgerEntry:
    return LedgerEntry(
        transaction_id=txn.id,
        account_id=account.id,
        amount=amount,
        direction=direction,
    )


class TestBalancedPostings:
    def test_two_legged_posting_commits(self, session: Session, cash: Account, payable: Account):
        txn = post_transaction(
            session,
            description="capture INR 1,500.00 for merchant",
            entries=[debit(cash.id, 150_000), credit(payable.id, 150_000)],
        )
        session.commit()

        stored = session.get(Transaction, txn.id)
        assert stored is not None
        assert stored.status is TransactionStatus.POSTED
        assert stored.posted_at is not None
        assert len(stored.entries) == 2
        assert trial_balance(session).is_balanced

    def test_split_posting_with_more_than_two_legs(
        self, session: Session, cash: Account, payable: Account, revenue: Account
    ):
        # INR 1,000.00 captured, INR 20.00 kept as a platform fee.
        post_transaction(
            session,
            description="capture with fee split",
            entries=[
                debit(cash.id, 100_000),
                credit(payable.id, 98_000),
                credit(revenue.id, 2_000),
            ],
        )
        session.commit()

        balance = trial_balance(session)
        assert balance.total_debits == balance.total_credits == 100_000

    def test_entries_may_be_inserted_one_statement_at_a_time(
        self, session: Session, cash: Account, payable: Account
    ):
        """The check is deferred, so a half-written transaction is fine mid-flight."""
        txn = _raw_transaction()
        session.add(txn)
        session.add(_entry(txn, cash, 5_000, EntryDirection.DEBIT))
        session.flush()  # unbalanced right now, and that is allowed

        session.add(_entry(txn, payable, 5_000, EntryDirection.CREDIT))
        session.commit()  # balanced by COMMIT time

        assert session.scalar(select(func.count()).select_from(LedgerEntry)) == 2


class TestServiceLayerRejections:
    """Fast failures, before the database is touched."""

    def test_unbalanced_entries_rejected(self, session: Session, cash: Account, payable: Account):
        with pytest.raises(UnbalancedTransactionError, match="difference=1"):
            post_transaction(
                session,
                description="off by one paisa",
                entries=[debit(cash.id, 10_001), credit(payable.id, 10_000)],
            )

    def test_single_entry_rejected(self, session: Session, cash: Account):
        with pytest.raises(InvalidEntryError, match="at least 2 entries"):
            post_transaction(session, description="one-legged", entries=[debit(cash.id, 100)])

    def test_zero_and_negative_amounts_rejected(
        self, session: Session, cash: Account, payable: Account
    ):
        for amount in (0, -100):
            with pytest.raises(InvalidEntryError, match="must be positive"):
                post_transaction(
                    session,
                    description="bad amount",
                    entries=[debit(cash.id, amount), credit(payable.id, amount)],
                )

    def test_float_amount_rejected(self, session: Session, cash: Account, payable: Account):
        """Money is integer minor units. A float amount is a bug, not a rounding hint."""
        with pytest.raises(InvalidEntryError, match="must be an int of minor units"):
            post_transaction(
                session,
                description="rupees instead of paise",
                entries=[
                    EntryDraft(cash.id, 10.5, EntryDirection.DEBIT),
                    EntryDraft(payable.id, 10.5, EntryDirection.CREDIT),
                ],
            )

    def test_posting_to_unknown_accounts_rejected(self, session: Session):
        with pytest.raises(InvalidEntryError, match="no such accounts"):
            post_transaction(
                session,
                description="ghost accounts",
                entries=[debit(uuid.uuid4(), 100), credit(uuid.uuid4(), 100)],
            )

    def test_cross_currency_posting_rejected(self, session: Session, cash: Account):
        usd_account = make_account(session, name="assets:usd_cash", currency="USD")
        with pytest.raises(InvalidEntryError, match="cross-currency"):
            post_transaction(
                session,
                description="INR against USD",
                entries=[debit(cash.id, 1_000), credit(usd_account.id, 1_000)],
            )


class TestDatabaseEnforcement:
    """The guarantee that holds even when the service layer is bypassed."""

    def test_unbalanced_entries_rejected_at_commit(
        self, session: Session, cash: Account, payable: Account
    ):
        txn = _raw_transaction()
        session.add(txn)
        session.add_all(
            [
                _entry(txn, cash, 9_999, EntryDirection.DEBIT),
                _entry(txn, payable, 1, EntryDirection.CREDIT),
            ]
        )

        with pytest.raises(DBAPIError, match="is unbalanced"):
            session.commit()

        session.rollback()
        assert session.scalar(select(func.count()).select_from(Transaction)) == 0

    def test_lone_entry_rejected_at_commit(self, session: Session, cash: Account):
        txn = _raw_transaction()
        session.add(txn)
        session.add(_entry(txn, cash, 1_000, EntryDirection.DEBIT))

        with pytest.raises(DBAPIError, match="requires at least 2"):
            session.commit()
        session.rollback()

    def test_posted_transaction_without_entries_rejected(self, session: Session):
        session.add(_raw_transaction())

        with pytest.raises(DBAPIError, match="no ledger entries"):
            session.commit()
        session.rollback()

    def test_pending_transaction_without_entries_is_allowed(self, session: Session):
        """Payment intents (step 2) exist before any money moves."""
        session.add(_raw_transaction(status=TransactionStatus.PENDING))
        session.commit()

        assert session.scalar(select(func.count()).select_from(Transaction)) == 1

    def test_negative_amount_rejected_by_check_constraint(
        self, session: Session, cash: Account, payable: Account
    ):
        txn = _raw_transaction()
        session.add(txn)
        session.add_all(
            [
                _entry(txn, cash, -500, EntryDirection.DEBIT),
                _entry(txn, payable, -500, EntryDirection.CREDIT),
            ]
        )

        with pytest.raises(IntegrityError, match="amount_positive"):
            session.commit()
        session.rollback()


class TestAppendOnly:
    """History is corrected by reversing entries, never by editing rows."""

    def test_update_is_rejected(self, session: Session, cash: Account, payable: Account):
        post_transaction(
            session,
            description="capture",
            entries=[debit(cash.id, 2_500), credit(payable.id, 2_500)],
        )
        session.commit()

        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(text("UPDATE ledger_entries SET amount = 1"))
            session.commit()
        session.rollback()

    def test_delete_is_rejected(self, session: Session, cash: Account, payable: Account):
        post_transaction(
            session,
            description="capture",
            entries=[debit(cash.id, 2_500), credit(payable.id, 2_500)],
        )
        session.commit()

        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(text("DELETE FROM ledger_entries"))
            session.commit()
        session.rollback()

    def test_reversal_is_the_supported_correction(
        self, session: Session, cash: Account, payable: Account
    ):
        post_transaction(
            session,
            description="capture booked in error",
            entries=[debit(cash.id, 7_000), credit(payable.id, 7_000)],
        )
        session.commit()

        post_transaction(
            session,
            description="reversal of erroneous capture",
            entries=[debit(payable.id, 7_000), credit(cash.id, 7_000)],
        )
        session.commit()

        assert account_balance(session, cash.id) == 0
        assert account_balance(session, payable.id) == 0
        assert session.scalar(select(func.count()).select_from(LedgerEntry)) == 4


class TestConcurrency:
    def test_parallel_postings_keep_the_ledger_balanced(
        self, engine: Engine, session: Session, cash: Account, payable: Account
    ):
        """Independent writers must not be able to interleave into an imbalance."""
        workers = 8
        postings_per_worker = 5
        amount = 1_250
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

        def worker(worker_id: int) -> int:
            written = 0
            with factory() as worker_session:
                for n in range(postings_per_worker):
                    post_transaction(
                        worker_session,
                        description=f"worker {worker_id} posting {n}",
                        entries=[debit(cash.id, amount), credit(payable.id, amount)],
                    )
                    worker_session.commit()
                    written += 1
            return written

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(worker, range(workers)))

        assert sum(results) == workers * postings_per_worker

        session.expire_all()
        expected_total = workers * postings_per_worker * amount
        balance = trial_balance(session)
        assert balance.is_balanced
        assert balance.total_debits == expected_total
        assert session.scalar(select(func.count()).select_from(Transaction)) == (
            workers * postings_per_worker
        )

    def test_a_failing_writer_does_not_corrupt_concurrent_writers(
        self, engine: Engine, session: Session, cash: Account, payable: Account
    ):
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

        def good_writer(n: int) -> str:
            with factory() as worker_session:
                post_transaction(
                    worker_session,
                    description=f"good posting {n}",
                    entries=[debit(cash.id, 500), credit(payable.id, 500)],
                )
                worker_session.commit()
            return "committed"

        def bad_writer(n: int) -> str:
            with factory() as worker_session:
                txn = _raw_transaction()
                worker_session.add(txn)
                worker_session.add_all(
                    [
                        _entry(txn, cash, 500, EntryDirection.DEBIT),
                        _entry(txn, payable, 499, EntryDirection.CREDIT),
                    ]
                )
                try:
                    worker_session.commit()
                except DBAPIError:
                    worker_session.rollback()
                    return "rejected"
            return "committed"

        jobs = [good_writer if i % 2 == 0 else bad_writer for i in range(10)]
        with ThreadPoolExecutor(max_workers=10) as pool:
            outcomes = list(pool.map(lambda pair: pair[1](pair[0]), enumerate(jobs)))

        assert outcomes.count("committed") == 5
        assert outcomes.count("rejected") == 5

        session.expire_all()
        balance = trial_balance(session)
        assert balance.is_balanced
        assert balance.total_debits == 5 * 500
