"""Customer-facing invoices with local fonts and no internal cost disclosure."""
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace

_FONT_DIR = Path(__file__).with_name('fonts')
_BRANDING_DIR = Path(__file__).resolve().parents[2] / 'static' / 'branding'


class InvoiceUnsupportedText(ValueError):
    """Text contains a glyph outside the packaged fonts' verified coverage."""


class InvoiceInvalidAmount(ValueError):
    """Stored billing amounts cannot be rendered safely."""


class InvoiceFooterTooLarge(ValueError):
    """Branding footer does not fit the reserved printable region."""


def _hex_to_rgb(value):
    try:
        value = value.lstrip('#')
        if len(value) == 6:
            return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except (AttributeError, ValueError):
        pass
    return (34, 197, 94)


def _fmt(value):
    if value is None:
        return 'Not recorded'
    if not isfinite(value):
        raise InvoiceInvalidAmount('Invalid stored billing amount')
    return f'${value:,.2f}'


class _InvoicePDF(FPDF):
    def __init__(self, branding, color):
        super().__init__()
        self.branding = branding
        self.color = color
        for family, regular, bold in (
            ('Invoice', 'NotoSans-Regular.ttf', 'NotoSans-Bold.ttf'),
            ('InvoiceCJK', 'NotoSansSC-Regular.ttf', 'NotoSansSC-Bold.ttf'),
        ):
            self.add_font(family, '', _FONT_DIR / regular)
            self.add_font(family, 'B', _FONT_DIR / bold)
        self.set_fallback_fonts(['InvoiceCJK'])
        self._supported_glyphs = {
            style: frozenset(self.fonts['invoice'+style].cmap) | frozenset(self.fonts['invoicecjk'+style].cmap)
            for style in ('', 'B')
        }
        self.alias_nb_pages()
        self.set_auto_page_break(auto=True, margin=30)

    def normalize_text(self, value):
        # Controls must not inject layout; preserve intentional line breaks.
        value = ''.join(c if c == '\n' or ord(c) >= 32 else ' ' for c in str(value))
        if self.current_font and hasattr(self.current_font, 'cmap'):
            supported = self._supported_glyphs[self.font_style.replace('U', '')]
            if any(ord(c) not in supported for c in value if c != '\n'):
                raise InvoiceUnsupportedText('Invoice text uses unsupported characters')
        return super().normalize_text(value)

    def footer(self):
        self.set_font('Invoice', '', 8)
        footer = self.branding.get('footer_text') or ''
        if footer and self.multi_cell(0, 4, footer, dry_run=True, output='HEIGHT') > 12:
            raise InvoiceFooterTooLarge('Invoice branding footer exceeds printable space')
        self.set_y(-25)
        self.set_draw_color(*self.color)
        self.line(self.l_margin, self.y, self.w-self.r_margin, self.y)
        self.set_font('Invoice', '', 8)
        self.set_text_color(100, 100, 100)
        if footer:
            self.multi_cell(0, 4, footer, align='C', new_x='LMARGIN', new_y='NEXT')
        self.cell(0, 5, f'Page {self.page_no()}/{{nb}}', align='C')


