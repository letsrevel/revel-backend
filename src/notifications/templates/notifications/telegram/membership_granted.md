{%load i18n %}🎊 {%blocktranslate with org=context.organization_name role=context.role %}You are now a <b>{{ role }}</b> of <b>{{ org }}</b>!{%endblocktranslate%}{%if context.next_charge_amount%} {%blocktranslate with amount=context.next_charge_amount date=context.next_charge_date %}Next charge: {{ amount }} on {{ date }}.{%endblocktranslate%}{%endif%}

<a href="{{context.frontend_url}}">{%trans "View Organization"%}</a>
