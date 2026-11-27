# Order-to-Audit: a small snack factory from salesman's order to auditor's report

This is the shortlisting task for the NIT Trichy CDI internship. It links two loops:

- **Sales loop:** salesman's phone order → admin check → GST invoice → dispatch → payment.
- **Production loop:** purchase → BOM-based production run → finished goods, with planned vs actual variance and cost per packet.

Both loops share one ledger-based stock model, and both feed the monthly audit reports.

The design note (data model, stock and BOM maths, costing method) is in [docs/DESIGN.md](docs/DESIGN.md). The original problem statement is in [docs/PROBLEM.md](docs/PROBLEM.md).

## Run it

```bash
docker compose up --build
# open http://localhost:8000
```

The first start creates the tables and loads the section 5 benchmark data. To reset to that data later:
`docker compose exec app python -m app.seed`

| Login | Role | Sees |
| --- | --- | --- |
| `salesman_a`, `salesman_b` | Salesman | Phone order screen, own orders and customer-wise sales |
| `admin` | Factory admin | Orders, invoices, dispatch, payments, plus everything else |
| `production` | Production supervisor | Purchases, production runs, overheads, stock, masters |
| `auditor` | Accountant / auditor | Reports, invoices, stock ledger, audit log (read-only) |
| `owner` | Owner | Dashboard, reports, stock |

The password for every demo login is `demo123`. All names, GSTINs and logins are invented.

**Running without Docker:**
1. Start the database with `docker compose up db` (or any Postgres on port 5433 with user/password `cdi`/`cdi`).
2. Then:
   ```bash
   cd backend
   uv sync
   uv run python -m app.seed
   uv run uvicorn app.main:app --reload
   uv run pytest    # 36 tests; uses a separate database, cdi_test
   ```

**Opening the salesman screen on a real phone:** use `http://<laptop-ip>:8000` on the same Wi-Fi. Browsers only allow service workers on `localhost` or HTTPS. Over plain HTTP on a LAN IP, offline orders are still queued and sent later, but the page itself must be open already.

## Stack and why

- **FastAPI + SQLAlchemy + PostgreSQL**, with server-rendered pages (Jinja2).
  - The hard parts of this problem are the data model and the maths: ledger, BOM, GST, costing. Those live in plain Python services with tests.
  - Server-rendered screens keep each role's pages simple and need no separate frontend build.
  - Postgres gives exact `NUMERIC` money, transactions, and row locks for invoice numbering.
- **The salesman screen is a small PWA** (service worker + localStorage queue), so orders can be taken on a weak network.

## What's built, against section 6

**Must have: all done**
- Login with five roles; every route checks the role (a 403 page otherwise).
- Masters: items (raw, packaging, finished), recipe/BOM and pack sizes, customers, suppliers. You can add a new pack size that shares an existing recipe.
- Mobile order entry: pick a customer, tap +/−, submit (under a minute). Admin edits, approves or rejects, with a live stock check.
- GST invoice PDF:
  - Includes GSTIN, HSN, place of supply, CGST/SGST or IGST, financial-year numbering (`INV/2026-27/0001`) and the amount in words.
  - Can be shared via a WhatsApp or email link. The link carries a signed URL, so the customer can open the PDF without logging in.
- A production run updates raw-material and finished-goods stock automatically, and shows planned vs actual consumption and yield with variance.
- Stock summary and stock ledger, where every movement names its source document.
- Every report exports to Excel, CSV and PDF.

**Should have: all done**
- Purchases and suppliers.
- Payments against an invoice or on account, outstanding balances, and ageing (0–30 / 31–60 / 61–90 / 90+ days).
- Cost per packet with overheads (direct per run, or a monthly pool shared by kg).
- GST summary in GSTR-1 sections (B2B / B2C / credit notes), the GSTR-3B 3.1(a) total, and the HSN summary (GSTR-1 table 12).
- Variance alerts beyond a configurable tolerance (3% by default).
- Owner dashboard: today's orders, approvals pending, stock and reorder flags, variance alerts, cost and margin per packet, outstanding dues.

**Stretch: done**
- Offline order capture with sync, with no duplicates on resend.
- Reorder levels with low-stock flags.
- Batch numbers on production runs.

**Other things the brief asks for**
- Audit trail with before/after values for every edit.
- Posted invoices are cancelled with a credit note (stock comes back if the goods had been dispatched), never deleted.
- `Decimal` arithmetic everywhere.
- Seed script for the benchmark data.
- Tests for stock, tax and costing maths, plus one test that drives the whole benchmark through the web app as each role.

