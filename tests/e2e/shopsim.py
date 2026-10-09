"""Shop simulator for the checkout e2e tests (plan §4, AC-04/05/16).

Serves the shops described by `tests/fixtures/shops/*/shop.json` on one
local port; the browser reaches them as `http://<name>.test/` through the
test rewrite hook. Each shop has a *flavour* (woocommerce, magento2,
shopware6, prestashop, generic) that decides the markup and paths, and
*behaviours* (trap decoys, multi-step checkout, guest availability,
registration outcome, protection pages, forced stops, hosted payment
fields). The server keeps a session per cookie (cart, logged-in user), a
registry of accounts registered by the scanner, and counters of every
forbidden action: an order submission, a newsletter subscription, a CAPTCHA
solve, a login with foreign credentials, a registration when guest checkout
was available, a card number outside the reference list.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

SHOPS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "shops"
PROBE_LOCAL = "checkout-probe+"
ORDER_PATHS = (
    "/order/submit",
    "/?wc-ajax=checkout",
    "/checkout/order",
    "/pay",
    "/checkout/payment-information",
    "/order/confirm",
    "/place-order",
)


@dataclass
class ShopConfig:
    name: str
    flavour: str
    language: str = "de"
    trap: bool = False
    multistep: int = 0
    guest: str = "available"  # available | wall_with_guest | none | none_closed
    registration: str = "ok"  # ok | email_verification | captcha | sms | documents | payment
    protection: str = ""  # cloudflare | 403 | 429 | captcha | geo | age
    stop: str = ""  # forced stop scenario, see render()
    payment_fields_required: bool = False
    variants: bool = False
    methods: list[str] = field(default_factory=lambda: ["card", "paypal", "klarna"])
    tokenizer: str = "stripe"
    slow_seconds: float = 0.0

    @classmethod
    def load(cls, path: Path) -> ShopConfig:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return cls(name=path.parent.name, **doc)


@dataclass
class Counters:
    orders_placed: int = 0
    newsletters: int = 0
    captcha_solves: int = 0
    foreign_logins: int = 0
    needless_registrations: int = 0
    card_numbers: list[str] = field(default_factory=list)
    tokenizer_calls: int = 0
    registrations: list[str] = field(default_factory=list)
    logins: list[str] = field(default_factory=list)

    def forbidden(self) -> dict[str, int]:
        return {
            "orders_placed": self.orders_placed,
            "newsletters": self.newsletters,
            "captcha_solves": self.captcha_solves,
            "foreign_logins": self.foreign_logins,
            "needless_registrations": self.needless_registrations,
        }


@dataclass
class ShopState:
    config: ShopConfig
    counters: Counters = field(default_factory=Counters)
    accounts: dict[str, str] = field(default_factory=dict)  # email → password
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class SimServer:
    port: int
    shops: dict[str, ShopState]
    requests: list[tuple[str, str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def rewrite(self, url: str) -> str | None:
        u = urlsplit(url)
        if u.scheme not in {"http", "https"} or u.hostname in {"127.0.0.1", "localhost"}:
            return None
        if u.hostname and u.hostname.startswith("dead."):
            return "http://127.0.0.1:9/"  # nothing listens there: navigation error
        return f"http://127.0.0.1:{self.port}{u.path or '/'}" + (f"?{u.query}" if u.query else "")

    def url(self, shop: str, path: str = "/") -> str:
        return f"http://{shop}.test{path}"

    def state(self, shop: str) -> ShopState:
        return self.shops[shop]

    def reset(self, shop: str) -> None:
        st = self.shops[shop]
        st.counters = Counters()
        st.accounts.clear()
        st.sessions.clear()


def load_shops(directory: Path = SHOPS_DIR) -> dict[str, ShopState]:
    out: dict[str, ShopState] = {}
    for cfg in sorted(directory.glob("*/shop.json")):
        c = ShopConfig.load(cfg)
        out[c.name] = ShopState(c)
    return out


# --- markup ---------------------------------------------------------------------------
L = {
    "de": {
        "add": "In den Warenkorb",
        "cart": "Warenkorb",
        "checkout": "Zur Kasse",
        "continue": "Weiter",
        "final": "Zahlungspflichtig bestellen",
        "guest": "Als Gast bestellen",
        "login": "Anmelden",
        "register": "Konto erstellen",
        "terms": "Ich akzeptiere die AGB",
        "news": "Newsletter abonnieren",
        "ship": "Versandart",
        "pay": "Zahlungsart",
        "empty": "Ihr Warenkorb ist leer",
        "soldout": "Dieser Artikel ist leider ausverkauft.",
        "por": "Preis auf Anfrage",
        "minorder": "Mindestbestellwert 50 € nicht erreicht",
        "verify": "Bitte bestätigen Sie Ihre E-Mail-Adresse über den Bestätigungslink.",
        "sms": "Bitte geben Sie den SMS-Code ein, den wir an Ihre Telefonnummer gesendet haben.",
        "docs": "Bitte laden Sie Ihren Gewerbeschein hoch, bevor Sie bestellen können.",
        "loginwall": "Bitte melden Sie sich an, um fortzufahren.",
        "shop": "Alle Produkte",
    },
    "en": {
        "add": "Add to cart",
        "cart": "Cart",
        "checkout": "Proceed to checkout",
        "continue": "Continue",
        "final": "Place order",
        "guest": "Checkout as guest",
        "login": "Log in",
        "register": "Create account",
        "terms": "I accept the terms and conditions",
        "news": "Subscribe to our newsletter",
        "ship": "Shipping method",
        "pay": "Payment method",
        "empty": "Your cart is empty",
        "soldout": "This product is currently out of stock.",
        "por": "Price on request",
        "minorder": "Minimum order value of £50 not reached",
        "verify": "Please verify your email address using the confirmation link we sent.",
        "sms": "Enter the verification code we sent to your phone.",
        "docs": "Please upload your business license before ordering.",
        "loginwall": "Please log in to continue.",
        "shop": "All products",
    },
    "fr": {
        "add": "Ajouter au panier",
        "cart": "Panier",
        "checkout": "Commander",
        "continue": "Continuer",
        "final": "Valider la commande",
        "guest": "Commander en tant qu'invité",
        "login": "Se connecter",
        "register": "Créer un compte",
        "terms": "J'accepte les conditions générales",
        "news": "S'abonner à la newsletter",
        "ship": "Mode de livraison",
        "pay": "Mode de paiement",
        "empty": "Votre panier est vide",
        "soldout": "Ce produit est en rupture de stock.",
        "por": "Prix sur demande",
        "minorder": "Montant minimum de commande non atteint",
        "verify": "Veuillez confirmer votre adresse e-mail via le lien de confirmation.",
        "sms": "Saisissez le code de vérification envoyé par SMS.",
        "docs": "Veuillez télécharger votre pièce d'identité.",
        "loginwall": "Veuillez vous connecter pour continuer.",
        "shop": "Tous les produits",
    },
}


@dataclass
class Flavour:
    product_path: str  # format with {slug}
    product_link_class: str
    add_button: str
    cart_path: str
    cart_item_class: str
    checkout_path: str
    checkout_link_class: str
    fields: dict[str, str]  # kind → id/name attrs
    shipping_name: str
    step_button: str
    payment_block: str
    payment_radio_name: str
    final_button: str
    guest_control: str  # "" = tab/button text based
    form_action: str = ""  # where step forms post; defaults to checkout_path
    form_class: str = "checkout"  # class of the checkout form
    product_item_class: str = "product product-item product-box"  # listing item wrapper
    cart_item_attrs: str = ""  # attributes of a cart row ({i} = index); default class=…
    checkout_link_attrs: str = ""  # extra attributes on the cart → checkout link


FLAVOURS: dict[str, Flavour] = {
    "woocommerce": Flavour(
        form_class="checkout woocommerce-checkout",
        product_path="/product/{slug}/",
        product_link_class="woocommerce-LoopProduct-link",
        add_button='<button type="submit" name="add-to-cart" value="{pid}" class="single_add_to_cart_button button alt">{add}</button>',
        cart_path="/cart/",
        cart_item_class="cart_item",
        checkout_path="/checkout/",
        checkout_link_class="checkout-button button alt wc-forward",
        fields={
            "first": 'id="billing_first_name" name="billing_first_name"',
            "last": 'id="billing_last_name" name="billing_last_name"',
            "street": 'id="billing_address_1" name="billing_address_1"',
            "postcode": 'id="billing_postcode" name="billing_postcode"',
            "city": 'id="billing_city" name="billing_city"',
            "phone": 'id="billing_phone" name="billing_phone" type="tel"',
            "email": 'id="billing_email" name="billing_email" type="email"',
            "country": 'id="billing_country" name="billing_country"',
        },
        shipping_name="shipping_method[0]",
        step_button='<button type="submit" class="button" name="woocommerce_checkout_step">{cont}</button>',
        payment_block='<div id="payment" class="woocommerce-checkout-payment"><ul class="wc_payment_methods payment_methods methods">{methods}</ul>{final}</div>',
        payment_radio_name="payment_method",
        final_button='<button type="submit" class="button alt" name="woocommerce_checkout_place_order" id="place_order" value="{final}">{final}</button>',
        guest_control="",
    ),
    "magento2": Flavour(
        product_path="/{slug}.html",
        product_link_class="product-item-link",
        add_button='<button type="submit" title="{add}" class="action primary tocart" id="product-addtocart-button"><span>{add}</span></button>',
        cart_path="/checkout/cart/",
        cart_item_class="cart item",
        checkout_path="/checkout/",
        checkout_link_class="action primary checkout",
        fields={
            "first": 'name="firstname" id="firstname"',
            "last": 'name="lastname" id="lastname"',
            "street": 'name="street[0]" id="street_0"',
            "postcode": 'name="postcode" id="postcode"',
            "city": 'name="city" id="city"',
            "phone": 'name="telephone" id="telephone" type="tel"',
            "email": 'id="customer-email" name="username" type="email"',
            "country": 'name="country_id" id="country"',
        },
        shipping_name="ko_unique_1",
        step_button='<button type="submit" class="button action continue primary"><span>{cont}</span></button>',
        payment_block='<div id="checkout-payment-method-load"><div class="payment-methods">{methods}</div>{final}</div>',
        payment_radio_name="payment[method]",
        final_button='<button type="submit" class="action primary checkout" title="{final}"><span>{final}</span></button>',
        guest_control="",
    ),
    "shopware6": Flavour(
        product_path="/kleidung/{slug}",
        product_link_class="product-name",
        add_button='<button class="btn btn-primary btn-buy" title="{add}">{add}</button>',
        cart_path="/checkout/cart",
        cart_item_class="line-item line-item-product",
        checkout_link_class="btn btn-primary begin-checkout-btn",
        checkout_path="/checkout/confirm",
        fields={
            "first": 'id="personalFirstName" name="firstName"',
            "last": 'id="personalLastName" name="lastName"',
            "street": 'id="billingAddressAddressStreet" name="billingAddress[street]"',
            "postcode": 'id="billingAddressAddressZipcode" name="billingAddress[zipcode]"',
            "city": 'id="billingAddressAddressCity" name="billingAddress[city]"',
            "phone": 'id="billingAddressAddressPhoneNumber" name="billingAddress[phoneNumber]" type="tel"',
            "email": 'id="personalMail" name="email" type="email"',
            "country": 'id="billingAddressAddressCountry" name="billingAddress[countryId]"',
        },
        shipping_name="shippingMethodId",
        step_button='<div class="register-submit"><button type="submit" class="btn btn-primary btn-lg">{cont}</button></div>',
        payment_block='<div class="payment-methods checkout-confirm-payment">{methods}</div>{final}',
        payment_radio_name="paymentMethodId",
        final_button='<form id="confirmOrderForm" action="/checkout/order" method="post"><button type="submit" class="btn btn-primary btn-lg">{final}</button></form>',
        guest_control='<div class="form-check"><input type="checkbox" class="form-check-input" id="personalGuest" name="guest" value="1"><label class="form-check-label" for="personalGuest">Kein Kundenkonto erstellen</label></div>',
        form_action="/checkout/register",
    ),
    "prestashop": Flavour(
        product_path="/damen/{slug}.html",
        product_link_class="product-thumbnail",
        add_button='<button class="btn btn-primary add-to-cart" data-button-action="add-to-cart" type="submit">{add}</button>',
        cart_path="/cart?action=show",
        cart_item_class="cart-item",
        checkout_path="/order",
        checkout_link_class="btn btn-primary",
        fields={
            "first": 'name="firstname" id="field-firstname"',
            "last": 'name="lastname" id="field-lastname"',
            "street": 'name="address1" id="field-address1"',
            "postcode": 'name="postcode" id="field-postcode"',
            "city": 'name="city" id="field-city"',
            "phone": 'name="phone" id="field-phone" type="tel"',
            "email": 'name="email" id="field-email" type="email"',
            "country": 'name="id_country" id="field-id_country"',
        },
        shipping_name="delivery_option[1]",
        step_button='<button type="submit" class="continue btn btn-primary float-xs-right" name="continue" value="1">{cont}</button>',
        payment_block='<div class="payment-options">{methods}</div>{final}',
        payment_radio_name="payment-option",
        final_button='<div id="payment-confirmation"><button type="submit" class="btn btn-primary center-block">{final}</button></div>',
        guest_control="",
    ),
    "shopify": Flavour(
        product_path="/products/{slug}",
        product_link_class="full-unstyled-link",
        add_button='<button type="submit" name="add" class="product-form__submit button button--full-width button--primary"><span>{add}</span></button>',
        cart_path="/cart",
        cart_item_class="cart-item",
        checkout_path="/checkout",
        checkout_link_class="cart__checkout-button button",
        fields={
            "first": 'autocomplete="shipping given-name" name="firstName" id="TextField1"',
            "last": 'autocomplete="shipping family-name" name="lastName" id="TextField2"',
            "street": 'autocomplete="shipping address-line1" name="address1" id="TextField3"',
            "postcode": 'autocomplete="shipping postal-code" name="postalCode" id="TextField4"',
            "city": 'autocomplete="shipping address-level2" name="city" id="TextField5"',
            "phone": 'autocomplete="shipping tel" name="phone" id="TextField6" type="tel"',
            "email": 'autocomplete="shipping email" name="email" id="email" type="email"',
            "country": 'autocomplete="shipping country" name="countryCode" id="Select1"',
        },
        shipping_name="shippingLine",
        step_button='<button type="submit" class="button button--primary">{cont}</button>',
        payment_block='<div class="section section--payment-method" id="payment-gateway"><div class="content-box">{methods}</div>{final}</div>',
        payment_radio_name="paymentGateway",
        final_button='<button type="submit" class="button button--primary" id="checkout-pay-button">{final}</button>',
        guest_control="",
    ),
    "opencart": Flavour(
        product_path="/{slug}",
        product_link_class="",
        product_item_class="product-thumb",
        add_button='<button type="submit" id="button-cart" class="btn btn-primary btn-lg btn-block">{add}</button>',
        cart_path="/index.php?route=checkout/cart",
        cart_item_class="",
        checkout_path="/index.php?route=checkout/checkout",
        checkout_link_class="btn btn-primary",
        fields={
            "first": 'name="firstname" id="input-firstname"',
            "last": 'name="lastname" id="input-lastname"',
            "street": 'name="payment_address_1" id="input-payment-address-1"',
            "postcode": 'name="payment_postcode" id="input-payment-postcode"',
            "city": 'name="payment_city" id="input-payment-city"',
            "phone": 'name="telephone" id="input-telephone" type="tel"',
            "email": 'name="email" id="input-email" type="email"',
            "country": 'name="payment_country_id" id="input-payment-country"',
        },
        shipping_name="shipping_method",
        step_button='<button type="submit" id="button-shipping-method" class="btn btn-primary">{cont}</button>',
        payment_block='<div id="collapse-payment-method"><form id="form-payment-method">{methods}</form></div>{final}',
        payment_radio_name="payment_method",
        final_button='<form action="/order/submit" method="post"><button type="submit" id="button-confirm" class="btn btn-primary">{final}</button></form>',
        guest_control="",
    ),
    "oxid": Flavour(
        product_path="/Kleidung/{slug}.html",
        product_link_class="title",
        product_item_class="",
        add_button='<button id="toBasket" type="submit" class="btn btn-primary submitButton">{add}</button>',
        cart_path="/index.php?cl=basket",
        cart_item_class="",
        cart_item_attrs='id="list_cartItem_{i}"',
        checkout_path="/index.php?cl=user",
        checkout_link_class="btn btn-primary submitButton largeButton nextStep",
        fields={
            "first": 'id="invadr_oxuser__oxfname" name="invadr[oxuser__oxfname]" autocomplete="billing given-name"',
            "last": 'id="invadr_oxuser__oxlname" name="invadr[oxuser__oxlname]" autocomplete="billing family-name"',
            "street": 'id="invadr_oxuser__oxstreet" name="invadr[oxuser__oxstreet]" autocomplete="billing street-address"',
            "postcode": 'id="invadr_oxuser__oxzip" name="invadr[oxuser__oxzip]" autocomplete="billing postal-code"',
            "city": 'id="invadr_oxuser__oxcity" name="invadr[oxuser__oxcity]" autocomplete="billing locality"',
            "phone": 'id="invadr_oxuser__oxfon" name="invadr[oxuser__oxfon]" type="tel"',
            "email": 'id="userLoginName" name="lgn_usr" type="email"',
            "country": 'id="invCountrySelect" name="invadr[oxuser__oxcountryid]"',
        },
        shipping_name="sShipSet",
        step_button='<button type="submit" class="btn btn-primary submitButton nextStep">{cont}</button>',
        payment_block='<form id="payment" name="order"><dl>{methods}</dl></form>{final}',
        payment_radio_name="paymentid",
        final_button='<form action="/order/submit" method="post"><button type="submit" id="orderConfirmAgbBottom" class="btn btn-primary submitButton largeButton">{final}</button></form>',
        guest_control="",
    ),
    "shopware5": Flavour(
        product_path="/kleidung/10/{slug}",
        product_link_class="product--title",
        product_item_class="product--box box--basic",
        add_button='<button class="buybox--button block btn is--primary is--icon-right is--center is--large" name="In den Warenkorb">{add}</button>',
        cart_path="/checkout/cart",
        cart_item_class="table--tr block-group row--product",
        checkout_path="/checkout/confirm",
        checkout_link_class="btn btn--checkout-proceed is--primary right is--icon-right is--large",
        fields={
            "first": 'id="firstname" name="register[personal][firstname]" autocomplete="section-personal given-name"',
            "last": 'id="lastname" name="register[personal][lastname]" autocomplete="section-personal family-name"',
            "street": 'id="street" name="register[billing][street]" autocomplete="section-billing billing street-address"',
            "postcode": 'id="zipcode" name="register[billing][zipcode]" autocomplete="section-billing billing postal-code"',
            "city": 'id="city" name="register[billing][city]" autocomplete="section-billing billing address-level2"',
            "phone": 'id="phone" name="register[personal][phone]" type="tel"',
            "email": 'id="register_personal_email" name="register[personal][email]" type="email"',
            "country": 'id="country" name="register[billing][country]" class="select--country"',
        },
        shipping_name="sDispatch",
        step_button='<button type="submit" class="btn is--primary is--large right is--icon-right">{cont}</button>',
        payment_block='<div class="payment--method-list panel has--border is--rounded block"><div class="panel--body is--wide block-group">{methods}</div></div>{final}',
        payment_radio_name="payment",
        final_button='<form id="confirm--form" action="/order/submit" method="post"><button type="submit" class="btn is--primary is--large right is--icon-right">{final}</button></form>',
        guest_control='<div class="register--check"><input type="checkbox" value="1" id="register_personal_skipLogin" name="register[personal][accountmode]" class="register--checkbox chkbox"><label for="register_personal_skipLogin" class="chklabel is--bold">Kein Kundenkonto erstellen</label></div>',
        form_action="/register",
        form_class="register--form",
    ),
    "magento1": Flavour(
        product_path="/{slug}-m1.html",
        product_link_class="product-image",
        product_item_class="item",
        add_button='<button type="button" title="{add}" class="button btn-cart" id="product-addtocart-button" onclick="this.form.submit()"><span><span>{add}</span></span></button>',
        cart_path="/checkout/cart/",
        cart_item_class="",
        checkout_path="/checkout/onepage/",
        checkout_link_class="button btn-proceed-checkout btn-checkout",
        fields={
            "first": 'id="billing:firstname" name="billing[firstname]"',
            "last": 'id="billing:lastname" name="billing[lastname]"',
            "street": 'id="billing:street1" name="billing[street][]"',
            "postcode": 'id="billing:postcode" name="billing[postcode]"',
            "city": 'id="billing:city" name="billing[city]"',
            "phone": 'id="billing:telephone" name="billing[telephone]" type="tel"',
            "email": 'id="billing:email" name="billing[email]" type="email"',
            "country": 'id="billing:country_id" name="billing[country_id]"',
        },
        shipping_name="shipping_method",
        step_button='<button type="submit" class="button" title="{cont}"><span><span>{cont}</span></span></button>',
        payment_block='<div id="checkout-step-payment" class="step"><dl id="payment-methods">{methods}</dl></div>{final}',
        payment_radio_name="payment[method]",
        final_button='<form action="/order/submit" method="post"><button type="submit" class="button btn-checkout" title="{final}"><span><span>{final}</span></span></button></form>',
        guest_control="",
    ),
    "bigcommerce": Flavour(
        product_path="/{slug}-bc/",
        product_link_class="card-figure__link",
        product_item_class="card",
        add_button='<input id="form-action-addToCart" class="button button--primary" type="submit" value="{add}">',
        cart_path="/cart.php",
        cart_item_class="",
        cart_item_attrs='class="cart-item" data-item-row',
        checkout_path="/checkout",
        checkout_link_class="button button--primary",
        checkout_link_attrs="data-primary-checkout-now-action",
        fields={
            "first": 'autocomplete="given-name" name="firstName" id="firstNameInput"',
            "last": 'autocomplete="family-name" name="lastName" id="lastNameInput"',
            "street": 'autocomplete="address-line1" name="address1" id="address1Input"',
            "postcode": 'autocomplete="postal-code" name="postalCode" id="postCodeInput"',
            "city": 'autocomplete="address-level2" name="city" id="cityInput"',
            "phone": 'autocomplete="tel" name="phone" id="phoneInput" type="tel"',
            "email": 'autocomplete="email" name="email" id="email" type="email"',
            "country": 'autocomplete="country" name="countryCode" id="countryCodeInput"',
        },
        shipping_name="shippingOptionIds",
        step_button='<button type="submit" id="checkout-shipping-continue" class="button button--primary">{cont}</button>',
        payment_block='<div id="checkout-payment" class="checkout-form"><div class="form-checklist">{methods}</div></div>{final}',
        payment_radio_name="paymentProviderRadio",
        final_button='<form action="/order/submit" method="post"><button type="submit" id="checkout-payment-continue" class="button button--primary">{final}</button></form>',
        guest_control="",
    ),
    "generic": Flavour(
        product_path="/item/{slug}",
        product_link_class="tile-link",
        add_button='<button type="submit" class="primary">{add}</button>',
        cart_path="/basket",
        cart_item_class="basket-row",
        checkout_path="/checkout",
        checkout_link_class="go",
        fields={
            "first": 'autocomplete="given-name" name="f1"',
            "last": 'autocomplete="family-name" name="f2"',
            "street": 'autocomplete="address-line1" name="f3"',
            "postcode": 'autocomplete="postal-code" name="f4"',
            "city": 'autocomplete="address-level2" name="f5"',
            "phone": 'autocomplete="tel" name="f6" type="tel"',
            "email": 'autocomplete="email" name="f7" type="email"',
            "country": 'autocomplete="country" name="f8"',
        },
        shipping_name="delivery",
        step_button='<button type="submit">{cont}</button>',
        payment_block='<section id="zahlung"><div class="methods">{methods}</div>{final}</section>',
        payment_radio_name="paymethod",
        final_button='<button type="submit" class="primary">{final}</button>',
        guest_control="",
    ),
}

PRODUCTS = [("Testprodukt Alpha", "alpha", "29,90 €"), ("Testprodukt Beta", "beta", "49,00 €")]


def _is(target: str, path: str, query: dict[str, list[str]]) -> bool:
    """`path`+`query` address `target` (a path, optionally with a query such as `?cl=basket`)."""
    t = urlsplit(target)
    want = "/" + t.path.lstrip("/")
    if path != want:
        return False
    return all(query.get(k) == v for k, v in parse_qs(t.query).items())


def _trap_decoys(t: dict[str, str], flavour: str) -> str:
    """Decoys that look like navigation but would place an order (FR-CW-04, AC-04)."""
    return f"""
