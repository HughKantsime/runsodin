"""Shared phase exceptions; owner modules reexport these identical objects."""

class RetirementUnavailable(RuntimeError):
    def __init__(self,reason):
        self.reason=reason
        super().__init__('Retirement preparation could not be confirmed')

class EditUnavailable(ValueError):pass

class NoCompletedReceiptYet(Exception):pass
