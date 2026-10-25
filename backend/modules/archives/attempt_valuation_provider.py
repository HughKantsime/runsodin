"""Stateless Archives-owned private reader; no caller authority/transaction replacement."""


class AttemptValuationService:
    def read_private_valuation(self, db, request, actor, attempt_id):
        from modules.archives.valuation_private_read import read_private_valuation
        return read_private_valuation(db,request,actor,attempt_id)

    def read_order_measured_costs(self, db, request, actor, order_id):
        from modules.archives.order_measured_costs import read_order_measured_costs
        return read_order_measured_costs(db,request,actor,order_id)