class InvoiceGenerator:
    def __init__(self, branding, order):
        self.branding = branding
        self.order = order
        self.primary_rgb = _hex_to_rgb(branding.get('primary_color'))
        self.pdf = _InvoicePDF(branding, self.primary_rgb)

    def _add_header(self):
        pdf = self.pdf
        pdf.set_fill_color(*self.primary_rgb)
        pdf.rect(0, 0, pdf.w, 8, 'F')
        pdf.set_y(14)
        # Only uploaded local raster assets are eligible; never fetch a logo URL.
        logo = self.branding.get('logo_url')
        if isinstance(logo, str) and __import__('re').fullmatch(r'/static/branding/logo(?:-[0-9a-f]{32})?\.(?:png|jpg|webp)', logo):
            path = (_BRANDING_DIR / logo.rsplit('/', 1)[1]).resolve()
            if path.is_relative_to(_BRANDING_DIR.resolve()) and path.is_file() and path.stat().st_size <= 5*1024*1024:
                from PIL import Image
                with Image.open(path) as image:
                    if image.format in ('PNG', 'JPEG', 'WEBP') and 0 < image.width*image.height <= 4_000_000:
                        image.verify()
                        pdf.image(path, x=pdf.w-45, y=14, w=30, h=15, keep_aspect_ratio=True)
        pdf.set_font('Invoice', 'B', 20)
        pdf.set_text_color(*self.primary_rgb)
        pdf.multi_cell(pdf.epw-35, 10, self.branding.get('app_name') or 'O.D.I.N.', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Invoice', 'B', 26)
        pdf.set_text_color(60, 60, 60)
        pdf.cell(0, 14, 'INVOICE', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Invoice', '', 10)
        number = self.order.get('order_number') or f"#{self.order.get('id', '')}"
        pdf.multi_cell(0, 6, f'Invoice: {number}', new_x='LMARGIN', new_y='NEXT')
        date = self.order.get('order_date')
        if isinstance(date, str):
            try:
                date = datetime.fromisoformat(date.replace('Z', '+00:00'))
            except ValueError:
                date = None
        if date:
            pdf.cell(0, 6, f"Date: {date.strftime('%B %d, %Y')}", new_x='LMARGIN', new_y='NEXT')
        pdf.cell(0, 6, f"Generated: {datetime.now(timezone.utc).strftime('%B %d, %Y')}", new_x='LMARGIN', new_y='NEXT')
        pdf.ln(4)

    def _add_customer(self):
        name = self.order.get('customer_name')
        email = self.order.get('customer_email')
        if name or email:
            pdf = self.pdf
            pdf.set_font('Invoice', 'B', 11)
            pdf.cell(0, 8, 'Bill To:', new_x='LMARGIN', new_y='NEXT')
            pdf.set_font('Invoice', '', 10)
            for value in (name, email):
                if value:
                    pdf.multi_cell(0, 6, value, new_x='LMARGIN', new_y='NEXT')
            pdf.ln(4)

    def _add_line_items(self):
        pdf = self.pdf
        pdf.set_font('Invoice', '', 9)
        pdf.set_fill_color(255, 255, 255)
        subtotal = 0.0
        complete = bool(self.order.get('items'))
        with pdf.table(col_widths=(70, 30, 20, 35, 35), line_height=6, padding=1,
                       text_align=('LEFT', 'LEFT', 'RIGHT', 'RIGHT', 'RIGHT'),
                       headings_style=FontFace(emphasis='BOLD', color=(255,255,255), fill_color=self.primary_rgb),
                       borders_layout='HORIZONTAL_LINES', repeat_headings=1) as table:
            row = table.row()
            for value in ('Item', 'SKU', 'Qty', 'Unit Price', 'Subtotal'):
                row.cell(value)
            for item in self.order.get('items', []):
                quantity = item.get('quantity', 0)
                price = item.get('unit_price')
                # A missing price must never become a free line through enrichment.
                value = price * quantity if price is not None else None
                if value is None:
                    complete = False
                else:
                    subtotal += value
                row = table.row()
                for text in (item.get('product_name') or f"Product #{item.get('product_id', '?')}",
                             item.get('product_sku') or '-', str(quantity), _fmt(price), _fmt(value)):
                    row.cell(text)
        pdf.ln(5)
        return subtotal if complete else None

    def _add_totals(self, subtotal):
        pdf = self.pdf
        # Keep this small block together rather than splitting totals across pages.
        if pdf.will_page_break(30):
            pdf.add_page()
        pdf.set_font('Invoice', '', 10)
        def row(label, value, bold=False):
            pdf.set_font('Invoice', 'B' if bold else '', 10)
            pdf.set_x(110)
            pdf.cell(55, 8, label, align='R')
            pdf.cell(35, 8, _fmt(value), align='R', new_x='LMARGIN', new_y='NEXT')
        row('Items Subtotal:', subtotal)
        shipping = self.order.get('shipping_charged')
        if shipping is not None:
            row('Shipping:', shipping)
        # Revenue already includes charged shipping; adding it again double bills.
        total = self.order.get('revenue')
        if total is None and subtotal is not None:
            total = subtotal + (shipping if shipping is not None else 0)
        row('Invoice Total:', total, bold=True)

    def generate(self):
        self.pdf.add_page()
        self._add_header()
        self._add_customer()
        self._add_totals(self._add_line_items())
        return self.pdf.output()
