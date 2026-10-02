{% load i18n %}**{% blocktranslate with org=context.organization_name %}Invoices to issue yourself for {{ org }}{% endblocktranslate %}**

{% blocktranslate count counter=context.document_count|default:0 %}Revel did not issue this document because the law requires it to go through a national e-invoicing or fiscalization system (such as Peppol, KSeF or Verifactu). Issue it from your own system.{% plural %}Revel did not issue these documents because the law requires them to go through a national e-invoicing or fiscalization system (such as Peppol, KSeF or Verifactu). Issue them from your own system.{% endblocktranslate %}

{% blocktranslate with invoices=context.invoice_count invoice_total=context.invoice_totals|join:", "|default:"-" credit_notes=context.credit_note_count credit_note_total=context.credit_note_totals|join:", "|default:"-" %}Invoices: {{ invoices }} ({{ invoice_total }}). Credit notes: {{ credit_notes }} ({{ credit_note_total }}).{% endblocktranslate %}

{% for item in context.items %}- {% if item.kind == "credit_note" %}{% trans "Credit note" %}{% else %}{% trans "Invoice" %}{% endif %} — {{ item.event_name }} — {{ item.buyer_name }}{% if item.buyer_vat_id %} ({{ item.buyer_vat_id }}){% endif %} — {{ item.amount }}
{% endfor %}{% if context.more_count %}{% blocktranslate with more=context.more_count %}…and {{ more }} more.{% endblocktranslate %}{% endif %}

{% if not context.is_owner %}{% trans "The organization owner sees the full list with buyer details under Billing; the ticket list flags these sales." %}

{% endif %}[{% if context.is_owner %}{% trans "Open the invoices to issue yourself" %}{% else %}{% trans "Open the ticket list" %}{% endif %}]({{ context.action_url }})
