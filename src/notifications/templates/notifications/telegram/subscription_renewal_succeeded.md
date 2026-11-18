{%load i18n %}✅ {%blocktranslate with plan=context.plan_name org=context.organization_name amount=context.amount date=context.period_end %}Your <b>{{ plan }}</b> membership at <b>{{ org }}</b> was renewed for {{ amount }}. Next renewal: {{ date }}.{%endblocktranslate%}{%if context.next_charge_amount%} {%blocktranslate with amount=context.next_charge_amount date=context.next_charge_date %}Next charge: {{ amount }} on {{ date }}.{%endblocktranslate%}{%endif%}

{%if context.manage_subscription_url%}<a href="{{context.manage_subscription_url}}">{%trans "Manage Billing"%}</a>{%endif%}
