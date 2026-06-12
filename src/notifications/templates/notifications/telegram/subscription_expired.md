{%load i18n %}🔔 {%blocktranslate with plan=context.plan_name org=context.organization_name date=context.expired_at %}Your <b>{{ plan }}</b> membership at <b>{{ org }}</b> ended on {{ date }}.{%endblocktranslate%}

{%if context.revival_url and context.revival_window_end%}<a href="{{context.revival_url}}">{%blocktranslate with date=context.revival_window_end %}Reactivate by {{ date }}{%endblocktranslate%}</a>{%endif%}
