{%load i18n %}🔔 {%blocktranslate with plan=context.plan_name org=context.organization_name date=context.period_end amount=context.amount %}Your **{{ plan }}** membership at **{{ org }}** renews on {{ date }} for {{ amount }}.{%endblocktranslate%}

{%if context.is_online%}{%if context.manage_subscription_url%}[{%trans "Manage Billing"%}]({{context.manage_subscription_url}}){%endif%}{%else%}{%blocktranslate with org=context.organization_name %}To renew, pay {{ org }} directly.{%endblocktranslate%}

{%if context.payment_instructions%}{{context.payment_instructions}}{%else%}[{%blocktranslate with org=context.organization_name %}Contact {{ org }}{%endblocktranslate%}]({{context.organization_contact_url}}){%endif%}{%endif%}
