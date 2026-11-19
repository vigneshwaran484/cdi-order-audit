# Internship Problem Statement: Order-to-Audit System for a Small Food Manufacturer


Centre for Digital Infrastructure, NIT Tiruchirappalli


## 1. Background


Build a working prototype of one system that takes a small food factory from a salesman's order to the monthly auditor's report. This is the shortlisting task for our internship. There is no single right answer: we want to see how you model a real business, not how many features you can list.


The client is a hypothetical small-scale manufacturer of packaged South Indian snacks (murukku, mixture, chips, sweets). It has about 5–15 field salesmen visiting retailers and distributors, one factory with an admin/billing desk, a production floor, and an external accountant and auditor.


Today these functions run separately:


- Salesmen phone in or WhatsApp their orders, which the admin re-types.
- Invoices are made in one tool, stock is kept in a notebook or spreadsheet, and production is tracked on paper.
- Nobody can say quickly how much raw material should have been used against how much was used, so wastage goes unnoticed.
- At month end, someone assembles sales, GST, stock and collection figures by hand for the auditor.


The result is re-entry errors, stock that does not match the books, unknown cost per packet, and a stressful month end.


## 2. The challenge


Design and build a system where every transaction is entered once, at its source, and flows automatically into stock, production, costing and accounts. The core of the problem is linking two loops:


- **Sales loop:** order → verification → GST invoice → dispatch → finished-goods stock goes down → receivables and collections.
- **Production loop:** raw material purchased → issued to production against a recipe (Bill of Materials) → finished packets produced → finished-goods stock goes up → variance and cost per packet.


Both loops meet at finished-goods stock, and both feed the monthly reports. Most invoicing apps handle the first loop. The second one, recipe-based production with planned-vs-actual consumption, is what makes this problem interesting, and it carries the most weight in evaluation.


    SALES LOOP:      Salesman order → Admin verifies → GST invoice → Dispatch ──(stock goes down)──┐
                                                                                                   ▼
    PRODUCTION LOOP: Purchase → Raw stock ──(issued per BOM)──> Production run ──(stock goes up)──> Finished goods
                                                                                                   │
    All of the above → Monthly reports for the auditor (sales & GST, stock ledger, production variance, cost per packet, outstanding)


Accounting software such as Tally must not be required for daily work. An export or sync to such software is a welcome extra, not a dependency.


## 3. Users and roles


| Role | Device | What they need to do |
| --- | --- | --- |
| Salesman | Mobile phone, often on weak network | Pick a customer, add products and quantities, submit an order; see their own orders, status and customer-wise sales |
| Factory admin | Desktop | Receive orders, verify price, quantity and stock, raise GST invoices, record dispatch, record payments |
| Production supervisor | Desktop or tablet on the floor | Record raw-material receipts and issues, log daily production runs, enter actual output and wastage |
| Accountant / auditor | Desktop, read-only | Pull monthly sales, GST, stock, purchase, collection and outstanding reports; export them |
| Owner | Any | See a summary: today's orders, stock levels, variance alerts, cost per packet, outstanding dues |


Each role should see only what it needs. Role-based access is part of the requirement, not an extra.


## 4. Functional requirements


### 4.1 Field order taking (mobile)


- Salesman logs in, selects a customer (retailer or distributor) and adds products with quantities.
- Price comes from a price list; customer-specific prices or schemes are optional.
- Order is submitted straight to the factory, with no phone call or WhatsApp step.
- Orders taken offline are saved and sent when the network returns (strongly preferred).
- Salesman sees order status (submitted, approved, invoiced, dispatched) and their customer-wise sales.


### 4.2 Order verification and GST billing


- Admin sees incoming orders, can edit quantities or prices, and approves or rejects them.
- Before approval, the system shows whether finished-goods stock covers the order.
- Approved orders become GST-compliant tax invoices: GSTIN, HSN code, taxable value, CGST/SGST or IGST by place of supply, invoice numbering by financial year.
- Invoice can be downloaded as PDF and shared with the customer (WhatsApp share link or email is enough).
- Dispatch reduces finished-goods stock. Partial dispatch and returns are optional.
- Payments received are recorded against invoices, giving each customer's outstanding balance.


### 4.3 Bill of Materials (BOM) and production


This module carries the most weight.


