{%load i18n %}⚠️ {%blocktranslate with plan=context.plan_name org=context.organization_name date=context.grace_period_end %}We couldn't collect payment for your **{{ plan }}** membership at **{{ org }}**. Resolve this by {{ date }}.{%endblocktranslate%}

{%if context.is_online and context.customer_portal_url%}[{%trans "Update Payment Method"%}]({{context.customer_portal_url}}){%else%}[{%blocktranslate with org=context.organization_name %}Contact {{ org }}{%endblocktranslate%}](/organizations/{{context.organization_slug}}/contact){%endif%}
