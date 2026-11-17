{%load i18n %}{%blocktranslate with tier=context.tier_name plan=context.plan_name amount=context.amount org=context.organization_name %}To activate your <b>{{ tier }}</b> membership ({{ plan }}, {{ amount }}), pay <b>{{ org }}</b> directly. Your membership starts once they confirm your payment. If no payment is recorded within 30 days, your request is cancelled.{%endblocktranslate%}

{%if context.payment_instructions%}{{ context.payment_instructions }}{%else%}{%blocktranslate with org=context.organization_name url=context.organization_contact_url %}{{ org }} will tell you how to pay: {{ url }}{%endblocktranslate%}{%endif%}

<a href="{{context.memberships_url}}">{%blocktranslate with org=context.organization_name %}Your membership at {{ org }}{%endblocktranslate%}</a>
