# Shop fixtures

Two kinds of fixtures live here:

- static directories (`woo-shop`, `shopify-shop`, `parked`, `blog`, `pii`) served by
  `tests/integration/test_light_worker.py` for the light scanner (stage 1);
- directories with a `shop.json` served by the checkout simulator
  `tests/e2e/shopsim.py` (stage 2). The JSON selects the markup *flavour*
  (`woocommerce`, `magento2`, `shopware6`, `prestashop`, `shopify`, `opencart`, `oxid`,
  `shopware5`, `magento1`, `bigcommerce`, `generic`) and the
  behaviours: `trap` (order decoys on every page), `multistep`, `guest`
  (`available` | `wall_with_guest` | `none` | `none_closed`), `registration`
  (`ok` | `email_verification` | `captcha` | `sms` | `documents` | `payment`),
  `protection` (`cloudflare` | `403` | `429` | `captcha` | `geo` | `age`),
  `stop` (forced stop scenario), `payment_fields_required`, `variants`,
  `slow_seconds`. The browser reaches a shop as `http://<dir>.test/`.

The simulator counts every forbidden action (order submissions, newsletter
subscriptions, CAPTCHA solves, foreign logins, registrations while guest
checkout was available, card numbers sent to the tokenizer); AC-04 asserts
all of them stay at zero.
