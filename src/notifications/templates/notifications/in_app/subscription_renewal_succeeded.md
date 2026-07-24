{%load i18n %}✅ {%blocktranslate with plan=context.plan_name org=context.organization_name amount=context.amount date=context.period_end %}Your **{{ plan }}** membership at **{{ org }}** was renewed for {{ amount }}. Next renewal: {{ date }}.{%endblocktranslate%}

{%if context.customer_portal_url%}[{%trans "Manage Billing"%}]({{context.customer_portal_url}}){%endif%}
