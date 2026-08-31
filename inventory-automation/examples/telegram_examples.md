# Telegram input examples

Recommended single-message format:

```text
Checkout 2x Atlas 75 white, 1x Zigbee hub | customer Tan | site Punggol | installer Hasan | job ZD-1042
```

Supported sentence form:

```text
Checkout 2x Atlas 75 white and 1x Zigbee hub for Tan at Punggol. Installer Hasan. Job ZD-1042.
```

Ambiguous input deliberately enters the review queue:

```text
Checkout 2x Atlas 75 | customer Tan | site Punggol | installer Hasan | job ZD-1042
```

The bot shows one button per matching SKU. It will not guess the colour.

Missing hard job key deliberately enters the review queue:

```text
Checkout 1x Zigbee hub | customer Tan | site Punggol | installer Hasan
```

Unknown items are not written:

```text
Checkout 1x mystery controller | customer Tan | site Punggol | installer Hasan | job ZD-1042
```
