{%load i18n markdown_tags %}{%if context.application_accepted%}{%blocktranslate with org=context.organization_name %}**{{ org }}** accepted your application.{%endblocktranslate%}

{%endif%}{%blocktranslate with tier=context.tier_name plan=context.plan_name amount=context.amount org=context.organization_name days=context.payment_window_days %}To activate your **{{ tier }}** membership ({{ plan }}, {{ amount }}), pay **{{ org }}** directly. Your membership starts once they confirm your payment. If no payment is recorded within {{ days }} days, your request is cancelled.{%endblocktranslate%}

{%if context.payment_instructions%}> {{ context.payment_instructions|strip_leading_heading }}{%else%}{%blocktranslate with org=context.organization_name url=context.organization_contact_url %}{{ org }} will tell you how to pay: {{ url }}{%endblocktranslate%}{%endif%}

[{%trans "Your memberships" %}]({{ context.memberships_url }})