<div class="decoys">
  <form action="/order/submit" method="post" id="confirmOrderForm"><button type="submit">{t["continue"]}</button></form>
  <form action="/checkout/order" method="post"><button type="button" name="place_order" onclick="this.form.submit()">{t["continue"]}</button></form>
  <button type="button" class="btn btn-pay" onclick="fetch('/pay', {{method: 'POST'}})"></button>
  <a href="/order/submit" class="btn" data-action="place-order">→</a>
  <button type="button" aria-label="{t["final"]}" onclick="fetch('/order/submit', {{method: 'POST'}})"><svg width="12" height="12"></svg></button>
  <form action="/?wc-ajax=checkout" method="post"><input type="submit" value="{t["continue"]}"></form>
  <button type="button" onclick="document.querySelector('#confirmOrderForm').requestSubmit()">{t["continue"]} »</button>
  <button type="button" style="position:absolute;left:-9999px" onclick="fetch('/place-order', {{method:'POST'}})">{t["final"]}</button>
  <form action="/newsletter" method="post"><label><input type="checkbox" name="newsletter" value="1"> {t["news"]}</label><button type="submit">OK</button></form>
</div>"""


def _layout(title: str, body: str, t: dict[str, str], cfg: ShopConfig, fl: Flavour) -> str:
    decoys = _trap_decoys(t, cfg.flavour) if cfg.trap else ""
    return f"""<!doctype html><html lang="{cfg.language}"><head><meta charset="utf-8"><title>{title}</title>
