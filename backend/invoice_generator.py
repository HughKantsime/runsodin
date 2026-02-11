"""
O.D.I.N. Invoice Generator

Generates branded PDF invoices from order data using fpdf2.
Uses the branding system (app name, colors, logo) and enriched
order response data (items, P&L, customer info).
"""

from fpdf import FPDF
from datetime import datetime


def _hex_to_rgb(hex_color: str) -> tuple:
    """Convert '#RRGGBB' to (R, G, B) tuple."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (34, 197, 94)  # Default green
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _fmt(val, prefix="$", default="—"):
    """Format a currency value or return a dash."""
    if val is None:
        return default
    return f"{prefix}{val:,.2f}"


class InvoiceGenerator:
    """Generate a PDF invoice from branding + enriched order data."""

    def __init__(self, branding: dict, order: dict):
        self.branding = branding
        self.order = order
        self.primary_rgb = _hex_to_rgb(branding.get("primary_color", "#22c55e"))
        self.pdf = FPDF()
        self.pdf.set_auto_page_break(auto=True, margin=25)

    def _add_header(self):
        pdf = self.pdf
        pr, pg, pb = self.primary_rgb

        # Color bar at top
        pdf.set_fill_color(pr, pg, pb)
        pdf.rect(0, 0, 210, 8, "F")

        # App name / company
        pdf.set_y(14)
        pdf.set_font("Helvetica", "B", 20)
        pdf.set_text_color(pr, pg, pb)
        app_name = self.branding.get("app_name", "O.D.I.N.")
        pdf.cell(0, 10, app_name, new_x="LMARGIN", new_y="NEXT")

        # "INVOICE" title
        pdf.set_font("Helvetica", "B", 28)
        pdf.set_text_color(60, 60, 60)
        pdf.cell(0, 14, "INVOICE", new_x="LMARGIN", new_y="NEXT")

        # Order number and date
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(100, 100, 100)
        order_num = self.order.get("order_number") or f"#{self.order.get('id', '—')}"
        pdf.cell(0, 6, f"Invoice: {order_num}", new_x="LMARGIN", new_y="NEXT")

        order_date = self.order.get("order_date")
        if order_date:
            if isinstance(order_date, str):
                try:
                    order_date = datetime.fromisoformat(order_date.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    order_date = None
            if order_date:
                pdf.cell(0, 6, f"Date: {order_date.strftime('%B %d, %Y')}", new_x="LMARGIN", new_y="NEXT")

        generated = datetime.utcnow().strftime("%B %d, %Y")
        pdf.cell(0, 6, f"Generated: {generated}", new_x="LMARGIN", new_y="NEXT")

        platform = self.order.get("platform")
        if platform:
            pdf.cell(0, 6, f"Platform: {platform.title()}", new_x="LMARGIN", new_y="NEXT")

        pdf.ln(4)

    def _add_customer(self):
        pdf = self.pdf
        name = self.order.get("customer_name")
        email = self.order.get("customer_email")
        if not name and not email:
            return

        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(60, 60, 60)
        pdf.cell(0, 8, "Bill To:", new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(80, 80, 80)
        if name:
            pdf.cell(0, 6, name, new_x="LMARGIN", new_y="NEXT")
        if email:
            pdf.cell(0, 6, email, new_x="LMARGIN", new_y="NEXT")

        pdf.ln(6)

    def _add_line_items(self):
        pdf = self.pdf
        pr, pg, pb = self.primary_rgb
        items = self.order.get("items", [])

        # Table header
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(pr, pg, pb)
        pdf.set_text_color(255, 255, 255)

        col_widths = [70, 30, 25, 30, 35]  # Item, SKU, Qty, Unit Price, Subtotal
        headers = ["Item", "SKU", "Qty", "Unit Price", "Subtotal"]

        for i, header in enumerate(headers):
            align = "R" if i >= 2 else "L"
            pdf.cell(col_widths[i], 8, header, border=0, fill=True, align=align)
        pdf.ln()

        # Table rows
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(60, 60, 60)
        subtotal = 0.0
        fill = False

        for item in items:
            if fill:
                pdf.set_fill_color(245, 245, 245)
            else:
                pdf.set_fill_color(255, 255, 255)

            product_name = item.get("product_name") or f"Product #{item.get('product_id', '?')}"
            sku = item.get("product_sku") or "—"
            qty = item.get("quantity", 0)
            unit_price = item.get("unit_price")
            line_subtotal = item.get("subtotal")
            if line_subtotal is None and unit_price is not None:
                line_subtotal = unit_price * qty
            if line_subtotal:
                subtotal += line_subtotal

            pdf.cell(col_widths[0], 7, product_name[:35], border=0, fill=True)
            pdf.cell(col_widths[1], 7, str(sku)[:15], border=0, fill=True)
            pdf.cell(col_widths[2], 7, str(qty), border=0, fill=True, align="R")
            pdf.cell(col_widths[3], 7, _fmt(unit_price), border=0, fill=True, align="R")
            pdf.cell(col_widths[4], 7, _fmt(line_subtotal), border=0, fill=True, align="R")
            pdf.ln()
            fill = not fill

        # Separator line
        pdf.set_draw_color(200, 200, 200)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(4)

        return subtotal

    def _add_totals(self, items_subtotal: float):
        pdf = self.pdf
        pr, pg, pb = self.primary_rgb
        o = self.order

        # Right-aligned totals block
        x_label = 120
        x_val = 170
        w_label = 50
        w_val = 25

        def _row(label, value, bold=False):
            pdf.set_font("Helvetica", "B" if bold else "", 10)
            pdf.set_text_color(80, 80, 80)
            pdf.set_x(x_label)
            pdf.cell(w_label, 7, label, align="R")
            pdf.set_text_color(60, 60, 60)
            if bold:
                pdf.set_text_color(pr, pg, pb)
            pdf.cell(w_val, 7, value, align="R", new_x="LMARGIN", new_y="NEXT")

        _row("Items Subtotal:", _fmt(items_subtotal))

        # Fees
        platform_fees = o.get("platform_fees")
        if platform_fees:
            _row("Platform Fees:", f"-{_fmt(platform_fees, prefix='$')}")

        payment_fees = o.get("payment_fees")
        if payment_fees:
            _row("Payment Fees:", f"-{_fmt(payment_fees, prefix='$')}")

        shipping_charged = o.get("shipping_charged")
        if shipping_charged:
            _row("Shipping (charged):", _fmt(shipping_charged))

        shipping_cost = o.get("shipping_cost")
        if shipping_cost:
            _row("Shipping (cost):", f"-{_fmt(shipping_cost, prefix='$')}")

        # Revenue
        revenue = o.get("revenue")
        if revenue:
            pdf.ln(2)
            pdf.set_draw_color(200, 200, 200)
            pdf.line(x_label, pdf.get_y(), x_label + w_label + w_val, pdf.get_y())
            pdf.ln(2)
            _row("Total Revenue:", _fmt(revenue), bold=True)

        # Cost & Profit
        estimated_cost = o.get("estimated_cost")
        actual_cost = o.get("actual_cost")
        profit = o.get("profit")
        margin = o.get("margin_percent")

        if estimated_cost or actual_cost:
            pdf.ln(4)
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(80, 80, 80)
            pdf.set_x(x_label)
            pdf.cell(w_label + w_val, 7, "Cost Analysis", align="C", new_x="LMARGIN", new_y="NEXT")

            if estimated_cost:
                _row("Estimated Cost:", _fmt(estimated_cost))
            if actual_cost:
                _row("Actual Cost:", _fmt(actual_cost))
            if profit is not None:
                margin_str = f" ({margin}%)" if margin is not None else ""
                _row("Profit:", f"{_fmt(profit)}{margin_str}", bold=True)

    def _add_footer(self):
        pdf = self.pdf
        pr, pg, pb = self.primary_rgb
        footer_text = self.branding.get("footer_text", "")

        # Bottom bar
        pdf.set_y(-20)
        pdf.set_draw_color(pr, pg, pb)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())

        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(150, 150, 150)

        if footer_text:
            pdf.cell(0, 6, footer_text, align="C", new_x="LMARGIN", new_y="NEXT")

        app_name = self.branding.get("app_name", "O.D.I.N.")
        pdf.cell(0, 6, f"Generated by {app_name}", align="C")

    def generate(self) -> bytes:
        """Build and return the PDF as bytes."""
        self.pdf.add_page()
        self._add_header()
        self._add_customer()
        items_subtotal = self._add_line_items()
        self._add_totals(items_subtotal)
        self._add_footer()
        return self.pdf.output()
