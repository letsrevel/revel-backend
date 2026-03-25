# Implementation Plan: Attendee Invoicing (Issue #350)

## Overview

Issue invoices to attendees (buyers) on behalf of organizers (sellers) for online ticket purchases. The organizer is the legal seller; Revel acts as an intermediary generating and delivering invoices. VAT treatment is determined at checkout time based on the buyer's billing info, meaning the Stripe checkout amount varies per buyer.

## Key Design Decisions

| Decision | Choice |
|----------|--------|
| Invoicing mode | **Enum** on Organization: `NONE` (default), `HYBRID`, `AUTO` |
| HYBRID mode | Invoice generated as **DRAFT** — org can review, edit any field (except seller/org), delete, or manually issue/send |
| AUTO mode | Invoice generated as **ISSUED** and emailed immediately |
| VAT calculation | At **checkout time** based on buyer billing info — not retroactive |
| VAT preview | New endpoint calculates/validates before Stripe redirect |
| VIES caching | Redis, 30min TTL, keyed by VAT ID |
| Billing snapshot | `buyer_billing_snapshot` JSONField on **every** Payment in the batch |
| Invoice amounts | Initially populated from Payment records; fully editable in DRAFT (org is liable) |
| VIES unavailable fallback | Standard VAT (full price) — FE gates checkout until validated |
| Platform fee base | Calculated on the **amount actually charged** (net for reverse charge). Attendee invoices are independent of platform fee invoices — orgs editing draft amounts doesn't affect Revel's billing |
| Scope | Online (Stripe) payments only |

## Invoicing Modes

| Mode | Generation | Status | Editable | Email | Use case |
|------|-----------|--------|----------|-------|----------|
| `NONE` | No invoice generated | — | — | — | Default, invoicing not needed |
| `HYBRID` | Auto-generated as `DRAFT` | DRAFT | All fields except seller (org) info | Manual by org admin | Org wants control, needs to review/adjust before sending |
| `AUTO` | Auto-generated as `ISSUED` | ISSUED | Immutable | Auto-sent to buyer | Org trusts the system, wants hands-off |