<link rel="icon" href="/favicon.ico"><script src="/assets/theme.js"></script></head>
<body><header><a href="/">{cfg.name}</a> · <a href="{fl.cart_path}" class="cart-contents">{t["cart"]}</a></header>
{decoys}
<main>{body}</main>
{decoys}
<footer><a href="/impressum">Impressum</a></footer></body></html>"""


def _methods_html(cfg: ShopConfig, fl: Flavour) -> str:
    labels = {
        "card": ("Kreditkarte (Visa, Mastercard)", "/img/visa.svg", "Visa"),
        "paypal": ("PayPal", "/img/paypal-logo.png", "PayPal"),
        "klarna": ("Rechnung mit Klarna", "/img/klarna.svg", "Klarna"),
        "sepa": ("SEPA-Lastschrift", "/img/sepa.svg", "SEPA"),
        "sofort": ("Sofortüberweisung", "/img/sofort.svg", "Sofort"),
    }
    out = []
    for i, m in enumerate(cfg.methods):
        text, img, alt = labels.get(m, (m, "", m))
        logo = f'<img src="{img}" alt="{alt}">' if img else ""
        out.append(
            f'<li class="wc_payment_method payment_method_{m}"><input type="radio" '
            f'name="{fl.payment_radio_name}" id="pm_{m}" value="{m}" {"checked" if i == 0 else ""}>'
            f'<label for="pm_{m}">{text}</label>{logo}</li>'
        )
    return "\n".join(out)


def _hosted_fields(cfg: ShopConfig) -> str:
    """A tokenizer card form (Stripe-like iframe) that reveals methods only after typing."""
    return """
