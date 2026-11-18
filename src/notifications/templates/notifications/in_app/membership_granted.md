{%load i18n %}🎊 {%blocktranslate with org=context.organization_name role=context.role %}You are now a **{{ role }}** of **{{ org }}**!{%endblocktranslate%}{%if context.next_charge_amount%} {%blocktranslate with amount=context.next_charge_amount date=context.next_charge_date %}Next charge: {{ amount }} on {{ date }}.{%endblocktranslate%}{%endif%}

[{%trans "View Organization"%}]({{context.frontend_url}})
