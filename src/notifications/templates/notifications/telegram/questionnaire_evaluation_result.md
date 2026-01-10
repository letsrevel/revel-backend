{% load i18n %}{% if context.evaluation_status == "APPROVED" %}✅ {% blocktranslate with questionnaire=context.questionnaire_name %}<b>Your questionnaire {{ questionnaire }} has been approved!</b>{% endblocktranslate %}{% else %}❌ {% blocktranslate with questionnaire=context.questionnaire_name %}<b>Your questionnaire {{ questionnaire }} was not approved.</b>{% endblocktranslate %}{% endif %}

{% trans "Questionnaire:" %} {{ context.questionnaire_name }}
{% trans "Organization:" %} {{ context.organization_name }}
{% trans "Status:" %} {% if context.evaluation_status == "APPROVED" %}{% trans "Approved" %}{% else %}{% trans "Not Approved" %}{% endif %}
{% if context.evaluation_score %}{% trans "Score:" %} {{ context.evaluation_score }}{% endif %}
{% if context.evaluation_comments %}
<i>{% trans "Evaluator Comments:" %}</i>
{{ context.evaluation_comments }}
{% endif %}
{% if context.event_url %}<a href="{{ context.event_url }}">{% trans "View Event" %}</a>{% endif %}