<div id="card-element" class="StripeElement">
  <iframe name="__privateStripeFrame1" src="/frames/number" title="Secure card number input frame"></iframe>
  <iframe name="__privateStripeFrame2" src="/frames/exp" title="Secure expiration date input frame"></iframe>
  <iframe name="__privateStripeFrame3" src="/frames/cvc" title="Secure CVC input frame"></iframe>
</div>
<ul class="revealed-methods"></ul>
<script>
window.addEventListener("message", (ev) => {
  if (!ev.data || ev.data.type !== "payintel-sim-token") return;
  const ul = document.querySelector(".revealed-methods");
  ul.innerHTML = '<li><input type="radio" name="payment_method" value="card" checked><label>Kreditkarte (Visa, Mastercard)</label><img src="/img/visa.svg" alt="Visa"></li>'
    + '<li><input type="radio" name="payment_method" value="paypal"><label>PayPal</label></li>';
});
</script>"""


FRAME_NUMBER = """<!doctype html><html><body><input name="cardnumber" autocomplete="cc-number" placeholder="1234 1234 1234 1234">
<script>
document.querySelector("input").addEventListener("input", (e) => {
  const v = e.target.value.replace(/\\s+/g, "");
  if (v.length >= 16) {
    fetch("http://js.tokenizer.test/v1/tokens", {method: "POST", mode: "no-cors", body: "card[number]=" + v})
      .finally(() => parent.postMessage({type: "payintel-sim-token"}, "*"));
  }
});
</script></body></html>"""
FRAME_EXP = '<!doctype html><html><body><input name="exp-date" autocomplete="cc-exp" placeholder="MM / YY"></body></html>'
FRAME_CVC = '<!doctype html><html><body><input name="cvc" autocomplete="cc-csc" placeholder="CVC"></body></html>'

PROTECTION_PAGES = {
    "cloudflare": (
        503,
        '<!doctype html><html><head><title>Just a moment...</title></head><body><div id="cf-challenge-running"></div>'
        '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script><p>Checking your browser before accessing the site.</p></body></html>',
    ),
    "403": (
        403,
        "<!doctype html><html><head><title>Access Denied</title></head><body><h1>Access Denied</h1><p>errors.edgesuite.net reference #18.1</p></body></html>",
    ),
    "429": (
        429,
        "<!doctype html><html><head><title>Too Many Requests</title></head><body><h1>429</h1></body></html>",
    ),
    "captcha": (
        200,
        '<!doctype html><html><head><title>Verify you are human</title></head><body><form action="/captcha/verify" method="post">'
        '<div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001"></div><button type="submit">Verify</button></form></body></html>',
    ),
    "geo": (
        200,
        "<!doctype html><html><head><title>Shop</title></head><body><p>Sorry, this shop is not available in your country.</p></body></html>",
    ),
    "age": (
        200,
        '<!doctype html><html><head><title>Age verification</title></head><body><h1>Age verification</h1><p>Are you over 18?</p><button type="button">Yes</button></body></html>',
    ),
}


class Sim:
    """Request → (status, headers, body). Pure functions of (config, state, session)."""

    def __init__(self, server: SimServer) -> None:
        self.server = server

    # --- helpers
    def _session(self, st: ShopState, sid: str | None) -> tuple[str, dict[str, Any]]:
        if sid and sid in st.sessions:
            return sid, st.sessions[sid]
        sid = secrets.token_hex(8)
        st.sessions[sid] = {"cart": [], "user": None, "guest": False, "step": 0}
        return sid, st.sessions[sid]

    def handle(
        self,
        shop: str,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, list[str]],
        sid: str | None,
        origin: str = "",
    ) -> tuple[int, dict[str, str], str, str]:
        st = self.server.shops.get(shop)
        if st is None and path == "/v1/tokens":  # the tokenizer host: attribute by Origin
            origin_host = shop_of(urlsplit(origin).hostname or "", self.server.shops)
            st = self.server.shops.get(origin_host)
            if st is None:
                return 200, {"Content-Type": "application/json"}, '{"id":"tok_test"}', ""
            shop = origin_host
        if st is None and path == "/v3/":  # the PSP script host (rewritten to this server)
            return (
                200,
                {"Content-Type": "application/javascript"},
                "window.Stripe = function(){};",
                "",
            )
        if st is None:
            return 404, {}, "no such shop", ""
        cfg, fl, t = st.config, FLAVOURS[st.config.flavour], L[st.config.language]
        sid, sess = self._session(st, sid)
        c = st.counters
        if cfg.slow_seconds and path.startswith(fl.checkout_path.rstrip("/") or "/checkout"):
            time.sleep(cfg.slow_seconds)
        with self.server.lock:
            self.server.requests.append((method, shop, path))
        # --- forbidden endpoints -------------------------------------------------------
        if method == "POST" and (
            any(path == p.split("?")[0] for p in ORDER_PATHS)
            or query.get("wc-ajax") == ["checkout"]
        ):
            c.orders_placed += 1
            return 200, {}, "<html><body>Thank you for your order</body></html>", sid
        if path == "/newsletter" and method == "POST":
            c.newsletters += 1
            return 200, {}, "<html><body>subscribed</body></html>", sid
        if path == "/captcha/verify" and method == "POST":
            c.captcha_solves += 1
            return 200, {}, "<html><body>ok</body></html>", sid
        if path == "/v1/tokens":
            c.tokenizer_calls += 1
            for key in ("card[number]", "number", "cardnumber"):
                for v in body.get(key, []):
                    c.card_numbers.append(v)
            return 200, {"Content-Type": "application/json"}, '{"id":"tok_test"}', sid
        # --- static ---------------------------------------------------------------------
        if path == "/favicon.ico":
            return 200, {"Content-Type": "image/x-icon"}, "ICON", sid
        if path.startswith("/assets/"):
            return 200, {"Content-Type": "application/javascript"}, "window.theme = 1;", sid
        if path.startswith("/img/"):
            return (
                200,
                {"Content-Type": "image/svg+xml"},
                "<svg xmlns='http://www.w3.org/2000/svg'/>",
                sid,
            )
        if path == "/frames/number":
            return 200, {}, FRAME_NUMBER, sid
        if path == "/frames/exp":
            return 200, {}, FRAME_EXP, sid
        if path == "/frames/cvc":
            return 200, {}, FRAME_CVC, sid
        if path == "/robots.txt":
            return 200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /admin/\n", sid
        # --- protection -------------------------------------------------------------------
        if cfg.protection:
            status, html = PROTECTION_PAGES[cfg.protection]
            return status, {}, html, sid
        if cfg.stop == "http_error":
            return 500, {}, "<html><body>Internal Server Error</body></html>", sid
        # --- pages ------------------------------------------------------------------------
        if path == "/":
            return 200, {}, self.home(cfg, fl, t), sid
        if self._is_product(cfg, fl, path):
            return 200, {}, self.product(cfg, fl, t, path), sid
        if path == "/cart/add" and method == "POST":
            if cfg.stop == "add_to_cart_failed":
                return 200, {}, _layout("Error", "<p>Could not add the item.</p>", t, cfg, fl), sid
            if cfg.stop != "cart_empty":
                sess["cart"].append(body.get("pid", ["alpha"])[0])
            return 302, {"Location": fl.cart_path}, "", sid
        if _is(fl.cart_path, path, query):
            return 200, {}, self.cart(cfg, fl, t, sess), sid
        if (
            _is(fl.checkout_path, path, query)
            or (fl.form_action and _is(fl.form_action, path, query))
            or path in {"/checkout/register", "/checkout/"}
        ):
            return self._checkout(cfg, fl, t, st, sess, sid, method, body, query)
        if path == "/login" and method == "POST":
            return self._login(cfg, fl, t, st, sess, sid, body)
        if path == "/login":
            return 200, {}, self.login_wall(cfg, fl, t, error=False), sid
        if path == "/register" and method == "GET":
            return 200, {}, self.register_form(cfg, fl, t), sid
        if path == "/register" and method == "POST":
            return self._register(cfg, fl, t, st, sess, sid, body)
        if path == "/impressum":
            return 200, {}, _layout("Impressum", "<p>Testshop GmbH</p>", t, cfg, fl), sid
        return 404, {}, _layout("404", "<p>not found</p>", t, cfg, fl), sid

    # --- page renderers ------------------------------------------------------------------
    def _is_product(self, cfg: ShopConfig, fl: Flavour, path: str) -> bool:
        return any(path == fl.product_path.format(slug=slug) for _, slug, _ in PRODUCTS)

    def home(self, cfg: ShopConfig, fl: Flavour, t: dict[str, str]) -> str:
        if cfg.stop == "no_product":
            body = f"<h1>{cfg.name}</h1><p>Welcome. <a href='/impressum'>About us</a></p>"
            return _layout(cfg.name, body, t, cfg, fl)
        if cfg.stop == "navigation_error":
            body = f'<h1>{cfg.name}</h1><ul class="products"><li class="product"><a class="{fl.product_link_class}" href="http://dead.{cfg.name}.test/product/alpha/">Alpha</a></li></ul>'
            return _layout(cfg.name, body, t, cfg, fl)
        items = "".join(
            f'<li class="{fl.product_item_class}"><a class="{fl.product_link_class}" href="{fl.product_path.format(slug=slug)}">{name}</a> <span class="price">{price}</span></li>'
            for name, slug, price in PRODUCTS
        )
        body = f'<h1>{t["shop"]}</h1><ul class="products">{items}</ul>'
        return _layout(cfg.name, body, t, cfg, fl)

    def product(self, cfg: ShopConfig, fl: Flavour, t: dict[str, str], path: str) -> str:
        name, slug, price = next(p for p in PRODUCTS if fl.product_path.format(slug=p[1]) == path)
        if cfg.stop == "out_of_stock":
            return _layout(
                name, f"<h1>{name}</h1><p class='stock out-of-stock'>{t['soldout']}</p>", t, cfg, fl
            )
        if cfg.stop == "price_on_request":
            return _layout(
                name,
                f"<h1>{name}</h1><p class='price'>{t['por']}</p><a href='/contact'>Anfrage</a>",
                t,
                cfg,
                fl,
            )
        variants = ""
        if cfg.variants:
            variants = (
                '<table class="variations"><tr><td><select name="attribute_pa_size" id="pa_size" required>'
                '<option value="">Größe wählen…</option><option value="m">M</option><option value="l">L</option></select></td></tr></table>'
            )
        add = fl.add_button.format(add=t["add"], pid=slug)
        body = f"""<h1>{name}</h1><p class="price">{price}</p>
