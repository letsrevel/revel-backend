import os
import django


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "revel.settings")

django.setup()

from transformers import pipeline, AutoTokenizer, AutoModelForSequenceClassification
from decouple import config


hf_token = config("HUGGING_FACE_HUB_TOKEN")

tokenizer = AutoTokenizer.from_pretrained('qualifire/prompt-injection-sentinel', token=hf_token)
model = AutoModelForSequenceClassification.from_pretrained('qualifire/prompt-injection-sentinel', token=hf_token)

save_directory = "src/models/prompt-injection-sentinel"
tokenizer.save_pretrained(save_directory)
model.save_pretrained(save_directory)

pipe = pipeline("text-classification", model=model, tokenizer=tokenizer)
result = pipe("Hello there")
print(result[0])
