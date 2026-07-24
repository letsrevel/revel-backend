{%load i18n %}🔔 {%blocktranslate with plan=context.plan_name org=context.organization_name date=context.period_end amount=context.amount %}Your **{{ plan }}** membership at **{{ org }}** renews on {{ date }} for {{ amount }}.{%endblocktranslate%}

{%if context.is_online%}{%if context.customer_portal_url%}[{%trans "Manage Billing"%}]({{context.customer_portal_url}}){%endif%}{%endif%}