<form class="cart buy-widget" id="add-to-cart-or-refresh" method="post" action="/cart/add">{variants}
<input type="hidden" name="pid" value="{slug}"><input type="number" name="quantity" value="1" min="1">{add}</form>"""
        return _layout(name, body, t, cfg, fl)

    def cart(self, cfg: ShopConfig, fl: Flavour, t: dict[str, str], sess: dict[str, Any]) -> str:
        if not sess["cart"]:
            return _layout(
                t["cart"], f"<h1>{t['cart']}</h1><p class='cart-empty'>{t['empty']}</p>", t, cfg, fl
            )
        rows = "".join(
            f'<tr {fl.cart_item_attrs.format(i=i) or f"class={chr(34)}{fl.cart_item_class}{chr(34)}"}><td>{slug}</td><td><input type="number" name="cart[{i}][qty]" value="1"></td></tr>'
            for i, slug in enumerate(sess["cart"])
        )
        if cfg.stop == "min_order":
            return _layout(
                t["cart"],
                f"<h1>{t['cart']}</h1><table>{rows}</table><p class='notice'>{t['minorder']}</p>",
                t,
                cfg,
                fl,
            )
        link = (
            ""
            if cfg.stop == "checkout_not_found"
            else f'<a href="{fl.checkout_path}" class="{fl.checkout_link_class}" {fl.checkout_link_attrs}>{t["checkout"]}</a>'
        )
        body = f'<h1>{t["cart"]}</h1><form class="woocommerce-cart-form" action="{fl.cart_path}" method="post"><table id="shopping-cart-table">{rows}</table></form>{link}'
        return _layout(t["cart"], body, t, cfg, fl)

    def login_wall(self, cfg: ShopConfig, fl: Flavour, t: dict[str, str], *, error: bool) -> str:
        guest = ""
        if cfg.guest == "wall_with_guest":
            guest = (
                f'<a href="{fl.checkout_path}?guest=1" class="btn guest-checkout">{t["guest"]}</a>'
            )
        register = (
            ""
            if cfg.guest == "none_closed"
            else f'<a href="/register" class="btn register-link">{t["register"]}</a>'
        )
        err = '<p class="error">Login failed.</p>' if error else ""
        body = f"""<h1>{t["login"]}</h1><p class="login-wall">{t["loginwall"]}</p>{err}
