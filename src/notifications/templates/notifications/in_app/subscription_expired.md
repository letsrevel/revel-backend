{%load i18n %}🔔 {%blocktranslate with plan=context.plan_name org=context.organization_name date=context.expired_at %}Your **{{ plan }}** membership at **{{ org }}** ended on {{ date }}.{%endblocktranslate%}

{%if context.revival_url and context.revival_window_end%}[{%blocktranslate with date=context.revival_window_end %}Reactivate by {{ date }}{%endblocktranslate%}]({{context.revival_url}}){%endif%}
