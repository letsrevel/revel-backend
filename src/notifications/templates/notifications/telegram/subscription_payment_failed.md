{%load i18n %}⚠️ {%blocktranslate with plan=context.plan_name org=context.organization_name date=context.grace_period_end %}We couldn't collect payment for your <b>{{ plan }}</b> membership at <b>{{ org }}</b>. Resolve this by {{ date }}.{%endblocktranslate%}

{%if context.is_online and context.manage_subscription_url%}<a href="{{context.manage_subscription_url}}">{%trans "Update Payment Method"%}</a>{%else%}<a href="{{context.organization_contact_url}}">{%blocktranslate with org=context.organization_name %}Contact {{ org }}{%endblocktranslate%}</a>{%endif%}