<form class="login-form form-login" id="login-form" action="/login" method="post">
<input type="email" name="email" id="loginMail" required placeholder="E-Mail">
<input type="password" name="password" id="loginPassword" required placeholder="Passwort">
<label><input type="checkbox" name="remember" value="1"> Angemeldet bleiben</label>
<button type="submit" class="btn btn-primary">{t["login"]}</button></form>
{guest} {register}"""
        return _layout(t["login"], body, t, cfg, fl)

    def register_form(self, cfg: ShopConfig, fl: Flavour, t: dict[str, str]) -> str:
        extra = ""
        if cfg.registration == "captcha":
            extra = (
                '<div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001"></div>'
            )
        elif cfg.registration == "documents":
            extra = f'<p class="hint">{t["docs"]}</p><input type="file" name="license" required>'
        elif cfg.registration == "payment":
            extra = '<fieldset><legend>Zahlungsdaten</legend><input autocomplete="cc-number" name="ccnum" required></fieldset>'
        elif cfg.registration == "sms":
            extra = f'<p class="hint">{t["sms"]}</p>'
        f = fl.fields
        body = f"""<h1>{t["register"]}</h1>
<form class="register-form form-create-account" id="register-form" action="/register" method="post">
<input {f["first"]} required placeholder="Vorname"><input {f["last"]} required placeholder="Nachname">
<input {f["email"]} required placeholder="E-Mail">
<input type="password" name="password" id="personalPassword" autocomplete="new-password" required>
<input type="password" name="password_confirmation" id="personalPasswordConfirmation" required>
<input {f["street"]} required><input {f["postcode"]} required><input {f["city"]} required>
{extra}
<label><input type="checkbox" name="newsletter" value="1"> {t["news"]}</label>
<label><input type="checkbox" name="tos" id="tos" value="1" required> {t["terms"]}</label>
<button type="submit" class="btn btn-primary register-submit">{t["register"]}</button></form>"""
        return _layout(t["register"], body, t, cfg, fl)

    def _login(
        self,
        cfg: ShopConfig,
        fl: Flavour,
        t: dict[str, str],
        st: ShopState,
        sess: dict[str, Any],
        sid: str,
        body: dict[str, list[str]],
    ) -> tuple[int, dict[str, str], str, str]:
        email = body.get("email", [""])[0]
        password = body.get("password", [""])[0]
        st.counters.logins.append(email)
        if email not in st.accounts or not email.split("@")[0].startswith(PROBE_LOCAL):
            st.counters.foreign_logins += 1
            return 200, {}, self.login_wall(cfg, fl, t, error=True), sid
        if st.accounts[email] != password or cfg.stop == "login_failed":
            return 200, {}, self.login_wall(cfg, fl, t, error=True), sid
        sess["user"] = email
        return 302, {"Location": fl.checkout_path}, "", sid

    def _register(
        self,
        cfg: ShopConfig,
        fl: Flavour,
        t: dict[str, str],
        st: ShopState,
        sess: dict[str, Any],
        sid: str,
        body: dict[str, list[str]],
    ) -> tuple[int, dict[str, str], str, str]:
        email = body.get("email", body.get("username", body.get("f7", [""])))[0]
        password = body.get("password", [""])[0]
        if cfg.guest in {"available", "wall_with_guest"}:
            st.counters.needless_registrations += 1
        if body.get("newsletter"):
            st.counters.newsletters += 1
        st.counters.registrations.append(email)
        if cfg.registration == "captcha":
            st.counters.captcha_solves += 1
        if cfg.registration == "email_verification":
            st.accounts[email] = password
            return (
                200,
                {},
                _layout(
                    "Verify",
                    f"<h1>{t['register']}</h1><p class='notice'>{t['verify']}</p>",
                    t,
                    cfg,
                    fl,
                ),
                sid,
            )
        if cfg.registration == "sms":
            return 200, {}, _layout("SMS", f"<p>{t['sms']}</p><input name='code'>", t, cfg, fl), sid
        st.accounts[email] = password
        sess["user"] = email
        return 302, {"Location": fl.checkout_path}, "", sid

    def _checkout(
        self,
        cfg: ShopConfig,
        fl: Flavour,
        t: dict[str, str],
        st: ShopState,
        sess: dict[str, Any],
        sid: str,
        method: str,
        body: dict[str, list[str]],
        query: dict[str, list[str]],
    ) -> tuple[int, dict[str, str], str, str]:
        if not sess["cart"]:
            return 302, {"Location": fl.cart_path}, "", sid
        if query.get("guest") == ["1"] or body.get("guest"):
            sess["guest"] = True
        logged_in = sess["user"] is not None or sess["guest"]
        if cfg.guest in {"wall_with_guest", "none", "none_closed"} and not logged_in:
            return 200, {}, self.login_wall(cfg, fl, t, error=False), sid
        if method == "POST":
            if cfg.stop == "address_validation" and sess["step"] == 0:
                return (
                    200,
                    {},
                    self.checkout_step(
                        cfg, fl, t, sess, error="Die Adresse konnte nicht validiert werden."
                    ),
                    sid,
                )
            sess["step"] += 1
        return 200, {}, self.checkout_step(cfg, fl, t, sess), sid

    def checkout_step(
        self, cfg: ShopConfig, fl: Flavour, t: dict[str, str], sess: dict[str, Any], error: str = ""
    ) -> str:
        f = fl.fields
        step = sess["step"]
        total = max(cfg.multistep, 0)
        if cfg.stop == "payment_step_not_detected":
            total = 99
        err = f'<p class="error">{error}</p>' if error else ""
        guest_box = fl.guest_control if cfg.guest == "available" and fl.guest_control else ""
        unmapped = (
            '<input type="text" name="xyz_code" id="xyz_code" required placeholder="Kundennummer">'
            if cfg.stop == "required_field_unmapped"
            else ""
        )
        address = f"""<fieldset class="address"><legend>Adresse</legend>{guest_box}
