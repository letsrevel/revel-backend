{%load i18n %}{%if context.trigger == "draft_event"%}{%blocktranslate with event=context.event_name %}You started an event called "{{ event }}" a while ago. It's still a draft, so nobody can see it yet.{%endblocktranslate%}

[{%trans "Open the draft"%}]({{context.action_url}}){%elif context.trigger == "private_profile"%}{%blocktranslate with org=context.organization_name %}{{ org }} is hidden from the public right now. New organizations on Revel start out private.{%endblocktranslate%}

[{%trans "Open settings"%}]({{context.action_url}}){%elif context.trigger == "no_events"%}{%blocktranslate with org=context.organization_name %}You set up {{ org }} on Revel a while ago, and there are no events in it yet.{%endblocktranslate%}

[{%trans "Create an event"%}]({{context.action_url}}){%elif context.trigger == "check_in"%}{%blocktranslate with org=context.organization_name %}I saw that {{ org }} hasn't published an event on Revel yet, and I'd like to know what's in the way.{%endblocktranslate%}{%elif context.trigger == "dormant"%}{%blocktranslate with org=context.organization_name event=context.event_name %}The last event on {{ org }} was "{{ event }}", and nothing new is scheduled.{%endblocktranslate%}

[{%trans "Create an event"%}]({{context.action_url}}){%endif%}
