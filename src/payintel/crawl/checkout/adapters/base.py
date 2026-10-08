"""Platform adapters (FR-CW-03): selectors a walk tries first, before the heuristic.

An adapter is data: the CSS selectors of a platform's product links, add-to-
cart button, cart and checkout paths, guest option, address fields, shipping
and payment blocks. The walker (`walker.py`) tries the adapter's selectors
first and the heuristic ones (`HEURISTIC`, built from `autocomplete`
attributes, common field names and the 10-language dictionary) after them,
so an adapter only has to describe what is specific to its platform. Every
click still goes through `GuardedPage` and the click policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from payintel.crawl.checkout.guardrails.page import BuyerField

Selectors = tuple[str, ...]


@dataclass(frozen=True)
class AdapterHints:
    name: str
    platform_ids: tuple[str, ...] = ()
    product_links: Selectors = ()
    product_url_patterns: tuple[str, ...] = ()
    variant_selects: Selectors = ()
    variant_options: Selectors = ()  # clickable swatches / radio labels
    add_to_cart: Selectors = ()
    cart_path: str = ""
    cart_links: Selectors = ()
    cart_items: Selectors = ()
    checkout_path: str = ""
    checkout_links: Selectors = ()
    guest_controls: Selectors = ()  # checkbox / radio / button / link / tab
    login_form: Selectors = ()
    register_controls: Selectors = ()
    register_form: Selectors = ()
    fields: dict[BuyerField, Selectors] = field(default_factory=dict)
    country_selects: Selectors = ()
    region_selects: Selectors = ()
    salutation_selects: Selectors = ()
    required_checkboxes: Selectors = ()
    shipping_options: Selectors = ()
    step_buttons: Selectors = ()
    payment_blocks: Selectors = ()
    payment_options: Selectors = ()
    login_fields: dict[str, Selectors] = field(default_factory=dict)  # email / password
    register_fields: dict[str, Selectors] = field(default_factory=dict)  # password / confirm


# --- heuristic (every platform, after the adapter's own selectors) --------------------
HEURISTIC = AdapterHints(
    name="heuristic",
    product_links=(
        "a[href*='/product/']",
        "a[href*='/products/']",
        "a[href*='/produkt/']",
        "a[href*='/produkte/']",
        "a[href*='/produit/']",
        "a[href*='/producto/']",
        "a[href*='/prodotto/']",
        "a[href*='/artikel/']",
        "a[href*='/item/']",
        "a[href*='/p/']",
        ".product a[href]",
        ".product-item a[href]",
        ".product-card a[href]",
        ".product-box a[href]",
        "[class*='product'] a[href]",
        "article a[href]",
    ),
    product_url_patterns=(
        r"/product[s]?/",
        r"/produkt[e]?/",
        r"/produit[s]?/",
        r"/producto[s]?/",
        r"/prodott[oi]/",
        r"/artikel/",
        r"/item/",
        r"/p/[\w-]+",
        r"-p-\d+",
        r"\.html?$",
    ),
    variant_selects=(
        "form select[name*='attribute' i]",
        "form select[name*='variant' i]",
        "form select[name*='option' i]",
        "form select[name*='size' i]",
        "form select[name*='color' i]",
        "form select[name*='colour' i]",
        "form select[name*='groesse' i]",
        "form select[name*='größe' i]",
        "form select[name*='farbe' i]",
        "form select[name*='taille' i]",
        "form select[name*='couleur' i]",
        "form select[name*='maat' i]",
        "form select[name*='talla' i]",
        "form select[name*='taglia' i]",
        "form select[name*='rozmiar' i]",
        "form select[name*='tamanho' i]",
        "form select[name*='storlek' i]",
        "form select[name*='størrelse' i]",
        "form select[name*='group' i]",
    ),
    variant_options=(
        "[class*='swatch'] [role=radio]",
        "[class*='swatch'] label",
        "[class*='variant'] label",
        "[class*='option'] input[type=radio]",
        "[class*='configurator'] input[type=radio]",
    ),
    add_to_cart=(
        "button[name='add-to-cart']",
        "button[name='add']",
        "button[id*='add-to-cart' i]",
        "button[id*='addtocart' i]",
        "button[class*='add-to-cart' i]",
        "button[class*='addtocart' i]",
        "button[class*='add_to_cart' i]",
        "button[class*='btn-buy' i]",
        "button[data-action*='add' i][data-action*='cart' i]",
        "form[action*='cart' i] button[type=submit]",
        "input[type=submit][name='add']",
    ),
    cart_links=(
        "a[href$='/cart']",
        "a[href$='/cart/']",
        "a[href*='/cart?']",
        "a[href*='/checkout/cart']",
        "a[href*='/warenkorb']",
        "a[href*='/panier']",
        "a[href*='/winkelwagen']",
        "a[href*='/carrito']",
        "a[href*='/carrello']",
        "a[href*='/koszyk']",
        "a[href*='/carrinho']",
        "a[href*='/varukorg']",
        "a[href*='/kurv']",
        "a[href*='/basket']",
        "a[class*='cart' i]",
        "a[id*='cart' i]",
    ),
    cart_items=(
        "[class*='cart-item' i]",
        "[class*='cart_item' i]",
        "[class*='line-item' i]",
        "[class*='cart-product' i]",
        "[class*='basket-item' i]",
        "tr.item",
        ".cart .item",
        "input[name*='quantity' i]",
        "input[name*='qty' i]",
    ),
    checkout_links=(
        "a[href$='/checkout']",
        "a[href$='/checkout/']",
        "a[href*='/checkout/confirm']",
        "a[href*='/checkout?']",
        "a[href*='/kasse']",
        "a[href*='/caisse']",
        "a[href*='/afrekenen']",
        "a[href*='/order']",
        "a[href*='/commande']",
        "a[class*='checkout' i]",
        "button[class*='checkout' i]",
        "a[id*='checkout' i]",
        "button[id*='checkout' i]",
    ),
    guest_controls=(
        "input[type=checkbox][id*='guest' i]",
        "input[type=radio][id*='guest' i]",
        "input[type=radio][value*='guest' i]",
        "input[type=checkbox][name*='guest' i]",
        "button[class*='guest' i]",
        "a[class*='guest' i]",
        "a[href*='guest' i]",
        "[data-checkout-type='guest']",
    ),
    login_form=(
        "form[action*='login' i]",
        "form[id*='login' i]",
        "form[class*='login' i]",
        "form:has(input[type=password]):not(:has(input[name*='confirm' i]))",
    ),
    register_controls=(
        "a[href*='register' i]",
        "a[href*='signup' i]",
        "a[href*='sign-up' i]",
        "a[href*='create-account' i]",
        "a[href*='account/create' i]",
        "a[href*='registrierung' i]",
        "a[href*='inscription' i]",
        "button[class*='register' i]",
        "a[class*='register' i]",
    ),
    register_form=(
        "form[action*='register' i]",
        "form[id*='register' i]",
        "form[class*='register' i]",
        "form[action*='account/create' i]",
        "form:has(input[type=password]):has(input[type=password] ~ input[type=password])",
    ),
    fields={
        BuyerField.EMAIL: (
            "input[autocomplete='email']",
            "input[type='email']",
            "input[name*='email' i]:not([name*='confirm' i])",
            "input[id*='email' i]:not([id*='confirm' i])",
        ),
        BuyerField.EMAIL_CONFIRM: (
            "input[name*='email' i][name*='confirm' i]",
            "input[id*='email' i][id*='confirm' i]",
            "input[name*='email' i][name*='repeat' i]",
        ),
        BuyerField.FIRST_NAME: (
            "input[autocomplete='given-name']",
            "input[name*='first' i][name*='name' i]",
            "input[name='firstname']",
            "input[name*='vorname' i]",
            "input[name*='prenom' i]",
            "input[name*='prénom' i]",
            "input[name*='voornaam' i]",
            "input[name*='nombre' i]",
            "input[name*='imie' i]",
            "input[name*='imię' i]",
            "input[name*='fornavn' i]",
            "input[name*='förnamn' i]",
            "input[id*='first' i][id*='name' i]",
        ),
        BuyerField.LAST_NAME: (
            "input[autocomplete='family-name']",
            "input[name*='last' i][name*='name' i]",
            "input[name='lastname']",
            "input[name*='nachname' i]",
            "input[name*='surname' i]",
            "input[name*='achternaam' i]",
            "input[name*='apellido' i]",
            "input[name*='cognome' i]",
            "input[name*='nazwisko' i]",
            "input[name*='apelido' i]",
            "input[name*='efternamn' i]",
            "input[name*='efternavn' i]",
            "input[name='nom']",
            "input[id*='last' i][id*='name' i]",
        ),
        BuyerField.FULL_NAME: (
            "input[autocomplete='name']",
            "input[name='name']",
            "input[name*='full' i][name*='name' i]",
        ),
        BuyerField.STREET_LINE: (
            "input[autocomplete='address-line1']",
            "input[autocomplete='street-address']",
            "input[name*='street' i]:not([name*='number' i]):not([name*='2' i])",
            "input[name*='address1' i]",
            "input[name*='address_1' i]",
            "input[name*='address-1' i]",
            "input[name*='strasse' i]",
            "input[name*='straße' i]",
            "input[name*='adresse' i]",
            "input[name*='straat' i]",
            "input[name*='direccion' i]",
            "input[name*='dirección' i]",
            "input[name*='indirizzo' i]",
            "input[name*='ulica' i]",
            "input[name*='morada' i]",
            "input[name*='gatuadress' i]",
            "input[name*='vejnavn' i]",
            "input[name*='address' i]:not([name*='email' i]):not([name*='2' i])",
            "input[id*='street' i]",
            "input[id*='address' i]:not([id*='email' i]):not([id*='2' i])",
        ),
        BuyerField.HOUSE_NUMBER: (
            "input[name*='housenumber' i]",
            "input[name*='house_number' i]",
            "input[name*='house-number' i]",
            "input[name*='hausnummer' i]",
            "input[name*='huisnummer' i]",
            "input[name*='streetnumber' i]",
            "input[name*='street_number' i]",
            "input[name*='numero' i]",
            "input[id*='housenumber' i]",
            "input[id*='hausnummer' i]",
        ),
        BuyerField.POSTCODE: (
            "input[autocomplete='postal-code']",
            "input[name*='postcode' i]",
            "input[name*='postal' i]",
            "input[name*='zip' i]",
            "input[name*='plz' i]",
            "input[name*='cp' i][name*='postal' i]",
            "input[name*='kod' i][name*='poczt' i]",
            "input[name*='codigo' i]",
            "input[name*='cap' i]",
            "input[id*='postcode' i]",
            "input[id*='zip' i]",
            "input[id*='plz' i]",
        ),
        BuyerField.CITY: (
            "input[autocomplete='address-level2']",
            "input[name*='city' i]",
            "input[name*='town' i]",
            "input[name*='ort' i]:not([name*='sort' i])",
            "input[name*='ville' i]",
            "input[name*='plaats' i]",
            "input[name*='ciudad' i]",
            "input[name*='citta' i]",
            "input[name*='città' i]",
            "input[name*='miasto' i]",
            "input[name*='localidade' i]",
            "input[name*='stad' i]",
            "input[name*='by' i][name*='navn' i]",
            "input[id*='city' i]",
        ),
        BuyerField.PHONE: (
            "input[autocomplete='tel']",
            "input[type='tel']",
            "input[name*='phone' i]",
            "input[name*='telephone' i]",
            "input[name*='telefon' i]",
            "input[name*='mobile' i]",
            "input[id*='phone' i]",
            "input[id*='telefon' i]",
        ),
    },
    country_selects=(
        "select[autocomplete='country']",
        "select[autocomplete='country-name']",
        "select[name*='country' i]",
        "select[name*='land' i]",
        "select[name*='pays' i]",
        "select[name*='pais' i]",
        "select[name*='país' i]",
        "select[name*='paese' i]",
        "select[name*='kraj' i]",
        "select[id*='country' i]",
    ),
    region_selects=(
        "select[autocomplete='address-level1']",
        "select[name*='state' i]",
        "select[name*='region' i]",
        "select[name*='province' i]",
        "select[name*='provincia' i]",
        "select[name*='county' i]",
        "select[id*='region' i]",
        "select[id*='state' i]",
    ),
    salutation_selects=(
        "select[name*='salutation' i]",
        "select[name*='anrede' i]",
        "select[name*='title' i]",
        "select[name*='gender' i]",
        "select[name*='civilite' i]",
        "select[name*='civilité' i]",
        "select[name*='prefix' i]",
    ),
    required_checkboxes=(
        "input[type=checkbox][required]",
        "input[type=checkbox][name*='terms' i]",
        "input[type=checkbox][name*='agb' i]",
        "input[type=checkbox][name*='privacy' i]",
        "input[type=checkbox][name*='conditions' i]",
        "input[type=checkbox][name*='gdpr' i]",
        "input[type=checkbox][name*='accept' i]",
        "input[type=checkbox][id*='terms' i]",
        "input[type=checkbox][id*='agb' i]",
        "input[type=checkbox][id*='privacy' i]",
        "input[type=checkbox][id*='conditions' i]",
    ),
    shipping_options=(
        "input[type=radio][name*='shipping' i]",
        "input[type=radio][name*='delivery' i]",
        "input[type=radio][name*='versand' i]",
        "input[type=radio][name*='livraison' i]",
        "input[type=radio][name*='verzend' i]",
        "input[type=radio][name*='envio' i]",
        "input[type=radio][name*='envío' i]",
        "input[type=radio][name*='spedizione' i]",
        "input[type=radio][name*='dostaw' i]",
        "input[type=radio][name*='entrega' i]",
        "input[type=radio][name*='frakt' i]",
        "input[type=radio][name*='levering' i]",
        "[class*='shipping-method' i] input[type=radio]",
        "[class*='delivery-option' i] input[type=radio]",
    ),
    payment_blocks=(
        "#payment",
        "[id*='payment-method' i]",
        "[class*='payment-method' i]",
        "[id*='payment' i]",
        "[class*='payment' i]",
        "[id*='zahlung' i]",
        "[class*='zahlung' i]",
        "[id*='paiement' i]",
        "[class*='paiement' i]",
        "[id*='betaling' i]",
        "[class*='betaling' i]",
        "[id*='pago' i]",
        "[class*='pago' i]",
        "[id*='pagamento' i]",
        "[class*='pagamento' i]",
        "[id*='platnosc' i]",
        "[class*='platnosc' i]",
        "[id*='betalning' i]",
        "[class*='betalning' i]",
    ),
    payment_options=(
        "input[type=radio][name*='payment' i]",
        "input[type=radio][name*='zahlung' i]",
        "input[type=radio][name*='paiement' i]",
        "input[type=radio][name*='betaling' i]",
        "input[type=radio][name*='pago' i]",
        "input[type=radio][name*='pagamento' i]",
        "input[type=radio][name*='platnosc' i]",
        "input[type=radio][name*='betalning' i]",
        "[class*='payment-method' i] input[type=radio]",
        "[class*='payment-option' i] input[type=radio]",
        "iframe[name^='__privateStripeFrame']",
        "[data-cse]",
        "iframe[id^='braintree-hosted-field']",
        "[class*='adyen-checkout'] input",
        "iframe[src*='paypal.com']",
        "iframe[src*='klarna.com']",
        "iframe[src*='mollie.com']",
        "iframe[src*='checkout.com']",
    ),
    login_fields={
        "email": (
            "form[action*='login' i] input[type=email]",
            "form[action*='login' i] input[name*='email' i]",
            "form[action*='login' i] input[name*='user' i]",
            "input[name='login[username]']",
            "input[type=email]",
            "input[name*='email' i]",
            "input[name*='username' i]",
        ),
        "password": (
            "form[action*='login' i] input[type=password]",
            "input[name='login[password]']",
            "input[type=password]",
        ),
    },
    register_fields={
        "password": (
            "input[type=password][autocomplete='new-password']",
            "input[type=password]:not([name*='confirm' i]):not([name*='repeat' i])",
        ),
        "confirm": (
            "input[type=password][name*='confirm' i]",
            "input[type=password][name*='repeat' i]",
            "input[type=password][name*='confirmation' i]",
            "input[type=password][id*='confirm' i]",
        ),
    },
)


def merged(primary: AdapterHints, fallback: AdapterHints = HEURISTIC) -> AdapterHints:
    """Adapter selectors first, heuristic ones after (duplicates removed, order kept)."""

    def cat(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(a + b))

    fields = {
        k: cat(primary.fields.get(k, ()), fallback.fields.get(k, ()))
        for k in set(primary.fields) | set(fallback.fields)
    }
    login_fields = {
        k: cat(primary.login_fields.get(k, ()), fallback.login_fields.get(k, ()))
        for k in set(primary.login_fields) | set(fallback.login_fields)
    }
    register_fields = {
        k: cat(primary.register_fields.get(k, ()), fallback.register_fields.get(k, ()))
        for k in set(primary.register_fields) | set(fallback.register_fields)
    }
    return AdapterHints(
        name=primary.name,
        platform_ids=primary.platform_ids,
        product_links=cat(primary.product_links, fallback.product_links),
        product_url_patterns=cat(primary.product_url_patterns, fallback.product_url_patterns),
        variant_selects=cat(primary.variant_selects, fallback.variant_selects),
        variant_options=cat(primary.variant_options, fallback.variant_options),
        add_to_cart=cat(primary.add_to_cart, fallback.add_to_cart),
        cart_path=primary.cart_path or fallback.cart_path,
        cart_links=cat(primary.cart_links, fallback.cart_links),
        cart_items=cat(primary.cart_items, fallback.cart_items),
        checkout_path=primary.checkout_path or fallback.checkout_path,
        checkout_links=cat(primary.checkout_links, fallback.checkout_links),
        guest_controls=cat(primary.guest_controls, fallback.guest_controls),
        login_form=cat(primary.login_form, fallback.login_form),
        register_controls=cat(primary.register_controls, fallback.register_controls),
        register_form=cat(primary.register_form, fallback.register_form),
        fields=fields,
        country_selects=cat(primary.country_selects, fallback.country_selects),
        region_selects=cat(primary.region_selects, fallback.region_selects),
        salutation_selects=cat(primary.salutation_selects, fallback.salutation_selects),
        required_checkboxes=cat(primary.required_checkboxes, fallback.required_checkboxes),
        shipping_options=cat(primary.shipping_options, fallback.shipping_options),
        step_buttons=cat(primary.step_buttons, fallback.step_buttons),
        payment_blocks=cat(primary.payment_blocks, fallback.payment_blocks),
        payment_options=cat(primary.payment_options, fallback.payment_options),
        login_fields=login_fields,
        register_fields=register_fields,
    )
