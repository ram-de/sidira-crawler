# Checkout

## User goal
Convert a cart of items into a confirmed order with minimum friction and full price transparency before commit.

## Roles
- **guest** — Anonymous shopper. Must sign in or create an account before commit; no saved payment methods.
- **member** — Logged-in customer with at least one saved payment method.
- **premium_member** — Member with active subscription. Sees fee waivers applied inline and gets an expedited single-tap commit on saved methods.

## Flow per role

### guest
1. Lands on `/cart` from upstream (reorder, search, or item add).
2. Taps `Checkout` → routes to `/checkout/auth-gate` which asks to *Sign in* or *Continue as guest*.
3. If *Continue as guest*: routes to `/checkout/contact` to collect email + phone before payment.
4. Routes to `/checkout/payment` to enter a card. No saved-method option.
5. Routes to `/checkout/review` showing itemized total + delivery + service + tip selector.
6. Taps `Place order` → routes to `/order/<id>/confirmation`.

### member
1. Lands on `/cart`. Taps `Checkout` → routes directly to `/checkout/review` (skip auth + payment gates because both are on file).
2. Review shows itemized total including delivery + service fees, tip selector defaulting to 18%, and the default saved payment method with a `Change` link.
3. Taps `Place order` → routes to `/order/<id>/confirmation`.

### premium_member
Same as member, plus:
- Fee row shows `$0.00 (Premium)` with a strikethrough on the original fee.
- A `Quick place` button appears at the top of the review screen for orders ≤ $50; tapping it commits without scrolling to confirm.

## Success criteria
- `Place order` tap → `/order/<id>/confirmation` rendered with a valid order ID.
- Confirmation shows ETA and itemized totals matching the review screen exactly.

## Edge cases & failure modes
- **Payment declined** — full-screen error, return user to `/checkout/payment` with the card slot pre-focused.
- **Item went unavailable between cart and review** — review screen surfaces a yellow banner: *"1 item is no longer available — your total has changed."* User must tap `Continue` to proceed.
- **Delivery address out of range** — review screen disables `Place order`; copy: *"This restaurant doesn't deliver to your address."*
- **Subscription expired between cart and checkout (premium_member)** — fee waivers are removed before commit; review screen forces the user to acknowledge the new total.
- **Tip selector NaN** — falls back to 18% silently.

## Dependencies
- Upstream: `cart` (always entered from here), `reorder` (a major source of cart traffic).
- Downstream: `order_tracking`, `payments`, `merchant_dispatch`.

## Implicit metric
Most likely optimized for **cart-to-confirmation conversion rate**, with secondary attention to the premium-only `Quick place` adoption — the existence of an expedited path signals the team is measuring time-to-commit as a friction proxy.
