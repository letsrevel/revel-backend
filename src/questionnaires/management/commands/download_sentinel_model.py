"""Download and save the prompt injection sentinel model for offline use."""

import typing as t
from pathlib import Path

from decouple import config
from django.conf import settings
from django.core.management.base import BaseCommand
from transformers import AutoModelForSequenceClassification, AutoTokenizer


class Command(BaseCommand):
    help = "Download and save the prompt injection sentinel model for offline use."

    def handle(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Download the sentinel model and tokenizer to local storage."""
        self.stdout.write("Downloading prompt injection sentinel model...")

        try:
            hf_token = config("HUGGING_FACE_HUB_TOKEN")
        except Exception:
            self.stdout.write(
                "HUGGING_FACE_HUB_TOKEN not found in environment. Please set this token to download the model.",
                self.style.ERROR,
            )
            return

        model_name = "qualifire/prompt-injection-sentinel"
        save_directory = Path(settings.BASE_DIR) / "questionnaires" / "llms" / "sentinel"

        # Create directory if it doesn't exist
        save_directory.mkdir(parents=True, exist_ok=True)

        try:
            # Download and save tokenizer
            self.stdout.write("Downloading tokenizer...")
            tokenizer = AutoTokenizer.from_pretrained(model_name, token=hf_token)  # type: ignore[no-untyped-call]
            tokenizer.save_pretrained(str(save_directory))
            self.stdout.write("Tokenizer saved successfully.", self.style.SUCCESS)

            # Download and save model
            self.stdout.write("Downloading model...")
            model = AutoModelForSequenceClassification.from_pretrained(model_name, token=hf_token)
            model.save_pretrained(str(save_directory))
            self.stdout.write("Model saved successfully.", self.style.SUCCESS)

            self.stdout.write(
                f"Sentinel model downloaded and saved to: {save_directory}",
                self.style.SUCCESS,
            )

        except Exception as e:
            self.stdout.write(
                f"Error downloading model: {e}",
                self.style.ERROR,
            )
