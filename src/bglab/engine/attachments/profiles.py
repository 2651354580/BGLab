"""Immutable attachment profiles for the fixed query-loop collector."""

from bglab.engine.attachments.code_providers import (
    build_background_notifications,
    build_changed_files,
    build_compaction_reminder,
    build_date_change,
    build_deferred_tool_listing,
    build_plan_mode,
    build_plan_mode_exit,
    build_skill_listing,
    build_todo_reminders,
    prefetch_relevant_memory,
    prefetch_skill_discovery,
)
from bglab.engine.attachments.session_head import (
    build_code_post_compact,
    build_code_session_head,
)
from bglab.engine.attachments.types import AttachmentProfile, AttachmentProvider


CODE_ATTACHMENT_PROFILE = AttachmentProfile(
    name="code",
    head=(AttachmentProvider("session_head", build_code_session_head),),
    user_input=(),
    thread=(
        AttachmentProvider("queued_commands", build_background_notifications),
        AttachmentProvider("date_change", build_date_change),
        AttachmentProvider("changed_files", build_changed_files),
        AttachmentProvider("skill_listing", build_skill_listing),
        AttachmentProvider("plan_mode", build_plan_mode),
        AttachmentProvider("plan_mode_exit", build_plan_mode_exit),
        AttachmentProvider("todo_reminders", build_todo_reminders),
        AttachmentProvider("compaction_reminder", build_compaction_reminder),
        
        AttachmentProvider("deferred_tool_listing", build_deferred_tool_listing),
    ),
    main=(),
    post_compact=(
        AttachmentProvider("code_post_compact", build_code_post_compact),
    ),
    memory_prefetch=prefetch_relevant_memory,
    
    # lifecycle slot; ordinary skill listing is not used as a substitute.
    skill_prefetch=prefetch_skill_discovery,
)