## Benchmark results (section 5)

`backend/tests/test_web_benchmark.py` runs these steps through the web screens. The other test files check each figure directly.

| Check | Expected | This system |
| --- | --- | --- |
| Flour closing | 120 kg | **120 kg** |
| Oil closing | 19.5 L | **19.5 l** |
| Pouches closing | 306 | **306** (188 used + 6 damaged issued) |
| Finished goods after run | 238 | **238** |
| After Retailer A dispatch | 88 | **88** |
| Yield variance | −6%, flagged | **−6.00%, flagged** |
| Oil variance | +5%, flagged | **+5.00%, flagged** |
| Retailer A invoice | 7,500 + 187.50 + 187.50 = 7,875 | **INV/2026-27/0001: 7,500.00 + 187.50 CGST + 187.50 SGST = 7,875.00** |
| Retailer A outstanding | 3,875 | **3,875.00** |
| Distributor B | short 32, IGST | **Short by 32 shown; approval blocked.** If the admin cuts the order to 88, it invoices with IGST ₹220 |
| Cost per packet | your figure | **₹31.14** (material ₹21.57 + overheads ₹9.57), margin ₹18.86 (37.7%) |

**Where and why the results differ from, or add to, the expected table:**
- **Pouch variance is also flagged (+3.19%).** The pouch standard is planned on the 188 good packets, and 194 pouches were issued because 6 were damaged. The damage is wastage, so it is flagged alongside yield and oil.
- **Cartons close at 21.** The benchmark says 9 cartons were used, and 188 packets fill 9 full cartons (8 packets loose), so the planned and actual carton counts match.
- **Cost per packet uses weighted-average material cost.** Flour is ₹43.125/kg after the ₹45 purchase. The 6 damaged pouches are included in the cost. Overheads are the ₹1,800 entered for this run. The full working is in docs/DESIGN.md §5.
- **Demo video, step 8:** labour was typed as ₹1,400 instead of ₹1,200 by mistake, so the video shows **₹32.21** per packet (₹4,055 material + ₹2,000 overheads, ÷ 188). With the benchmark's ₹1,200 + ₹600 the system gives **₹31.14**, which `test_cost_per_packet` and the end-to-end web test check. Every other figure in the video matches the benchmark.
- **Opening finished goods (50 packets)** are valued at the standard material cost at opening prices, ₹19.85 per packet. The brief gives no value for them.

## Assumptions

- One batch of the murukku recipe gives 40 kg of product (200 × 200 g). The recipe is stored per batch, and pack sizes divide it by pack weight.
- Recipe inputs are planned per batch. Packaging is planned per good packet, and count items are rounded down to whole pieces.
- "Issued" includes damaged or wasted pieces. Wastage is recorded separately for the report, but it also leaves stock.
- Finished-goods stock goes down at dispatch, not at invoicing. Approved and invoiced orders that haven't been dispatched yet are counted as committed in the stock check, so the same packets can't be promised twice.
- Place of supply = the customer's state, which comes from the GSTIN prefix and the customer master. The factory is in Tamil Nadu (33).
- HSN 21069099 (namkeen) at 5% GST, as the brief says.
- Salesmen cannot change prices; the price list applies. The admin can change price and quantity before approval, and every change is audited.
- A salesman only sees their own customers. Customers with no salesman assigned are visible to all salesmen.
- Purchase amounts are before input tax. The input tax credit side of GST is not modelled.

## Known gaps

- No partial dispatch or partial returns: a credit note reverses the whole invoice.
- Batch numbers are recorded on runs, but stock is not tracked per batch, so there is no first-expiry-first-out dispatch.
- No screens yet to edit or reverse a production run or purchase. Every other edit (orders, invoices) is supported and audited. Run and purchase corrections would be posted as `ADJUSTMENT` ledger rows, which the model supports but the UI doesn't have yet.
- No user-management screen (users come from the seed). No Tally export or e-invoice format.
- Forms rely on same-site session cookies; there are no CSRF tokens yet.
- No hosted demo link. Use `docker compose up`.

## Project layout

```
backend/app/
  models.py          schema (stock_moves ledger, BOM split into recipe + pack lines)
  stock.py           the only code that writes stock; weighted-average cost
  units.py, gst.py   unit conversion; GST maths (no database, easy to test)
  services/          purchases, production, sales, costing, reports, audit
  exports.py         CSV / Excel / PDF for any report; GST invoice PDF
  web/               routes per role; templates/ and static/ (PWA)
backend/tests/       stock, tax, costing and the end-to-end benchmark
docs/                problem statement and design note
```
