{%load i18n %}💳 {%blocktranslate with plan=context.plan_name org=context.organization_name %}Your **{{ plan }}** membership at **{{ org }}** is ready to renew — complete the payment to reactivate it.{%endblocktranslate%}

[{%blocktranslate with amount=context.amount %}Pay {{ amount }} to renew{%endblocktranslate%}]({{context.checkout_url}})
