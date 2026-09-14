"""Numbered two-level provider/model picker for the Rich + input REPL."""

from __future__ import annotations

from collections.abc import Callable

from bglab.llm.providers import PROVIDERS, canonical_model_reference


def pick_model(
    current: str,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> str | None:
    """Return a qualified model reference, or None when selection is cancelled."""
    try:
        current_ref = canonical_model_reference(current)
    except ValueError:
        current_ref = current

    while True:
        output_fn("Select provider:")
        for index, provider in enumerate(PROVIDERS, start=1):
            marker = "*" if current_ref.startswith(f"{provider.id}/") else " "
            output_fn(f"  {index}. [{marker}] {provider.display_name}")
        output_fn("  0. Cancel")

        try:
            provider_choice = input_fn("Provider: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if provider_choice in {"", "0", "q", "quit", "cancel"}:
            return None
        if not provider_choice.isdigit() or not (
            1 <= int(provider_choice) <= len(PROVIDERS)
        ):
            output_fn("Invalid provider selection.")
            continue

        provider = PROVIDERS[int(provider_choice) - 1]
        while True:
            output_fn(f"Select model from {provider.display_name}:")
            for index, model in enumerate(provider.models, start=1):
                reference = f"{provider.id}/{model.id}"
                marker = "*" if reference == current_ref else " "
                output_fn(
                    f"  {index}. [{marker}] {model.display_name} ({model.id})"
                )
            output_fn("  b. Back")
            output_fn("  0. Cancel")

            try:
                model_choice = input_fn("Model: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return None
            if model_choice in {"", "0", "q", "quit", "cancel"}:
                return None
            if model_choice in {"b", "back"}:
                break
            if not model_choice.isdigit() or not (
                1 <= int(model_choice) <= len(provider.models)
            ):
                output_fn("Invalid model selection.")
                continue

            model = provider.models[int(model_choice) - 1]
            return f"{provider.id}/{model.id}"
