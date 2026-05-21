# Reorder

## User goal
Let a returning customer place a repeat order from their order history in three taps or fewer, without rebuilding the cart manually.

## Roles
- **guest** — Anonymous shopper. No order history, so the capability surface degrades to a CTA prompting sign-in.
- **member** — Logged-in customer with at least one prior order.
- **premium_member** — Member with an active subscription. Gets a preferred-merchant accelerator and skips delivery-fee preview.

## Flow per role

### guest
1. Lands on `/home`. The "Order Again" rail is replaced by an empty-state card: *"Sign in to see your past orders."*
2. Tap `Sign in` → routes to `/auth/login`.
3. After successful auth, user is returned to `/home` and the member flow takes over from step 1.

### member
1. Lands on `/home`. The "Order Again" rail shows up to 6 past orders sorted by recency.
2. Taps an order tile → routes to `/reorder/<order_id>/review`.
3. Review screen lists prior items with current prices, flags any items that are now unavailable (badge: *"Sold out"*), and surfaces an editable delivery address.
4. Taps `Add to cart` → routes to `/cart` with the order's items pre-populated.
5. Standard checkout flow takes over from there (see `checkout` capability).

### premium_member
Same as member, plus:
- Past orders rail is preceded by a single accelerated tile labeled *"Reorder your usual from <merchant>"* if a goto-merchant is detected.
- Tapping the accelerator goes directly to `/cart` (skips the review screen) and shows a 4-second undo banner.
- Delivery-fee preview chip is hidden on the cart screen (subscription absorbs the fee).

## Success criteria
- `Add to cart` tap → `/cart` route reached with non-empty cart contents.
- For the accelerated path: the undo banner is dismissed without being tapped (signal that the user accepted the auto-add).

## Edge cases & failure modes
- **Empty order history (member)** — rail collapses; CTA changes to *"Explore restaurants near you."*
- **All prior items unavailable** — `Add to cart` button disabled; copy reads *"None of these items are available right now."*
- **Stale delivery address** — review screen prompts user to confirm or change.
- **Failed auth (guest)** — user remains on `/auth/login`, error toast appears.
- **Network failure on `Add to cart`** — full-screen retry state.

## Dependencies
- Upstream: `order_history` service (read), `merchant_catalog` (item availability + current pricing).
- Downstream: `checkout` capability — every successful reorder enters `/cart` and continues there.

## Implicit metric
Most likely optimized for **reorder-to-cart conversion rate** segmented by lifecycle (new vs returning vs subscribed). The accelerated-tile design pattern suggests a secondary focus on time-to-cart for premium subscribers.
