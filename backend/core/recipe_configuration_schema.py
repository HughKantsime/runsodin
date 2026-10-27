"""Current receipt ORM additions; the migration012 source stays byte-frozen."""
from sqlalchemy import Column, String
from core.dispatch_receipt_schema import DispatchAttemptReceipt

# Append in ALTER TABLE order, after every historical receipt column.
DispatchAttemptReceipt.stable_configuration_key = Column(String(64), nullable=True)
DispatchAttemptReceipt.stable_key_source = Column(String(32), nullable=True)