### HYBRID Mode Details
- Org admin can **edit** any field on a DRAFT invoice: buyer info, amounts, line items, VAT, currency — everything except seller (org) info. The org is liable for the invoice, not Revel.
- Org admin can **delete** a DRAFT (buyer didn't want an invoice, mistake, etc.)
- Org admin can **issue** a DRAFT → status becomes ISSUED, PDF is finalized, email is sent
- Once ISSUED, the invoice is immutable (can only be cancelled via credit note)
- Revel's platform fee invoices are always based on actual Payment/Stripe data, never on attendee invoice content

### Validation for HYBRID or AUTO
Same checks as enabling ticket sales:
1. `vat_country_code` in `EU_MEMBER_STATES`
2. `vat_id_validated` is `True`
3. `billing_name` is non-empty
4. `billing_address` is non-empty

Setting to `NONE` is always allowed.

## EU VAT Rules for Event Tickets

| Scenario | VAT Treatment | Buyer Pays |
|----------|--------------|------------|
| Domestic B2C (same country, no VAT ID) | Org's VAT rate | Gross (VAT-inclusive) |
| Domestic B2B (same country, valid VAT ID) | Org's VAT rate | Gross (VAT-inclusive) |
| EU cross-border B2B (different EU country, valid VAT ID) | **Reverse charge** (0%) | Net only |
| EU cross-border B2C (different EU country, no VAT ID) | Org's VAT rate | Gross (VAT-inclusive) |
| Non-EU buyer | No VAT (export) | Net only |

The tier price is always VAT-inclusive. For reverse charge / non-EU, the buyer pays the net portion only (gross minus VAT).

---

## Phase 1: Models & Migration

### Files to create
- `src/events/models/attendee_invoice.py`

### Files to modify
- `src/events/models/ticket.py` — add `buyer_billing_snapshot` on Payment
- `src/events/models/organization.py` — add `invoicing_mode` on Organization
- `src/events/models/__init__.py` — register new models

### AttendeeInvoice

```python
class AttendeeInvoice(TimeStampedModel):
    class InvoiceStatus(models.TextChoices):
        DRAFT = "draft"        # HYBRID mode: editable, not yet sent
        ISSUED = "issued"      # Finalized and sent (or ready to send)
        CANCELLED = "cancelled"  # Fully credited via credit note

    organization = FK(Organization, SET_NULL, null=True, related_name="attendee_invoices")
    event = FK(Event, SET_NULL, null=True)
    user = FK(RevelUser, SET_NULL, null=True, related_name="attendee_invoices")
    stripe_session_id = CharField(max_length=255, db_index=True)

    invoice_number = CharField(max_length=50, unique=True)
    status = CharField(choices=InvoiceStatus, default=InvoiceStatus.DRAFT)

    # Totals (initially from Payments, fully editable in DRAFT)
    total_gross = DecimalField(max_digits=10, decimal_places=2)
    total_net = DecimalField(max_digits=10, decimal_places=2)
    total_vat = DecimalField(max_digits=10, decimal_places=2)
    vat_rate = DecimalField(max_digits=5, decimal_places=2)
    currency = CharField(max_length=3)
    reverse_charge = BooleanField(default=False)

    # Discount
    discount_code_text = CharField(max_length=64, blank=True)
    discount_amount_total = DecimalField(max_digits=10, decimal_places=2, default=0)

    # Line items (JSON snapshot, editable in DRAFT)
    line_items = JSONField(default=list)
    # Structure per item:
    # {
    #     "description": "Event Name — Tier Name — Guest Name",
    #     "unit_price_gross": "121.00",
    #     "discount_amount": "10.00",
    #     "net_amount": "91.74",
    #     "vat_amount": "19.26",
    #     "vat_rate": "21.00",
    # }

    # Seller snapshot (org at time of purchase — NOT editable)
    seller_name = CharField(max_length=255)
    seller_vat_id = CharField(max_length=20, blank=True)
    seller_vat_country = CharField(max_length=2, blank=True)
    seller_address = TextField(blank=True)
    seller_email = EmailField()

    # Buyer snapshot (editable in DRAFT)
    buyer_name = CharField(max_length=255)
    buyer_vat_id = CharField(max_length=20, blank=True)
    buyer_vat_country = CharField(max_length=2, blank=True)
    buyer_address = TextField(blank=True)
    buyer_email = EmailField()

    issued_at = DateTimeField(null=True)
    pdf_file = ProtectedFileField(upload_to="invoices/attendee/", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            UniqueConstraint(fields=["stripe_session_id"], name="unique_attendee_invoice_per_session"),
        ]
        indexes = [
            Index(fields=["organization", "created_at"]),
            Index(fields=["user", "created_at"]),
        ]
```

### AttendeeInvoiceCreditNote

```python
class AttendeeInvoiceCreditNote(TimeStampedModel):
    invoice = FK(AttendeeInvoice, PROTECT, related_name="credit_notes")
    credit_note_number = CharField(max_length=50, unique=True)

    amount_gross = DecimalField(max_digits=10, decimal_places=2)
    amount_net = DecimalField(max_digits=10, decimal_places=2)
    amount_vat = DecimalField(max_digits=10, decimal_places=2)

    line_items = JSONField(default=list)
    payments = M2M(Payment, blank=True, related_name="attendee_credit_notes")

    issued_at = DateTimeField(null=True)
    pdf_file = ProtectedFileField(upload_to="invoices/attendee/credit_notes/", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
```

### Payment.buyer_billing_snapshot

```python
buyer_billing_snapshot = JSONField(null=True, blank=True, default=None)
# Structure:
# {
#     "billing_name": "Acme Corp",
#     "vat_id": "DE123456789",
#     "vat_country_code": "DE",
#     "vat_id_validated": true,
#     "billing_address": "123 Main St, Berlin",
#     "billing_email": "billing@acme.de"
# }
```

Define a `BuyerBillingSnapshot` TypedDict in `attendee_invoice.py` for type safety.

### Organization.invoicing_mode

```python
class InvoicingMode(models.TextChoices):
    NONE = "none"
    HYBRID = "hybrid"
    AUTO = "auto"

invoicing_mode = CharField(
    max_length=10,
    choices=InvoicingMode.choices,
    default=InvoicingMode.NONE,
)
```

---

## Phase 2: Org Opt-In & Validation

### Files to create
- `src/events/service/attendee_invoice_service.py` (validation logic; generation added in Phase 4)

### Files to modify
- `src/events/controllers/organization_admin/vat.py` — add mode endpoint
- `src/events/schema/invoice.py` — add mode schema
- `src/events/schema/organization.py` — include `invoicing_mode` in billing info schema

### Validation rules (for HYBRID or AUTO)

`validate_invoicing_prerequisites(org) -> None` raises HttpError(422) if:
1. `vat_country_code` not in `EU_MEMBER_STATES`
2. `vat_id_validated` is not `True`
3. `billing_name` is empty
4. `billing_address` is empty

Same checks as enabling ticket sales.

### Endpoint

```
PATCH /organization-admin/{slug}/invoicing
    permissions: IsOrganizationOwner
    payload: { mode: Organization.InvoicingMode }
    response: OrganizationBillingInfoSchema
```

Setting to `NONE` is always allowed. `HYBRID` or `AUTO` runs validation first.

---

## Phase 3: VAT Preview & Checkout Integration

### Phase 3a: VIES Redis Caching

**Modify**: `src/common/service/vies_service.py`

```python
def validate_vat_id_cached(vat_id: str) -> VIESValidationResult:
    cache_key = f"vies:validation:{vat_id.upper().replace(' ', '')}"
    cached = cache.get(cache_key)
    if cached is not None:
        return VIESValidationResult(**cached)
    result = validate_vat_id(vat_id)  # may raise VIESUnavailableError
    cache.set(cache_key, asdict(result), timeout=1800)  # 30min
    return result
```

On `VIESUnavailableError`: do NOT cache, re-raise.

### Phase 3b: VAT Preview Endpoint

**Create**: `src/events/service/attendee_vat_service.py`

Core function:
```python
def calculate_attendee_vat_breakdown(
    org: Organization,
    items: list[VATPreviewItem],  # tier, count, effective_price
    buyer_country: str,
    buyer_vat_id_valid: bool,
) -> AttendeeVATBreakdown:
```

**Add schemas to**: `src/events/schema/ticket.py`

```python
class BuyerBillingInfoSchema(Schema):
    billing_name: str
    vat_id: str = ""
    vat_country_code: str = ""
    billing_address: str = ""
    billing_email: str = ""
    save_to_profile: bool = False

class VATPreviewRequestSchema(Schema):
    billing_info: BuyerBillingInfoSchema
    items: list[VATPreviewItemSchema]  # tier_id, count

class VATPreviewLineItemSchema(Schema):
    tier_name: str
    ticket_count: int
    unit_price_gross: Decimal
    unit_price_net: Decimal
    unit_vat: Decimal
    vat_rate: Decimal
    line_net: Decimal
    line_vat: Decimal
    line_gross: Decimal

class VATPreviewResponseSchema(Schema):
    vat_id_valid: bool | None = None
    vat_id_validation_error: str | None = None
    reverse_charge: bool
    line_items: list[VATPreviewLineItemSchema]
    total_net: Decimal
    total_vat: Decimal
    total_gross: Decimal
    currency: str
```

**Endpoint** on ticket controller:
```
POST /events/{event_id}/vat-preview
    auth: I18nJWTAuth
    throttle: WriteThrottle
    payload: VATPreviewRequestSchema
    response: VATPreviewResponseSchema
```

### Phase 3c: Checkout Integration

**Modify**:
- `src/events/schema/ticket.py` — add `billing_info: BuyerBillingInfoSchema | None = None` to batch checkout payloads
- `src/events/service/batch_ticket_service.py` — accept and pass `billing_info`
- `src/events/service/stripe_service.py` — adjust Stripe amounts, store snapshot on Payments
- `src/events/controllers/event_public/tickets.py` — pass billing_info through

**Flow**:
1. Controller receives optional `billing_info`
2. BatchTicketService passes to `_online_checkout()`
3. stripe_service recalculates per-ticket amounts if billing triggers reverse charge / export
4. Platform fee calculated on **actual charged amount**
5. `buyer_billing_snapshot` stored on every Payment in the batch
6. If `save_to_profile=True` → upsert UserBillingProfile

**Edge cases**:
- `billing_info=None` → current behavior, no invoice generated
- VIES was cached as valid at preview time → reuse at checkout (no re-validation needed)

---

## Phase 4: Invoice Generation

### Files to create
- `src/templates/invoices/attendee_invoice.html`

### Files to modify
- `src/events/service/attendee_invoice_service.py` — generation logic
- `src/events/service/stripe_webhooks.py` — hook into payment success
- `src/events/tasks.py` — Celery task

### Generation logic

`generate_attendee_invoice(stripe_session_id: str) -> AttendeeInvoice | None`

1. Find Payments by `stripe_session_id` + `status=SUCCEEDED`
2. Check: `buyer_billing_snapshot` is not None on first payment
3. Check: org has `invoicing_mode != NONE`
4. Idempotency: return existing invoice if already exists for this session
5. Determine initial status:
   - `HYBRID` → `DRAFT`
   - `AUTO` → `ISSUED`
6. Inside `transaction.atomic()`:
   - Number: `{ORG_SLUG_UPPER}-{YEAR}-{SEQ:06d}` via `get_next_sequential_number()`
   - Build line items from Payment records
   - Snapshot seller (org) and buyer (from billing snapshot)
   - Create AttendeeInvoice with appropriate status
   - If AUTO: set `issued_at = now()`
7. Outside transaction:
   - Render PDF via WeasyPrint
   - Save to `pdf_file`
   - If AUTO: trigger email delivery

### Invoice numbering

Uses `get_next_sequential_number()` with per-org prefix:
- Prefix: `TECHCONF-` → `TECHCONF-2026-000001`
- SELECT FOR UPDATE prevents race conditions
- Number is assigned at generation time (even for DRAFT — ensures sequential integrity)

### Webhook integration

In `handle_checkout_session_completed()`, after payment success:
```python
transaction.on_commit(lambda: generate_attendee_invoice_task.delay(session_id))
```

### PDF template

Extends `_base_styles.html`. Structure:
- Header: "INVOICE" + invoice number + date (+ "DRAFT" watermark if draft)
- Seller block: org billing info (legal name, address, VAT ID)
- Buyer block: attendee billing info
- Line items table: Description | Unit Price | Discount | Net | VAT | Gross
- Totals: Subtotal, VAT (or "Reverse Charge" / "Export — 0%"), Total
- Footer: reverse charge notice if applicable

---

## Phase 5: Invoice Delivery & Endpoints

### Files to modify
- `src/events/service/attendee_invoice_service.py` — delivery, editing, issuing functions
- `src/events/controllers/dashboard.py` — attendee download endpoints
- `src/events/controllers/organization_admin/vat.py` — org admin endpoints
- `src/events/schema/invoice.py` — AttendeeInvoice schemas

### Email delivery

`deliver_attendee_invoice(invoice: AttendeeInvoice) -> None`

- **From**: `{org.slug}@letsrevel.io` (display: org billing_name)
- **Reply-To**: org `billing_email` or `contact_email`
- **BCC**: org `billing_email` (so org gets a copy)
- **To**: `invoice.buyer_email` (fallback: user.email)
- **Subject**: `Invoice {invoice_number} — {event_name}`
- **Attachment**: PDF via `attachment_storage_path`

Called automatically for AUTO mode; manually triggered by org admin for HYBRID mode.

### Attendee endpoints

```
GET  /dashboard/invoices                         → paginated list (user's ISSUED invoices only)
GET  /dashboard/invoices/{invoice_id}/download    → signed PDF URL
```

### Org admin endpoints

```
GET    /organization-admin/{slug}/attendee-invoices                        → paginated list (all statuses)
GET    /organization-admin/{slug}/attendee-invoices/{invoice_id}/download   → signed PDF URL
PATCH  /organization-admin/{slug}/attendee-invoices/{invoice_id}            → edit DRAFT (all fields except seller)
POST   /organization-admin/{slug}/attendee-invoices/{invoice_id}/issue      → issue DRAFT → ISSUED + send email
DELETE /organization-admin/{slug}/attendee-invoices/{invoice_id}            → delete DRAFT only
GET    /organization-admin/{slug}/attendee-credit-notes                    → paginated list
```

### DRAFT editing

`update_draft_invoice(invoice: AttendeeInvoice, payload: UpdateAttendeeInvoiceSchema) -> AttendeeInvoice`

- Validates: `invoice.status == DRAFT`, otherwise 409 Conflict
- Accepts: all fields except seller_* (org info) and invoice_number
- Buyer info, amounts, line items, VAT rate, reverse_charge, currency, discount — all editable
- Regenerates PDF preview after edit
- Returns updated invoice

### DRAFT issuing

`issue_draft_invoice(invoice: AttendeeInvoice) -> AttendeeInvoice`

- Sets `status = ISSUED`, `issued_at = now()`
- Finalizes PDF (removes DRAFT watermark)
- Triggers email delivery

---

## Phase 6: Credit Notes

### Files to create
- `src/templates/invoices/attendee_credit_note.html`

### Files to modify
- `src/events/service/attendee_invoice_service.py` — credit note generation
- `src/events/service/stripe_webhooks.py` — hook into refund handler
- `src/events/tasks.py` — Celery task

### Generation logic

`generate_attendee_credit_note(stripe_session_id: str, refunded_payment_ids: list[UUID]) -> AttendeeInvoiceCreditNote | None`

1. Find AttendeeInvoice by `stripe_session_id`
2. If no invoice exists → return None (invoicing wasn't active when purchased)
3. If invoice is DRAFT → delete the draft instead of creating a credit note (not yet issued, can just remove)
4. Inside `transaction.atomic()`:
   - Number: `{ORG_SLUG_UPPER}-CN-{YEAR}-{SEQ:06d}`
   - Build line items from refunded Payments
   - Create credit note, link to payments via M2M
   - If total credited across all credit notes == invoice total → set invoice status to CANCELLED
5. Render PDF, save, email (same delivery pattern as invoices)

### Webhook integration

In `handle_charge_refunded()`, after updating payments:
```python
transaction.on_commit(lambda: generate_attendee_credit_note_task.delay(session_id, refunded_ids))
```

### Credit note PDF template

Similar to invoice but with:
- Title: "CREDIT NOTE" instead of "INVOICE"
- Reference to original invoice number
- Credited amounts shown

---

## Phase 7: Org Dashboard

Mostly covered by Phase 2 (mode toggle) and Phase 5 (listing/editing endpoints). Remaining:

### Files to modify
- `src/events/schema/organization.py` — include `invoicing_mode` in admin detail schema and billing info schema

---

## Dependency Graph

```
Phase 1 (Models)
  ├─→ Phase 2 (Org Opt-In)
  ├─→ Phase 3a (VIES Cache)
  │     └─→ Phase 3b (VAT Preview)
  │           └─→ Phase 3c (Checkout Integration)
  │                 └─→ Phase 4 (Invoice Generation)
  │                       └─→ Phase 5 (Delivery & Endpoints)
  │                             └─→ Phase 6 (Credit Notes)
  └─→ Phase 7 (Dashboard — after Phases 2 + 5)
```

Phases 2, 3a can proceed in parallel after Phase 1.

---

## Files Summary

### New files
| File | Purpose |
|------|---------|
| `src/events/models/attendee_invoice.py` | AttendeeInvoice + CreditNote models + BuyerBillingSnapshot TypedDict |
| `src/events/service/attendee_invoice_service.py` | Validation, generation, editing, issuing, delivery, credit notes |
| `src/events/service/attendee_vat_service.py` | Attendee VAT calculation logic |
| `src/templates/invoices/attendee_invoice.html` | Invoice PDF template |
| `src/templates/invoices/attendee_credit_note.html` | Credit note PDF template |

### Modified files
| File | Changes |
|------|---------|
| `src/events/models/ticket.py` | Add `buyer_billing_snapshot` JSONField on Payment |
| `src/events/models/organization.py` | Add `invoicing_mode` enum + field |
| `src/events/models/__init__.py` | Register new models |
| `src/common/service/vies_service.py` | Add `validate_vat_id_cached()` |
| `src/events/schema/ticket.py` | BuyerBillingInfoSchema, VATPreview schemas, extend batch checkout payloads |
| `src/events/schema/invoice.py` | AttendeeInvoice/CreditNote schemas, mode schema, update schema |
| `src/events/schema/__init__.py` | Register new schemas |
| `src/events/schema/organization.py` | Include `invoicing_mode` in relevant schemas |
| `src/events/service/batch_ticket_service.py` | Accept and pass `billing_info` |
| `src/events/service/stripe_service.py` | Adjust Stripe amounts, store billing snapshot |
| `src/events/service/stripe_webhooks.py` | Trigger invoice/credit note generation |
| `src/events/tasks.py` | Celery tasks for async generation |
| `src/events/controllers/event_public/tickets.py` | VAT preview endpoint, pass billing_info |
| `src/events/controllers/organization_admin/vat.py` | Invoicing mode, attendee invoice CRUD, credit note list |
| `src/events/controllers/dashboard.py` | Attendee invoice list + download endpoints |
