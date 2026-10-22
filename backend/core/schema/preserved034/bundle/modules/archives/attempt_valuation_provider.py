"""Stateless Archives-owned private reader; no caller authority/transaction replacement."""


class AttemptValuationService:
    def read_private_valuation(self, db, request, actor, attempt_id):
        from modules.archives.valuation_private_read import read_private_valuation
        return read_private_valuation(db,request,actor,attempt_id)
