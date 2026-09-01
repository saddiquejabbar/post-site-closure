# Sanitized message examples

## Structured checkout

```text
Checkout by: Alex
Received by: Sam
Customer: Demo Home
Address: 10 Example Road
Quote: Q-1001
Purpose: Install
Items: 2x SKU-SWITCH-1G, 1x zigbee hub
Notes: Packed for tomorrow morning
```

## Natural sentence

```text
Checkout 2x SKU-SWITCH-1G and 1x zigbee hub for Demo Home at 10 Example Road, received by Sam, purpose install
```

When `Checkout By` is omitted, the Telegram sender display name is used.

## Return

```text
Return 2x SKU-SWITCH-1G for Demo Home at 10 Example Road; received by Sam; purpose return
```

Returns are stored as negative quantities.

## Ambiguous alias

```text
Items: 1x controller
```

If `controller` maps to more than one current workbook SKU, the request remains blocked and Telegram shows candidate buttons. No fuzzy match is ever written automatically.
