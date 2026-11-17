{%load i18n markdown_tags %}{%if context.application_accepted%}{%blocktranslate with org=context.organization_name %}<b>{{ org }}</b> accepted your application.{%endblocktranslate%}

{%endif%}{%blocktranslate with tier=context.tier_name plan=context.plan_name amount=context.amount org=context.organization_name days=context.payment_window_days|default:30 %}To activate your <b>{{ tier }}</b> membership ({{ plan }}, {{ amount }}), pay <b>{{ org }}</b> directly. Your membership starts once they confirm your payment. If no payment is recorded within {{ days }} days, your request is cancelled.{%endblocktranslate%}

{%if context.payment_instructions%}{{ context.payment_instructions }}{%else%}{%blocktranslate with org=context.organization_name url=context.organization_contact_url %}{{ org }} will tell you how to pay: {{ url }}{%endblocktranslate%}{%endif%}

<a href="{{context.memberships_url}}">{%trans "Your memberships" %}</a>