- Each finished product has a BOM: the standard quantity of each raw material and packaging item per batch, plus the expected yield (packets per batch).
- A production run records: product, batch size or raw material issued, expected output (from the BOM), actual output, and wastage or scrap.
- On saving a run, raw-material stock goes down and finished-goods stock goes up automatically.
- The system compares planned vs actual consumption and planned vs actual yield, and flags variance beyond a set tolerance (for example 3%).
- Units must convert correctly: kg, g, litres, pieces, packets, and cartons of packets.


### 4.4 Inventory


Maintain separate, always-current balances for:


- Raw materials (flour, oil, spices, and so on)
- Packaging materials (pouches, cartons, labels)
- Work in progress, if you choose to model it
- Finished goods, by product and pack size


For every item, opening balance + receipts − issues or sales ± adjustments = closing balance. A stock ledger showing every movement and its source document is required. Batch numbers and expiry dates are a strong plus for food products.


### 4.5 Costing


- Cost of raw materials and packaging per run, from purchase rates (pick and justify a method: last price, weighted average, or FIFO).
- Labour, power, gas and other overheads per run or per month, allocated to products by a rule you choose and explain.
- Output: an approximate cost per packet for each product, and margin against its selling price.


### 4.6 Reports for accounts and audit


Monthly reports, filterable by date range and exportable to Excel/CSV and PDF:


- Sales register, customer-wise and product-wise sales
- GST summary of outward supplies (in a shape that maps to GSTR-1 / GSTR-3B)
- Purchase register
- Stock summary and stock ledger for raw, packaging and finished goods
- Production report with planned vs actual consumption, yield and wastage
- Payments and collections, outstanding (receivables) by customer with ageing


Integration or export to accounting software (such as a Tally-compatible XML or Excel import) is optional.


## 5. Benchmark scenario (mandatory)


Every submission must run this scenario end to end in its demo video, using exactly this data. It is how we compare submissions like for like.


**The key question:** if the factory uses 40 kg of flour to make about 200 packets of 200 g murukku, does the system record raw-material consumption, expected output, actual output, wastage, closing raw-material balance and finished-goods stock automatically?


### Master data


BOM for **Murukku 200 g**, one batch = 200 packets expected:


| Item | Qty per batch | Unit | Rate (₹) |
| --- | --- | --- | --- |
| Flour | 40 | kg | 42 opening stock, 45 new purchase |
| Cooking oil | 10 | litre | 140 |
| Salt and spice mix | 1.2 | kg | 200 |
| 200 g pouch | 200 | piece | 2.50 |
| Carton (holds 20 packets) | 10 | piece | 15 |


Opening stock: flour 100 kg, oil 30 L, spice mix 5 kg, pouches 500, cartons 30, finished Murukku 200 g 50 packets. Selling price ₹50 per packet before tax; assume GST at 5% for this exercise. The factory is in Tamil Nadu.


### Steps to demonstrate


1. Record a purchase of 60 kg flour at ₹45/kg.
2. Start one production run of Murukku 200 g. Issue 40 kg flour, 10.5 L oil (0.5 L over standard), 1.2 kg spice mix.
3. Record actual output of 188 good packets, 6 pouches damaged, and 9 cartons used.
4. Salesman A, on mobile, places an order for Retailer A (Tamil Nadu): 150 packets.
5. Admin approves it, raises the GST invoice (CGST + SGST) and records dispatch.
6. Salesman B places an order for Distributor B (Karnataka): 120 packets. Show how the system handles insufficient stock.
7. Record a payment of ₹4,000 from Retailer A.
8. Add labour of ₹1,200 and gas/power of ₹600 for the run, then show cost per packet.
9. Generate the month's reports from section 4.6.


### Results we will check


| Check | Expected |
| --- | --- |
| Flour closing balance | 120 kg (100 + 60 − 40) |
| Oil closing balance | 19.5 L |
| Pouches closing balance | 306 (188 used + 6 damaged) |
| Finished goods after run | 238 packets (50 + 188) |
| Finished goods after Retailer A dispatch | 88 packets |
| Yield variance | 188 vs 200 expected, −6%, flagged |
| Oil consumption variance | +5%, flagged |
| Retailer A invoice | ₹7,500 taxable + ₹187.50 CGST + ₹187.50 SGST = ₹7,875 |
| Retailer A outstanding | ₹3,875 |
| Distributor B order | Shortage of 32 packets flagged; IGST applies if invoiced |
| Cost per packet | Your figure, with the valuation method and overhead rule stated |


If your numbers differ, explain why in the README. A reasoned difference is fine; an unexplained one is not.


## 6. Scope