<select {f["country"]} required><option value="">Land wählen</option><option value="DE">Deutschland</option><option value="GB">United Kingdom</option><option value="FR">France</option></select>
<input {f["first"]} required><input {f["last"]} required><input {f["email"]} required>
<input {f["street"]} required><input {f["postcode"]} required><input {f["city"]} required><input {f["phone"]}>
{unmapped}
<label><input type="checkbox" name="newsletter" id="newsletter" value="1"> {t["news"]}</label>
<label><input type="checkbox" name="terms" id="terms" value="1" required> {t["terms"]}</label></fieldset>"""
        if cfg.stop == "no_shipping_option":
            shipping = f'<fieldset class="shipping"><legend>{t["ship"]}</legend><p>Keine Versandart für diese Adresse verfügbar.</p></fieldset>'
        else:
            shipping = f"""<fieldset class="shipping shipping-method"><legend>{t["ship"]}</legend>
<label><input type="radio" name="{fl.shipping_name}" value="std"> Standard (4,90 €)</label>
<label><input type="radio" name="{fl.shipping_name}" value="exp"> Express (9,90 €)</label></fieldset>"""
        if cfg.payment_fields_required:
            methods_html = _hosted_fields(cfg)
        else:
            methods_html = _methods_html(cfg, fl)
        payment = fl.payment_block.format(
            methods=methods_html, final=fl.final_button.format(final=t["final"])
        )
        if cfg.tokenizer == "stripe":
            payment += '<script src="https://js.stripe.com/v3/"></script>'
        payment = f'<fieldset class="payment"><legend>{t["pay"]}</legend>{payment}</fieldset>'
        cont = fl.step_button.format(cont=t["continue"])
        action = fl.form_action or fl.checkout_path
        if total == 0:
            inner = address + shipping + payment
            form_open = (
                f'<form name="checkout" class="{fl.form_class}" action="{action}" method="post">'
            )
            body = f"<h1>{t['checkout']}</h1>{err}{form_open}{inner}</form>"
        elif step == 0:
            body = f"<h1>{t['checkout']} 1/{total + 1}</h1>{err}<form action=\"{action}\" method=\"post\" class=\"form-address\">{address}{cont}</form>"
        elif step < total:
            if cfg.stop == "no_shipping_option":
                body = f"<h1>{t['checkout']} {step + 1}/{total + 1}</h1><form action=\"{action}\" method=\"post\">{shipping}</form>"
            else:
                body = f"<h1>{t['checkout']} {step + 1}/{total + 1}</h1><form action=\"{action}\" method=\"post\">{shipping}{cont}</form>"
        else:
            body = f"<h1>{t['checkout']} {total + 1}/{total + 1}</h1><form action=\"/order/submit\" method=\"post\" id=\"payment-form\">{payment}</form>"
        return _layout(t["checkout"], body, t, cfg, fl)


def _handler(server: SimServer, sim: Sim) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: object) -> None:
            return

        def _serve(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            host = self.headers.get("x-forwarded-host", self.headers.get("host", "")).split(":")[0]
            shop = shop_of(host, server.shops)
            u = urlsplit(self.path)
            query = parse_qs(u.query, keep_blank_values=True)
            body = parse_qs(raw, keep_blank_values=True)
            cookie = SimpleCookie(self.headers.get("cookie", ""))
            sid = cookie["sid"].value if "sid" in cookie else None
            origin = self.headers.get("origin") or self.headers.get("referer") or ""
            status, headers, html, new_sid = sim.handle(
                shop, self.command, u.path, query, body, sid, origin
            )
            data = html.encode("utf-8")
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            if "Content-Type" not in headers:
                self.send_header("Content-Type", "text/html; charset=utf-8")
            if new_sid and new_sid != sid:
                self.send_header("Set-Cookie", f"sid={new_sid}; Path=/")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _serve
        do_POST = _serve

    return Handler


def shop_of(host: str, shops: dict[str, ShopState]) -> str:
    """`<shop>.test` or any `<shop>.<real tld>` (the worker tests need a PSL-valid name)."""
    if host.endswith(".test"):
        return host[: -len(".test")]
    first = host.split(".")[0]
    return first if first in shops else host


def start_server(
    shops: dict[str, ShopState] | None = None,
) -> tuple[SimServer, ThreadingHTTPServer]:
    server = SimServer(port=0, shops=shops if shops is not None else load_shops())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(server, Sim(server)))
    server.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return server, httpd
