{%load i18n %}💳 {%blocktranslate with plan=context.plan_name org=context.organization_name %}Your <b>{{ plan }}</b> membership at <b>{{ org }}</b> is ready to renew — complete the payment to reactivate it.{%endblocktranslate%}{%if context.next_charge_amount%}
{%blocktranslate with amount=context.next_charge_amount date=context.next_charge_date %}Next charge: {{ amount }} on {{ date }}.{%endblocktranslate%}{%endif%}

<a href="{{context.checkout_url}}">{%blocktranslate with amount=context.amount %}Pay {{ amount }} to renew{%endblocktranslate%}</a>
