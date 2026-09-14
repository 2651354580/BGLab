"""Focused Textual editor for the Primary and optional Standby slots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from textual import events
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from bglab.llm import credentials
from bglab.llm.provider_slots import ProviderSlot, ProviderSlotPolicy
from bglab.llm.providers import PROVIDERS

SlotId = Literal["primary", "standby"]
SecretWriter = Callable[[str, str, str], object]
SecretClearer = Callable[[str, str], object]
Probe = Callable[[ProviderSlot], object | Awaitable[object]]


@dataclass(frozen=True)
class ProviderSlotEdit:
    """Non-secret slot metadata returned by the editor."""

    slot_id: SlotId
    provider: str
    model: str
    base_url: str
    enabled: bool
    chat_stream: bool = True


class ProviderSlotsScreen(ModalScreen[ProviderSlotEdit | None]):
    """Small, keyboard-friendly slot editor.

    Secrets are handed directly to the credential service and immediately
    discarded.  They are never part of ``ProviderSlotEdit`` or a rendered
    widget value after submission.
    """

    id = "provider-slots"
    BINDINGS = [("escape", "back", "返回"), ("ctrl+s", "save", "保存")]

    CSS = """
    ProviderSlotsScreen { align: center middle; background: #10131c 85%; }
    #provider-slots-dialog { width: 72; max-width: 100%; height: 24; max-height: 100%; padding: 1 2; border: none; background: #1e2432; color: #e6e9f2; }
    #provider-slots-title { height: 1; color: #e6e9f2; text-style: bold; }
    #provider-steps { height: 1; margin: 1 0; color: #78869f; }
    #provider-slots-body { height: 1fr; min-height: 1; scrollbar-size: 1 1; scrollbar-color: #485774; scrollbar-background: #1e2432; }
    .provider-step { height: auto; }
    .provider-step Label, .provider-step Static { height: auto; }
    #provider-slot-list { height: 3; margin-bottom: 1; }
    #provider-slot-list > SelectCurrent { background: #151b28; border: none; padding: 1 1; color: #e6e9f2; }
    #provider-list { height: 6; }
    #model-list { height: 7; margin-bottom: 1; }
    #provider-slots-dialog OptionList { background: #1e2432; border: none; padding: 0; color: #bdc6d8; }
    #provider-slots-dialog OptionList > .option-list--option { padding: 0 1; }
    #provider-slots-dialog OptionList > .option-list--option-highlighted { background: #354568; color: #eff4ff; text-style: bold; }
    #provider-slots-dialog Input { height: 3; margin: 0; padding: 1 1; background: #151b28; border: none; border-left: thick #30394c; color: #e6e9f2; }
    #provider-slots-dialog Input:focus { border-left: thick #9bbcff; }
    ProviderSlotsScreen.compact #provider-slots-dialog Input { height: 1; padding: 0 1; }
    ProviderSlotsScreen.compact #model-list { height: 5; }
    #provider-selection { margin-bottom: 1; color: #9bbcff; }
    #provider-slot-status { height: auto; min-height: 1; margin-top: 1; color: #a0abc0; }
    #provider-slots-buttons { height: 3; min-height: 3; align: right middle; margin-top: 1; }
    #provider-slots-buttons Button { min-width: 8; width: auto; margin-left: 1; border: none; background: #1e2432; color: #a0abc0; }
    #provider-slots-buttons Button:focus { background: #354568; color: #ffffff; text-style: bold; }
    #provider-slots-buttons .primary-action { background: #9bbcff; color: #171d2c; text-style: bold; }
    #provider-slots-buttons .primary-action:focus { background: #c1d5ff; color: #101623; }
    .provider-slots-caption { color: #a0abc0; }
    """

    def __init__(
        self,
        policy: ProviderSlotPolicy,
        *,
        cwd: str | None = None,
        initial_slot: SlotId = "primary",
        focus_key: bool = False,
        secret_writer: SecretWriter | None = None,
        secret_clearer: SecretClearer | None = None,
        probe: Probe | None = None,
    ) -> None:
        super().__init__()
        self.policy = policy
        self.cwd = cwd
        self.selected_slot: SlotId = initial_slot
        self.focus_key = focus_key
        self.credential_action = "preserve"
        self.last_probe_result: object | None = None
        self._secret_writer = secret_writer or self._default_secret_writer
        self._secret_clearer = secret_clearer or self._default_secret_clearer
        self._probe = probe
        self._provider = self._slot_for(initial_slot).provider
        self._model = self._slot_for(initial_slot).model
        self._base_url = self._slot_for(initial_slot).base_url
        self._enabled = self._slot_for(initial_slot).enabled
        self._step = 3 if focus_key else 1
        self._probing = False

    def compose(self) -> ComposeResult:
        providers = [Option(f"{provider.display_name}\n[dim]{'官方 API' if provider.id == 'deepseek' else 'OpenCode Go 订阅服务'}[/]", id=provider.id) for provider in PROVIDERS]
        yield Container(
            Label("连接模型", id="provider-slots-title"),
            Static("", id="provider-steps"),
            VerticalScroll(
                Container(
                    Label("使用位置", classes="provider-slots-caption"),
                    Select([("主用 · 桌游使用此配置", "primary"), ("备用 · 仅实验性对话使用", "standby")],
                           value=self.selected_slot, allow_blank=False, id="provider-slot-list"),
                    Label("服务商", classes="provider-slots-caption"),
                    OptionList(*providers, id="provider-list"),
                    id="provider-step-1", classes="provider-step",
                ),
                Container(
                    Label("选择模型，Enter 继续", classes="provider-slots-caption"),
                    OptionList(id="model-list"),
                    Label("自定义模型 ID", classes="provider-slots-caption"),
                    Input(value=self._model, id="model-input"),
                    id="provider-step-2", classes="provider-step",
                ),
                Container(
                    Static("", id="provider-selection", markup=False),
                    Label("API 地址", classes="provider-slots-caption"),
                    Input(value=self._base_url, id="base-url-input"),
                    Label("API Key", classes="provider-slots-caption"),
                    Input(placeholder="请粘贴 API Key", password=True, id="credential-input"),
                    Static("", id="credential-status", classes="provider-slots-caption", markup=False),
                    id="provider-step-3", classes="provider-step",
                ),
                id="provider-slots-body",
            ),
            Static("Not tested", id="provider-slot-status", classes="provider-slots-caption"),
            Horizontal(
                Button("取消", id="provider-slot-cancel"),
                Button("上一步", id="provider-slot-back"),
                Button("停用备用", id="provider-slot-clear"),
                Button("测试连接", id="provider-slot-test"),
                Button("保存", id="provider-slot-save", classes="primary-action"),
                Button("下一步", id="provider-slot-next", classes="primary-action"),
                id="provider-slots-buttons",
            ),
            id="provider-slots-dialog",
        )

    def on_mount(self) -> None:
        self.set_class(self.size.height < 24, "compact")
        self._refresh_controls()
        self._show_step(self._step)

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.height < 24, "compact")

    def _show_step(self, step: int) -> None:
        self._step = step
        for index in (1, 2, 3):
            self.query_one(f"#provider-step-{index}").display = index == step
        labels = ("服务商", "模型", "API Key")
        self.query_one("#provider-steps", Static).update(
            "    ".join(f"[bold #9bbcff]{i}  {label}[/]" if i == step else f"{i}  {label}" for i, label in enumerate(labels, 1)),
        )
        for name in ("test", "save"):
            self.query_one(f"#provider-slot-{name}").display = step == 3
        self.query_one("#provider-slot-clear").display = step == 1 and self.selected_slot == "standby"
        self.query_one("#provider-slot-back").display = step > 1
        self.query_one("#provider-slot-next").display = step < 3
        self.query_one("#provider-slots-body", VerticalScroll).scroll_home(animate=False)
        self._refresh_key_status()
        self.query_one("#provider-slot-status", Static).update(
            ("↑↓ 选择   Enter 继续   Esc 取消", "↑↓ 选择   Enter 配置 Key   Tab 自定义",
             "测试会保存 Key 并请求一次模型；保存后应用配置。")[step - 1],
        )
        self.call_after_refresh(self._focus_step)

    def _focus_step(self) -> None:
        self.query_one({1: "#provider-list", 2: "#model-list", 3: "#credential-input"}[self._step]).focus()

    async def next_step(self) -> None:
        self._remember_controls()
        if self._step == 2 and (not self._model or "/" in self._model):
            self.query_one("#provider-slot-status", Static).update("请填写有效的模型 ID（不含服务商前缀）。")
            return
        self._show_step(min(3, self._step + 1))

    async def action_back(self) -> None:
        if self._probing:
            return
        if self._step > 1:
            self._remember_controls()
            self._show_step(self._step - 1)
        else:
            await self.cancel()

    async def action_save(self) -> None:
        if self._step == 3 and not self._probing:
            await self.save_slot()

    def _focus_key_input(self) -> None:
        try:
            self.query_one("#credential-input", Input).focus()
        except Exception:
            pass

    async def choose_slot(self, slot_id: SlotId) -> None:
        if slot_id not in {"primary", "standby"}:
            raise ValueError("slot must be primary or standby")
        self._remember_controls()
        self.selected_slot = slot_id
        slot = self._slot_for(slot_id)
        self._provider, self._model, self._base_url, self._enabled = (
            slot.provider,
            slot.model,
            slot.base_url,
            slot.enabled,
        )
        if slot_id == "standby":
            self._enabled = True
        self.credential_action = "preserve"
        self._set_control_value("#credential-input", "")
        self._refresh_controls()
        if self.is_mounted:
            self._show_step(1)

    async def choose_provider(self, provider_id: str) -> None:
        provider = next((item for item in PROVIDERS if item.id == provider_id), None)
        if provider is None:
            raise ValueError(f"Unknown provider '{provider_id}'")
        self._remember_controls()
        if self._provider != provider.id:
            self._set_control_value("#credential-input", "")
            self.credential_action = "preserve"
            self._base_url = provider.openai_base_url
            self._model = provider.models[0].id
        self._provider = provider.id
        self._refresh_controls()

    async def set_base_url(self, base_url: str) -> None:
        value = str(base_url).strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("base URL must be an absolute http(s) URL")
        self._base_url = value
        self._set_control_value("#base-url-input", value)

    async def choose_model(self, model_id: str) -> None:
        value = str(model_id).strip()
        if not value or "/" in value:
            raise ValueError("model ID must be non-empty and must not contain '/'")
        self._model = value
        self._set_control_value("#model-input", value)

    async def submit_secret_for_test(self, value: str) -> None:
        """Test seam for password submission; value is not retained."""
        if value == "":
            self.credential_action = "preserve"
            self._set_control_value("#credential-input", "")
            return
        slot = self._slot_from_current()
        self._secret_writer(slot.credential_env, value, self.cwd or "")
        self.credential_action = "replace"
        if self.selected_slot == "standby":
            self._enabled = True
        self._set_control_value("#credential-input", "")

    async def press_clear_standby(self) -> None:
        if self.selected_slot != "standby":
            await self.choose_slot("standby")
        slot = self._slot_from_current()
        self._secret_clearer(slot.credential_env, self.cwd or "")
        self.credential_action = "clear"
        self._enabled = False

    async def test_connection(self) -> bool:
        if self._probing:
            return False
        self._remember_controls()
        if not self._validate_connection():
            return False
        # The Provider client reads credentials from the local credential
        # service.  Persist a pending write-only value before probing, then
        # immediately clear the widget so it can never be rendered later.
        try:
            self._consume_secret_input()
        except OSError:
            self.query_one("#provider-slot-status", Static).update("Key 保存失败，请检查本地 .env 的写入权限。")
            return False
        slot = self._slot_from_current()
        if self._probe is None:
            self.last_probe_result = False
            self._set_probe_status(False)
            return False
        self._set_probe_status(None)
        self._probing = True
        self.query_one("#provider-slots-body").disabled = True
        self.query_one("#provider-slots-buttons").disabled = True
        try:
            result = self._probe(slot)
            if hasattr(result, "__await__"):
                result = await result
        except Exception:
            result = False
        finally:
            self._probing = False
            self.query_one("#provider-slots-body").disabled = False
            self.query_one("#provider-slots-buttons").disabled = False
        self.last_probe_result = result
        connected = bool(result)
        self._set_probe_status(connected)
        self._refresh_key_status()
        return connected

    async def save_slot(self) -> None:
        self._remember_controls()
        if not self._validate_connection():
            return
        try:
            self._consume_secret_input()
        except OSError:
            self.query_one("#provider-slot-status", Static).update("Key 保存失败，请检查本地 .env 的写入权限。")
            return
        if self.selected_slot == "standby":
            self._enabled = self.credential_action != "clear"
        edit = ProviderSlotEdit(
            slot_id=self.selected_slot,
            provider=self._provider,
            model=self._model,
            base_url=self._base_url,
            enabled=self._enabled,
            chat_stream=self._slot_from_current().chat_stream,
        )
        self.dismiss(edit)

    async def cancel(self) -> None:
        if self._probing:
            return
        self._set_control_value("#credential-input", "")
        self.dismiss(None)

    async def action_cancel(self) -> None:
        await self.cancel()

    def _slot_for(self, slot_id: SlotId) -> ProviderSlot:
        if slot_id == "primary":
            return self.policy.primary
        if self.policy.standby is not None:
            return self.policy.standby
        return ProviderSlot(
            id="standby",
            provider=self.policy.primary.provider,
            model=self.policy.primary.model,
            base_url=self.policy.primary.base_url,
            credential_env=f"{self.policy.primary.credential_env}_STANDBY",
            enabled=False,
        )

    def _slot_from_current(self) -> ProviderSlot:
        previous = self._slot_for(self.selected_slot)
        return ProviderSlot(
            id=self.selected_slot,
            provider=self._provider,
            model=self._model,
            base_url=self._base_url,
            credential_env=self._credential_env_for_provider(),
            enabled=self._enabled,
            chat_stream=(
                previous.chat_stream
                if (previous.provider, previous.base_url) == (self._provider, self._base_url)
                else True
            ),
        )

    def _credential_env_for_provider(self) -> str:
        for provider in PROVIDERS:
            if provider.id == self._provider:
                env = provider.api_key_env
                if self.selected_slot == "standby" and provider.id == self.policy.primary.provider:
                    env = f"{env}_STANDBY"
                return env
        return self._slot_for(self.selected_slot).credential_env

    def _remember_controls(self) -> None:
        try:
            self._base_url = self.query_one("#base-url-input", Input).value.strip()
            self._model = self.query_one("#model-input", Input).value.strip()
        except Exception:
            pass

    def _consume_secret_input(self) -> None:
        try:
            value = self.query_one("#credential-input", Input).value
        except Exception:
            value = ""
        if not value:
            return
        slot = self._slot_from_current()
        self._secret_writer(slot.credential_env, value, self.cwd or "")
        self.credential_action = "replace"
        self._set_control_value("#credential-input", "")

    def _refresh_controls(self) -> None:
        self._set_control_value("#base-url-input", self._base_url)
        self._set_control_value("#model-input", self._model)
        if self.is_mounted:
            models = self.query_one("#model-list", OptionList)
            models.clear_options()
            provider = next(item for item in PROVIDERS if item.id == self._provider)
            models.add_options([Option(model.display_name, id=model.id) for model in provider.models])
            models.highlighted = next((i for i, model in enumerate(provider.models) if model.id == self._model), 0)
            self.query_one("#provider-list", OptionList).highlighted = next(i for i, item in enumerate(PROVIDERS) if item.id == self._provider)
            self.query_one("#provider-slot-list", Select).value = self.selected_slot
            self._refresh_key_status()

    def _has_saved_key(self) -> bool:
        return self.credential_action == "replace" or credentials.credential_status(
            self._credential_env_for_provider(), start=self.cwd,
        ).configured

    def _validate_connection(self) -> bool:
        from urllib.parse import urlsplit

        error = ""
        try:
            url = urlsplit(self._base_url)
            valid_url = url.scheme in {"http", "https"} and bool(url.hostname) and not url.username and not url.password
        except ValueError:
            valid_url = False
        if not self._model or "/" in self._model:
            error = "请填写有效的模型 ID（不含服务商前缀）。"
        elif not valid_url:
            error = "API 地址须为完整的 http(s) 地址，不能在地址中填写 Key。"
        elif not self.query_one("#credential-input", Input).value.strip() and not self._has_saved_key():
            error = "尚未配置 API Key，请在上方粘贴后保存。"
        if error:
            self._show_step(3)
            self.query_one("#provider-slot-status", Static).update(error)
            return False
        return True

    def _refresh_key_status(self) -> None:
        if not self.is_mounted:
            return
        pending = bool(self.query_one("#credential-input", Input).value.strip())
        configured = self._has_saved_key()
        text = "已输入新 Key，等待保存。" if pending else (
            "已保存 Key；留空沿用，填写新 Key 可替换。" if configured else "尚未配置 Key，请在上方粘贴。"
        )
        self.query_one("#credential-status", Static).update(text)
        self.query_one("#credential-input", Input).placeholder = "留空沿用已保存的 Key" if configured else "请粘贴 API Key"
        provider = next(item for item in PROVIDERS if item.id == self._provider)
        self.query_one("#provider-selection", Static).update(f"{provider.display_name} / {self._model}")

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        if event.input.id in {"base-url-input", "model-input", "credential-input"} and self.is_mounted:
            self._refresh_key_status()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "model-input":
            await self.next_step()
        elif event.input.id == "base-url-input":
            self._focus_key_input()
        elif event.input.id == "credential-input":
            self.query_one("#provider-slot-save").focus()

    def _set_control_value(self, selector: str, value: str) -> None:
        try:
            self.query_one(selector, Input).value = value
        except Exception:
            pass

    def _set_probe_status(self, connected: bool | None) -> None:
        text = {
            None: "正在测试连接，请稍候…",
            True: "连接成功 · 点击「保存」完成配置。",
            False: "连接失败 · 请检查 API 地址、Key 和网络后重试。",
        }[connected]
        try:
            self.query_one("#provider-slot-status", Static).update(text)
        except Exception:
            pass

    @staticmethod
    def _default_secret_writer(env_name: str, value: str, cwd: str) -> object:
        from pathlib import Path

        return credentials.save_credential(env_name, value, start=Path(cwd) if cwd else None)

    @staticmethod
    def _default_secret_clearer(env_name: str, cwd: str) -> object:
        from pathlib import Path

        return credentials.clear_credential(env_name, start=Path(cwd) if cwd else None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "provider-slot-save":
            await self.save_slot()
        elif button_id == "provider-slot-cancel":
            await self.cancel()
        elif button_id == "provider-slot-clear":
            await self.press_clear_standby()
            self.dismiss(ProviderSlotEdit(self.selected_slot, self._provider, self._model, self._base_url, False))
        elif button_id == "provider-slot-test":
            self.run_worker(self.test_connection(), exclusive=True, group="provider-probe")
        elif button_id == "provider-slot-next":
            await self.next_step()
        elif button_id == "provider-slot-back":
            await self.action_back()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "provider-slot-list" and event.value in {"primary", "standby"} and event.value != self.selected_slot:
            await self.choose_slot(event.value)

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = str(event.option_id or "")
        if event.option_list.id == "provider-list" and option_id:
            await self.choose_provider(option_id)
            self._show_step(2)
        elif event.option_list.id == "model-list" and option_id:
            await self.choose_model(option_id)
            self._show_step(3)
