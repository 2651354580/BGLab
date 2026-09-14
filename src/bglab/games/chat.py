"""Durable local-game chat delivery; no model invocation or game action selection."""
from __future__ import annotations

from typing import Callable
import html
from threading import RLock

CHAT_REPLY_REQUEST_ALLOWANCE = 3
CHAT_BATCH_SIZE = 2


def render_received_chat(chat: dict, recipient: int) -> str:
    sender = html.escape(str(chat.get('from', '?')), quote=True)
    message = html.escape(str(chat.get('message', '')))
    return (
        f'<game-chat id="{chat["id"]}" from="P{sender}" to="P{recipient}">'
        f'{message}</game-chat>\n请用 BgChat 回复这条消息，reply_to={chat["id"]}；'
        '回复后仍需完成当前游戏行动（如果已经提交，则不要再行动）。'
    )


def pending_reply_ids(ctx: dict) -> list[int]:
    inbox = ctx.get('_chat_inbox')
    if not callable(inbox):
        return []
    received = {item['id'] for item in inbox()}
    return [mid for mid in ctx.get('_chat_batch', []) if mid in received]


def finish_unanswered(ctx: dict) -> None:
    callback = ctx.get('_chat_finish_unanswered')
    ids = pending_reply_ids(ctx)
    if ids and callable(callback):
        callback(ids)


class GameChatLog:
    def __init__(self, store, player_types: list[str], *, publish: Callable, enqueue: Callable, acknowledge: Callable, enabled: Callable):
        self._lock = RLock()
        self.store = store
        self.player_types = player_types
        self.publish = publish
        self.enqueue = enqueue
        self.acknowledge = acknowledge
        self.enabled = enabled
        self.messages: dict[int, dict] = {}
        self.replies: dict[int, dict] = {}
        self.client_requests: dict[tuple[int, str], dict] = {}
        self.sequence = 0
        self.published: set[tuple[str, int]] = set()
        for event in store.read_events():
            self._record(event)
        # The event is durable before inbox mutation. Reconcile that small
        # crash window instead of losing delivery or sending another reply.
        for message_id, event in self.messages.items():
            recipient = event['to_pid']
            if message_id in self.replies:
                self.acknowledge(recipient, message_id)
            elif event.get('reply_to') is None and self.player_types[recipient] == 'ai' and self.enabled(recipient):
                self.enqueue(recipient, event['from_pid'], event['message'], message_id)

    def _record(self, event: dict) -> None:
        if event.get('type') not in {'game_chat', 'game_chat_status'}:
            return
        message_id = event.get('message_id')
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            self.sequence = max(self.sequence, message_id)
            if event['type'] == 'game_chat':
                self.messages[message_id] = event
        reply_to = event.get('reply_to')
        if isinstance(reply_to, int) and not isinstance(reply_to, bool):
            self.replies[reply_to] = event
        client_id = event.get('client_id')
        if client_id:
            self.client_requests[(event['from_pid'], client_id)] = event

    def send(self, sender: int, recipient: int, message: str, *, reply_to: int | None = None, client_id: str | None = None) -> str:
        with self._lock:
            return self._send(sender, recipient, message, reply_to=reply_to, client_id=client_id)

    def _send(self, sender: int, recipient: int, message: str, *, reply_to: int | None = None, client_id: str | None = None) -> str:
        if not all(isinstance(pid, int) and not isinstance(pid, bool) and 0 <= pid < len(self.player_types) for pid in (sender, recipient)):
            raise ValueError('Chat player is outside this game')
        if sender == recipient:
            raise ValueError('Chat cannot target the sending player')
        message = str(message).strip()
        if not message or len(message) > 500:
            raise ValueError('Chat must contain 1–500 characters')
        if self.player_types[recipient] == 'ai' and not self.enabled(recipient):
            raise ValueError('This saved game has not enabled chat for that player')
        if client_id is not None:
            if not isinstance(client_id, str) or not client_id or len(client_id) > 128:
                raise ValueError('Invalid chat request id')
            previous = self.client_requests.get((sender, client_id))
            if previous:
                if previous['to_pid'] != recipient or previous['message'] != message:
                    raise ValueError('Chat request id was reused for a different message')
                self._deliver(previous)
                return self._receipt(previous)
        if reply_to is not None:
            if not isinstance(reply_to, int) or isinstance(reply_to, bool):
                raise ValueError('reply_to must be an integer message id')
            original = self.messages.get(reply_to)
            if not original or original['to_pid'] != sender or original['from_pid'] != recipient or original.get('reply_to') is not None:
                raise ValueError('reply_to does not identify a received message from that player')
            if reply_to in self.replies:
                self._deliver(self.replies[reply_to])
                return f'消息 {reply_to} 已处理，无需重复发送。'
        self.sequence += 1
        event = {'type': 'game_chat', 'game_id': self.store.game_id, 'message_id': self.sequence, 'from_pid': sender, 'to_pid': recipient, 'message': message}
        if reply_to is not None:
            event['reply_to'] = reply_to
        if client_id is not None:
            event['client_id'] = client_id
        self.store.log_event(event)
        self._record(event)
        self._deliver(event)
        return self._receipt(event)

    def _deliver(self, event: dict) -> None:
        reply_to = event.get('reply_to')
        if reply_to is not None:
            self.acknowledge(event['from_pid'], reply_to)
        elif self.player_types[event['to_pid']] == 'ai' and event['message_id'] not in self.replies:
            self.enqueue(event['to_pid'], event['from_pid'], event['message'], event['message_id'])
        key = (event['type'], event.get('message_id', reply_to))
        if key not in self.published:
            self.publish(event)
            self.published.add(key)

    def _receipt(self, event: dict) -> str:
        if event.get('reply_to') is not None or self.player_types[event['to_pid']] != 'ai':
            return f"消息 {event['message_id']} 已发送。"
        return f"消息 {event['message_id']} 已收到，AI 将在下次行动时回复。"

    def mark_unanswered(self, recipient: int, message_ids: list[int], *, game_finished: bool = False) -> None:
        with self._lock:
            self._mark_unanswered(recipient, message_ids, game_finished=game_finished)

    def _mark_unanswered(self, recipient: int, message_ids: list[int], *, game_finished: bool = False) -> None:
        for message_id in message_ids:
            original = self.messages.get(message_id)
            if not original or original['to_pid'] != recipient:
                continue
            if message_id in self.replies:
                self._deliver(self.replies[message_id])
                continue
            text = '对局已结束，这条消息未能获得 AI 回复。' if game_finished else 'AI 暂未能生成这条消息的回复；游戏继续进行，你可以再次发送。'
            event = {'type': 'game_chat_status', 'game_id': self.store.game_id, 'from_pid': recipient, 'to_pid': original['from_pid'], 'reply_to': message_id, 'status': 'unanswered', 'message': text}
            self.store.log_event(event)
            self._record(event)
            self._deliver(event)
