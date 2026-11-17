{%load i18n %}{%blocktranslate with tier=context.tier_name plan=context.plan_name amount=context.amount org=context.organization_name %}To activate your **{{ tier }}** membership ({{ plan }}, {{ amount }}), pay **{{ org }}** directly. Your membership starts once they confirm your payment. If no payment is recorded within 30 days, your request is cancelled.{%endblocktranslate%}

{%if context.payment_instructions%}{{ context.payment_instructions }}{%else%}{%blocktranslate with org=context.organization_name url=context.organization_contact_url %}{{ org }} will tell you how to pay: {{ url }}{%endblocktranslate%}{%endif%}

[{%trans "Your memberships" %}]({{ context.memberships_url }})
