"""Turn any Report into CSV, Excel or PDF, and draw the GST invoice PDF.

PDFs use fpdf2's built-in Helvetica so they work in any container without
font files; that font has no rupee sign, so amounts are printed as 'Rs'.
"""

import csv
import io
from datetime import date
from decimal import Decimal

from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.styles import Font

from app.services.reports import Report


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, date):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, Decimal):
        return f"{v.normalize():f}" if v == v.to_integral_value() else f"{v:f}"
    return str(v)


def to_csv(r: Report) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"{r.title}", f"{r.date_from:%d-%m-%Y} to {r.date_to:%d-%m-%Y}"])
    w.writerow([label for _, label in r.columns])
    for row in r.with_totals():
        w.writerow([_cell(row.get(k)) for k, _ in r.columns])
    for n in filter(None, r.notes):
        w.writerow([f"Note: {n}"])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens it as UTF-8


def to_xlsx(r: Report) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = r.key[:31]
    ws.append([r.title, f"{r.date_from:%d-%m-%Y} to {r.date_to:%d-%m-%Y}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([label for _, label in r.columns])
    for c in ws[2]:
        c.font = Font(bold=True)
    for row in r.with_totals():
        # Decimals go in as exact strings -> numbers via Decimal, never float maths.
        out = []
        for k, _ in r.columns:
            v = row.get(k)
            out.append(v if isinstance(v, (Decimal, int, date, str)) or v is None else str(v))
        ws.append(out)
        if row.get(r.columns[0][0]) == "TOTAL":
            for c in ws[ws.max_row]:
                c.font = Font(bold=True)
    for n in filter(None, r.notes):
        ws.append([f"Note: {n}"])
    for col in ws.columns:
        width = max(len(_cell(c.value)) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 45)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _PDF(FPDF):
    company = ""

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        self.cell(0, 6, f"{self.company} - page {self.page_no()}/{{nb}}", align="C")


def _ascii(s: str) -> str:
    return s.replace("₹", "Rs ").encode("latin-1", "replace").decode("latin-1")


def to_pdf(r: Report, company: str) -> bytes:
    pdf = _PDF(orientation="L", unit="mm", format="A4")
    pdf.company = _ascii(company)
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 7, _ascii(company), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, _ascii(f"{r.title}  |  {r.date_from:%d %b %Y} to {r.date_to:%d %b %Y}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    rows = r.with_totals()
    texts = [[_ascii(_cell(row.get(k))) for k, _ in r.columns] for row in rows]
    pdf.set_font("Helvetica", "", 7.5)
    widths = []
    for i, (_, label) in enumerate(r.columns):
        longest = max([pdf.get_string_width(label)] + [pdf.get_string_width(t[i]) for t in texts])
        widths.append(min(longest + 3, 70))
    scale = min(1, (pdf.w - 20) / sum(widths))
    widths = [w * scale for w in widths]

    def header():
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_fill_color(230, 230, 230)
        for w, (_, label) in zip(widths, r.columns):
            pdf.cell(w, 6, _ascii(label), border=1, fill=True)
        pdf.ln()

    header()
    for row, t in zip(rows, texts):
        if pdf.get_y() > pdf.h - 20:
            pdf.add_page()
            header()
        bold = row.get(r.columns[0][0]) == "TOTAL"
        pdf.set_font("Helvetica", "B" if bold else "", 7.5)
        for w, (k, _), txt in zip(widths, r.columns, t):
            num = isinstance(row.get(k), (Decimal, int))
            while txt and pdf.get_string_width(txt) > w - 1:
                txt = txt[:-1]
            pdf.cell(w, 5.5, txt, border=1, align="R" if num else "L")
        pdf.ln()
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 7.5)
    for n in filter(None, r.notes):
        pdf.multi_cell(0, 4.5, _ascii(f"Note: {n}"), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# ---------------------------------------------------------------- invoice

def _words(n: int) -> str:
    """Indian numbering in words, e.g. 7875 -> 'Seven Thousand Eight Hundred Seventy Five'."""
    ones = ("Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen "
            "Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen").split()
    tens = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()

    def two(x):
        return ones[x] if x < 20 else tens[x // 10] + ("" if x % 10 == 0 else " " + ones[x % 10])

    def three(x):
        h, rest = divmod(x, 100)
        parts = ([ones[h] + " Hundred"] if h else []) + ([two(rest)] if rest else [])
        return " ".join(parts)

    if n == 0:
        return "Zero"
    out = []
    for size, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        q, n = divmod(n, size)
        if q:
            out.append(f"{three(q) if q < 1000 else _words(q)} {name}")
    if n:
        out.append(three(n))
    return " ".join(out)


def amount_in_words(x: Decimal) -> str:
    rupees, paise = int(x), int((x - int(x)) * 100)
    s = f"Rupees {_words(rupees)}"
    if paise:
        s += f" and {_words(paise)} Paise"
    return s + " Only"


def invoice_pdf(inv, settings: dict, state_names: dict) -> bytes:
    pdf = _PDF(orientation="P", unit="mm", format="A4")
    pdf.company = _ascii(settings["company_name"])
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 8, "TAX INVOICE", align="C", new_x="LMARGIN", new_y="NEXT")
    if inv.status == "CANCELLED":
        pdf.set_text_color(200, 0, 0)
        pdf.cell(0, 6, "CANCELLED - reversed by credit note", align="C",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, _ascii(settings["company_name"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, _ascii(settings["company_address"]), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"GSTIN: {settings['company_gstin']}   State: "
                   f"{state_names.get(settings['company_state_code'], '')} "
                   f"({settings['company_state_code']})", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    y = pdf.get_y()
    c = inv.customer
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(95, 5, "Bill to / Ship to", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for line in (c.name, c.address or "", f"GSTIN: {c.gstin or 'Unregistered'}",
                 f"State: {state_names.get(c.state_code, '')} ({c.state_code})"):
        pdf.cell(95, 5, _ascii(line), new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(115, y)
    for label, val in (("Invoice no", inv.number), ("Date", f"{inv.invoice_date:%d-%m-%Y}"),
                       ("Place of supply", f"{state_names.get(inv.place_of_supply, '')} "
                                           f"({inv.place_of_supply})"),
                       ("Reverse charge", "No")):
        pdf.set_x(115)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(30, 5, label)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(55, 5, _ascii(val), new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(max(pdf.get_y(), y + 27))
    pdf.ln(3)

    intra = inv.igst == 0 and (inv.cgst > 0 or inv.place_of_supply == settings["company_state_code"])
    cols = [("#", 7), ("Description", 48), ("HSN", 18), ("Qty", 14), ("Rate", 17),
            ("Taxable", 22), ("GST%", 11)]
    cols += [("CGST", 18), ("SGST", 18)] if intra else [("IGST", 36)]
    cols += [("Total", 0)]
    used = sum(w for _, w in cols)
    cols[-1] = ("Total", 190 - used)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(230, 230, 230)
    for label, w in cols:
        pdf.cell(w, 6, label, border=1, fill=True, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 8)
    for i, ln in enumerate(inv.lines, 1):
        tax = [ln.cgst, ln.sgst] if intra else [ln.igst]
        vals = [str(i), ln.item.name, ln.hsn_code, f"{ln.qty.normalize():f} pkt", f"{ln.rate:.2f}",
                f"{ln.taxable:.2f}", f"{ln.gst_rate.normalize():f}"] + [f"{t:.2f}" for t in tax]
        vals.append(f"{ln.taxable + ln.cgst + ln.sgst + ln.igst:.2f}")
        for (label, w), v in zip(cols, vals):
            pdf.cell(w, 6, _ascii(v), border=1, align="L" if label == "Description" else "R")
        pdf.ln()

    pdf.ln(2)
    summary = [("Taxable value", inv.taxable)]
    summary += ([(f"CGST", inv.cgst), ("SGST", inv.sgst)] if intra else [("IGST", inv.igst)])
    summary += [("Invoice total", inv.total)]
    for label, val in summary:
        pdf.set_x(130)
        pdf.set_font("Helvetica", "B" if label == "Invoice total" else "", 9)
        pdf.cell(35, 6, label, border=1)
        pdf.cell(35, 6, f"Rs {val:,.2f}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, _ascii(f"Amount in words: {amount_in_words(inv.total)}"),
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(12)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, _ascii(f"For {settings['company_name']}"), align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.cell(0, 5, "Authorised signatory", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 7)
    pdf.cell(0, 4, "Demo data only: all names and GSTINs are invented.", align="C")
    return bytes(pdf.output())