A smaller system that runs the benchmark correctly beats a larger one that does not. Build in this order.


**Must have (minimum to be considered)**


- Login with at least three roles: salesman, admin, production
- Product, customer, raw-material and BOM masters
- Mobile-friendly order entry and admin approval
- GST invoice as PDF
- Production run that updates raw-material and finished-goods stock automatically
- Planned vs actual consumption and yield, with variance shown
- Stock summary and stock ledger
- Sales register and stock report, exportable to Excel/CSV


**Should have**


- Purchases and supplier records
- Payments, outstanding and ageing
- Cost per packet with overheads
- GST summary report
- Variance alerts beyond a tolerance
- Owner dashboard


**Stretch goals (pick any)**


- Offline order capture with sync
- Batch and expiry tracking, first-expiry-first-out dispatch
- Reorder levels and low-stock alerts for raw materials
- Production planning from pending orders ("to fulfil today's orders, make 2 batches")
- Salesman route or visit log, with GPS check-in
- Tally-compatible export
- e-Invoice / e-Way Bill data format (mock the government API)
- Tamil language interface for floor staff


## 7. Technical expectations


You choose the stack: web, mobile (native or PWA), low-code, or a mix. Justify the choice in two or three sentences in your README.


Whatever you choose, we expect:


- **A real database** with a schema you can explain. Stock balances must be derived from, or reconciled against, a ledger of movements, never just a number someone overwrites.
- **Correctness over polish.** Quantities and money must not drift through rounding. Use decimal or integer-paisa arithmetic, not floats, for money.
- **Traceability.** Every stock movement links back to its source: a purchase, production run, invoice or adjustment.
- **Audit trail.** Edits to invoices and production runs are logged with user and time; posted invoices are cancelled with a credit note, not deleted.
- **Seed data.** A script or file that loads the section 5 master data so we can rerun the benchmark ourselves.
- **Deployability.** A hosted demo link, or a single command to run it locally (Docker Compose or similar).


You may use AI coding assistants, open-source libraries and templates. You must understand every part of what you submit, because shortlisted candidates will be asked to explain and change their code live.


## 8. Deliverables


Submit one link to a public Git repository (GitHub or GitLab) containing:


1. **Source code** with a clear commit history. A single "final upload" commit counts against you.
2. **README** covering setup steps, stack and why, the features you built against the section 6 tiers, assumptions, known gaps, and your benchmark numbers with explanations of any differences.
3. **Design note** (max 2 pages, PDF or Markdown) with a data model or ER diagram, how stock and BOM consumption are computed, and your costing and valuation method.
4. **Demo video** (max 8 minutes, unlisted YouTube or Drive link) walking through the section 5 scenario in order, showing both the mobile salesman view and the admin view.
5. **Live link or run command** so we can try it ourselves, with test logins for each role.


Work alone: this is an individual task, and team submissions will not be accepted.


## 9. Evaluation


| Criterion | What we look for | Weight |
| --- | --- | --- |
| Benchmark correctness | Section 5 results match, or differences are explained | 30% |
| Production and inventory design | BOM model, unit conversion, ledger-based stock, variance logic | 20% |
| Sales-to-invoice flow | Mobile order to approval to GST invoice to dispatch, with stock checks | 15% |
| Data model and code quality | Clean schema, readable code, sensible structure, tests for stock and tax maths | 15% |
| Usability | A salesman can place an order in under a minute; screens fit each role | 10% |
| Reports and documentation | Auditor-ready exports, clear README and design note | 10% |
| Stretch goals | Bonus, only counted once the must-haves work | up to +10% |


Shortlisted applicants will have a 30-minute interview: a live walkthrough, questions on their design choices, and a small change made on the spot (for example, "add a second pack size of 500 g sharing the same BOM").


## 10. Rules and timeline


| Item | Detail |
| --- | --- |
| Problem released | [date] |
| Questions window | Until [date], by email to [contact]. Answers are shared with all applicants. |
| Submission deadline | [date, time IST] via [form link] |
| Shortlist announced | [date] |
| Interviews | [date range] |


- The business described here is a realistic composite. Where something is unclear, make a reasonable assumption and write it down. Documented assumptions are part of what we evaluate.
- Use only the dummy data in section 5 and data you invent. Do not use real customer or company data.
- Plagiarised or copied submissions are disqualified. Two submissions with substantially the same code are both disqualified.
- You keep the rights to your code. By submitting, you allow the evaluation panel to run and review it.